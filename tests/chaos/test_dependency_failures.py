"""Fault injection: dependencies fail, the service fails closed and recovers, nothing is lost."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.domain.errors import UpstreamError
from patient_access_agents.domain.models import (
    CaseStatus,
    EscalationReason,
    ReminderStatus,
    SchedulingPreferences,
)
from patient_access_agents.infrastructure.database import make_engine
from patient_access_agents.runtime.container import Container
from tests.conftest import SCHEDULER, agent_token, bearer, staff_token
from tests.fakes import FakeClock, FakeIdp, World

pytestmark = pytest.mark.chaos


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200  # liveness unaffected
    finally:
        container.engine = healthy


def test_fhir_outage_is_503_not_500(client: TestClient, user_idp: FakeIdp, world: World) -> None:
    world.fhir.down = True
    response = client.post(
        "/v1/referrals",
        json={"service_request_id": "sr-cardio"},
        headers=bearer(staff_token(user_idp, "access-scheduler")),
    )
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    world.fhir.down = False
    world.fhir.fail["/fhir/ServiceRequest"] = 502
    retry = client.post(
        "/v1/referrals",
        json={"service_request_id": "sr-cardio"},
        headers=bearer(staff_token(user_idp, "access-scheduler")),
    )
    assert retry.status_code == 502
    assert retry.json()["error"] == "upstream_error"


async def test_clearinghouse_outage_retries_with_backoff_then_recovers(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    world.clearinghouse.http_status = 503
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    case = await service.verify_eligibility(SCHEDULER, case.id)
    assert case.status is CaseStatus.ELIGIBILITY_PENDING
    assert case.next_eligibility_attempt_at is not None
    clock.advance(minutes=16)
    await service.run_due_work()
    case = await service.get_case(SCHEDULER, case.id)
    assert case.eligibility_attempts == 2
    world.clearinghouse.http_status = None
    clock.advance(minutes=31)
    await service.run_due_work()
    case = await service.get_case(SCHEDULER, case.id)
    assert case.status is CaseStatus.SLOTS_PROPOSED  # retry succeeded and the flow continued


@pytest.mark.parametrize("fault", ["timeout", "garbage", "http_400"])
async def test_clearinghouse_faults_never_fake_a_coverage_decision(
    service: PatientAccessService, world: World, fault: str
) -> None:
    if fault == "timeout":
        world.clearinghouse.timeout = True
    elif fault == "garbage":
        world.clearinghouse.raw_reply = "<html>gateway error</html>"
    else:
        world.clearinghouse.http_status = 400
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    case = await service.verify_eligibility(SCHEDULER, case.id)
    assert case.status is CaseStatus.ELIGIBILITY_PENDING
    assert case.eligibility is not None
    assert case.eligibility.source == "unavailable"
    reasons = {e.reason for e in await service.list_escalations(SCHEDULER, case_id=case.id)}
    if fault == "timeout":
        assert case.next_eligibility_attempt_at is not None
        assert not reasons
    else:
        assert EscalationReason.ELIGIBILITY_UNAVAILABLE in reasons


async def test_no_clearinghouse_configured_routes_to_people(
    tmp_path, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp  # type: ignore[no-untyped-def]
) -> None:
    from tests.conftest import build, make_settings

    settings = make_settings(tmp_path, clearinghouse_url=None, notification_webhook_url=None)
    container = await build(settings, world, clock, user_idp, agent_idp)
    try:
        case, _ = await container.service.intake(SCHEDULER, "sr-cardio")
        case = await container.service.verify_eligibility(SCHEDULER, case.id)
        assert case.status is CaseStatus.ELIGIBILITY_PENDING
        items = await container.service.list_escalations(SCHEDULER, case_id=case.id)
        assert [e.reason for e in items] == [EscalationReason.MANUAL_VERIFICATION]
        # Coverage missing entirely: also a person's job.
        world.fhir.resources.pop(("Coverage", "cov-ana"))
        world.fhir.resources.pop(("Coverage", "cov-leo"))
        other, _ = await container.service.intake(SCHEDULER, "sr-cardio-stat")
        other = await container.service.verify_eligibility(SCHEDULER, other.id)
        assert other.eligibility is not None
        assert other.eligibility.rejection_reason == "no active Coverage on file for the patient"
    finally:
        await container.aclose()


async def test_messaging_outage_backs_off_then_escalates(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    await service.update_preferences(
        SCHEDULER, case.id, SchedulingPreferences(reminder_channel="email")
    )
    await service.verify_eligibility(SCHEDULER, case.id)
    await service.propose_slots(SCHEDULER, case.id)
    world.messaging.status = 503
    for _ in range(6):
        await service.run_due_work()
        clock.advance(hours=4)
    reminders = await service.reminders.for_case(case.id)
    assert reminders[0].status is ReminderStatus.FAILED
    assert reminders[0].attempts == 4
    reasons = {e.reason for e in await service.list_escalations(SCHEDULER, case_id=case.id)}
    assert EscalationReason.REMINDER_FAILED in reasons


async def test_rejected_message_is_not_retried(service: PatientAccessService, world: World) -> None:
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    await service.update_preferences(
        SCHEDULER, case.id, SchedulingPreferences(reminder_channel="sms")
    )
    await service.verify_eligibility(SCHEDULER, case.id)
    await service.propose_slots(SCHEDULER, case.id)
    world.messaging.status = 400
    await service.run_due_work()
    reminders = await service.reminders.for_case(case.id)
    assert reminders[0].status is ReminderStatus.FAILED
    assert reminders[0].attempts == 1


async def test_fhir_outage_during_reminder_delivery_is_retried(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    await service.process_referral(SCHEDULER, "sr-cardio")
    world.fhir.down = True
    await service.run_due_work()
    world.fhir.down = False
    clock.advance(minutes=11)
    await service.run_due_work()
    case = (await service.list_cases(SCHEDULER))[0]
    reminder = (await service.reminders.for_case(case.id))[0]
    assert reminder.status is ReminderStatus.SENT
    assert reminder.attempts == 2


async def test_audit_failure_blocks_state_change(container: Container) -> None:
    service = container.service

    async def broken(**_: object) -> None:
        raise OSError("audit volume full")

    service._audit.record = broken  # type: ignore[method-assign]
    with pytest.raises(OSError, match="audit volume full"):
        await service.intake(SCHEDULER, "sr-cardio")
    assert await service.cases.by_service_request("sr-cardio") is None


def test_identity_fabric_outage_fails_closed(
    tmp_path, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp  # type: ignore[no-untyped-def]
) -> None:
    from patient_access_agents.api.app import create_app
    from tests.conftest import build, make_settings
    from tests.fakes import FABRIC_URL

    settings = make_settings(
        tmp_path, fabric_url=FABRIC_URL, agent_issuer=None, agent_audience=None, agent_jwks_url=None
    )

    async def factory() -> Container:
        return await build(settings, world, clock, user_idp, agent_idp)

    token = agent_token(agent_idp)
    world.fabric.issue(token, "spiffe://agents.corp.test/agent/access/coordinator", "case:*")
    with TestClient(create_app(factory)) as client:
        ok = client.post(
            "/v1/referrals", json={"service_request_id": "sr-cardio"}, headers=bearer(token)
        )
        assert ok.status_code == 201
        world.fabric.down = True
        down = client.get(f"/v1/cases/{ok.json()['case_id']}", headers=bearer(token))
        assert down.status_code == 503
        people = client.get("/v1/cases", headers=bearer(staff_token(user_idp, "access-scheduler")))
        assert people.status_code == 200  # staff are unaffected by the agent identity outage


async def test_slot_release_failure_is_logged_not_hidden(
    service: PatientAccessService, world: World
) -> None:
    case = await service.process_referral(SCHEDULER, "sr-cardio")
    proposal = case.proposals[0]
    original = world.fhir.handle
    puts = 0

    def flaky(request):  # type: ignore[no-untyped-def]
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            if puts >= 2:
                return httpx.Response(500)
        return original(request)

    world.fhir.handle = flaky  # type: ignore[method-assign]
    with pytest.raises(UpstreamError):
        await service.book(SCHEDULER, case.id, proposal.proposal_id)
    case = await service.get_case(SCHEDULER, case.id)
    assert case.status is CaseStatus.SLOTS_PROPOSED
