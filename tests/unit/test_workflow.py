"""The referral-to-appointment flow through the application service."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from agentic_core.audit import verify_chain
from agentic_core.domain.classification import Decision

from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
)
from patient_access_agents.domain.models import (
    CaseStatus,
    EligibilityOutcome,
    EscalationReason,
    EscalationStatus,
    Modality,
    ReminderKind,
    ReminderStatus,
)
from patient_access_agents.domain.scheduling import Constraint
from patient_access_agents.runtime.container import Container
from tests.conftest import (
    AGENT_PRINCIPAL,
    ELIGIBILITY,
    NAVIGATOR,
    PATIENT_ANA,
    SCHEDULER,
    SUPERVISOR,
)
from tests.fakes import FakeClock, World, add_referral


async def test_scheduler_takes_referral_to_booked_appointment(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    case, created = await service.intake(SCHEDULER, "sr-cardio")
    assert created
    assert case.status is CaseStatus.RECEIVED
    assert case.service == "cardiology-consult"
    assert case.sla_due_at == clock.now() + timedelta(hours=168)

    case = await service.verify_eligibility(SCHEDULER, case.id)
    assert case.status is CaseStatus.ELIGIBILITY_VERIFIED
    assert case.eligibility is not None
    assert case.eligibility.outcome is EligibilityOutcome.ACTIVE
    assert case.eligibility.plan_begin == "20260101"
    inquiry = world.clearinghouse.requests[0]
    assert "NM1*IL*1*RIVERA*ANA LUCIA****MI*SHP000123" in inquiry
    assert "EQ*3^98" in inquiry

    case, result = await service.propose_slots(SCHEDULER, case.id)
    assert case.status is CaseStatus.SLOTS_PROPOSED
    assert result.feasible
    first = case.proposals[0]
    assert len(first.slot_ids) == 2  # two 20-minute slots chained for a 40-minute visit
    assert first.start >= clock.now() + timedelta(hours=24)
    assert first.location_name in {"Riverbend Cardiology Clinic", "North Campus Heart Center", None}

    outcome = await service.book(SCHEDULER, case.id, first.proposal_id)
    assert outcome.outcome == "booked"
    case = outcome.case
    assert case.status is CaseStatus.SCHEDULED
    assert case.appointment is not None
    appointment = world.fhir.resources[("Appointment", case.appointment.appointment_id)]
    assert appointment["status"] == "booked"
    assert appointment["basedOn"] == [{"reference": "ServiceRequest/sr-cardio"}]
    for slot_id in first.slot_ids:
        assert world.fhir.resources[("Slot", slot_id)]["status"] == "busy"

    reminders = await service.reminders.for_case(case.id)
    outreach = [r for r in reminders if r.kind is ReminderKind.SCHEDULING_OUTREACH]
    assert outreach
    assert all(r.status is ReminderStatus.CANCELLED for r in outreach)
    planned = [r for r in reminders if r.kind is ReminderKind.APPOINTMENT_REMINDER]
    assert planned


async def test_audit_chain_covers_the_flow(container: Container) -> None:
    service = container.service
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    await service.verify_eligibility(SCHEDULER, case.id)
    lines = container.settings.audit_log_path.read_text(encoding="utf-8").splitlines()
    from agentic_core.audit import AuditEvent

    events = [AuditEvent.model_validate_json(line) for line in lines]
    key = service._audit._signer.public_key()  # type: ignore[attr-defined]
    assert verify_chain(events, {"audit-ephemeral": key}) == len(events)
    actions = [e.action for e in events]
    assert actions[:2] == ["case.create", "eligibility.verify"]
    text = "\n".join(lines)
    assert "Rivera" not in text
    assert "1980-04-02" not in text


async def test_intake_is_idempotent(service: PatientAccessService) -> None:
    first, created = await service.intake(SCHEDULER, "sr-cardio")
    again, created_again = await service.intake(SCHEDULER, "sr-cardio")
    assert created
    assert not created_again
    assert again.id == first.id


async def test_rejects_inactive_or_unknown_referrals(service: PatientAccessService) -> None:
    with pytest.raises(InvalidStateError, match="draft"):
        await service.intake(SCHEDULER, "sr-draft")
    with pytest.raises(NotFoundError):
        await service.intake(SCHEDULER, "sr-missing")


async def test_unroutable_referral_escalates_then_routes(service: PatientAccessService) -> None:
    case, _ = await service.intake(NAVIGATOR, "sr-unknown")
    assert case.service == "unrouted"
    open_items = await service.list_escalations(NAVIGATOR, case_id=case.id)
    assert [e.reason for e in open_items] == [EscalationReason.UNROUTABLE_REFERRAL]
    with pytest.raises(InvalidStateError, match="not routed"):
        await service.verify_eligibility(SUPERVISOR, case.id)
    routed = await service.route(NAVIGATOR, case.id, "cardiology-consult")
    assert routed.service == "cardiology-consult"
    items = await service.list_escalations(NAVIGATOR, case_id=case.id)
    assert items[0].status is EscalationStatus.RESOLVED
    with pytest.raises(NotFoundError):
        await service.route(NAVIGATOR, case.id, "no-such-service")


async def test_agent_booking_requires_human_approval(
    service: PatientAccessService, world: World
) -> None:
    case = await service.process_referral(AGENT_PRINCIPAL, "sr-cardio")
    assert case.status is CaseStatus.SLOTS_PROPOSED
    outcome = await service.book(AGENT_PRINCIPAL, case.id, case.proposals[0].proposal_id)
    assert outcome.outcome == "pending_approval"
    assert outcome.decision is Decision.REQUIRE_APPROVAL
    assert outcome.case.status is CaseStatus.BOOKING_PENDING_APPROVAL
    assert not world.fhir.of_type("Appointment")
    queue = await service.list_escalations(SCHEDULER, case_id=case.id)
    assert any(e.reason is EscalationReason.BOOKING_APPROVAL for e in queue)

    with pytest.raises(ForbiddenError):
        await service.decide_booking(AGENT_PRINCIPAL, case.id, approve=True, note="ok")
    approved = await service.decide_booking(SCHEDULER, case.id, approve=True, note="patient agreed")
    assert approved.outcome == "booked"
    assert approved.case.appointment is not None
    assert approved.case.appointment.booked_by == AGENT_PRINCIPAL.subject
    assert approved.case.appointment.approved_by == SCHEDULER.subject
    queue = await service.list_escalations(SCHEDULER, case_id=case.id)
    assert all(e.status is EscalationStatus.RESOLVED for e in queue)


async def test_declined_booking_returns_to_proposals(service: PatientAccessService) -> None:
    case = await service.process_referral(AGENT_PRINCIPAL, "sr-cardio")
    await service.book(AGENT_PRINCIPAL, case.id, case.proposals[0].proposal_id)
    declined = await service.decide_booking(
        SCHEDULER, case.id, approve=False, note="patient prefers afternoon"
    )
    assert declined.outcome == "declined"
    assert declined.case.status is CaseStatus.SLOTS_PROPOSED
    with pytest.raises(InvalidStateError):
        await service.decide_booking(SCHEDULER, case.id, approve=True, note="again")


async def test_requester_cannot_approve_own_booking(
    service: PatientAccessService, world: World
) -> None:
    add_referral(world.fhir, "sr-pa", member="SHP000PA")
    case = await service.process_referral(SCHEDULER, "sr-pa")
    assert case.prior_auth_pending
    outcome = await service.book(SCHEDULER, case.id, case.proposals[0].proposal_id)
    assert outcome.outcome == "pending_approval"  # prior authorization hold
    with pytest.raises(ForbiddenError, match="requested it"):
        await service.decide_booking(SCHEDULER, case.id, approve=True, note="self")
    other_scheduler = SCHEDULER.model_copy(update={"subject": "pat@corp.test"})
    with pytest.raises(ForbiddenError, match="eligibility staff"):
        await service.decide_booking(other_scheduler, case.id, approve=True, note="no")
    approved = await service.decide_booking(ELIGIBILITY, case.id, approve=True, note="auth 778")
    assert approved.case.status is CaseStatus.SCHEDULED


@pytest.mark.parametrize(
    ("member", "outcome", "status", "reason"),
    [
        (
            "SHP000INACT",
            EligibilityOutcome.INACTIVE,
            CaseStatus.ELIGIBILITY_DENIED,
            EscalationReason.ELIGIBILITY_DENIED,
        ),
        (
            "SHP000NF",
            EligibilityOutcome.REJECTED,
            CaseStatus.ELIGIBILITY_DENIED,
            EscalationReason.ELIGIBILITY_DENIED,
        ),
        (
            "SHP000NC",
            EligibilityOutcome.INACTIVE,
            CaseStatus.ELIGIBILITY_DENIED,
            EscalationReason.ELIGIBILITY_DENIED,
        ),
        (
            "SHP000UNK",
            EligibilityOutcome.MANUAL_REQUIRED,
            CaseStatus.ELIGIBILITY_PENDING,
            EscalationReason.MANUAL_VERIFICATION,
        ),
        (
            "SHP000PA",
            EligibilityOutcome.ACTIVE,
            CaseStatus.ELIGIBILITY_VERIFIED,
            EscalationReason.PRIOR_AUTHORIZATION,
        ),
    ],
)
async def test_eligibility_outcomes_escalate_to_people(
    service: PatientAccessService,
    world: World,
    *,
    member: str,
    outcome: EligibilityOutcome,
    status: CaseStatus,
    reason: EscalationReason,
) -> None:
    add_referral(world.fhir, "sr-x", member=member)
    case, _ = await service.intake(SCHEDULER, "sr-x")
    case = await service.verify_eligibility(ELIGIBILITY, case.id)
    assert case.eligibility is not None
    assert case.eligibility.outcome is outcome
    assert case.status is status
    items = await service.list_escalations(ELIGIBILITY, case_id=case.id)
    assert [e.reason for e in items] == [reason]
    assert items[0].queue == "eligibility"


async def test_manual_verification_clears_denial(
    service: PatientAccessService, world: World
) -> None:
    add_referral(world.fhir, "sr-x", member="SHP000NF")
    case, _ = await service.intake(SCHEDULER, "sr-x")
    case = await service.verify_eligibility(ELIGIBILITY, case.id)
    with pytest.raises(ForbiddenError):
        await service.record_manual_eligibility(
            SCHEDULER, case.id, active=True, note="called payer", payer_reference="REF-1"
        )
    case = await service.record_manual_eligibility(
        ELIGIBILITY, case.id, active=True, note="member id corrected", payer_reference="CALL-2231"
    )
    assert case.status is CaseStatus.ELIGIBILITY_VERIFIED
    assert case.eligibility is not None
    assert case.eligibility.source == "manual"
    items = await service.list_escalations(ELIGIBILITY, case_id=case.id)
    assert all(e.status is EscalationStatus.RESOLVED for e in items)


async def test_payer_unavailable_retries_then_escalates(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    add_referral(world.fhir, "sr-busy", member="SHP000BUSY")
    case, _ = await service.intake(SCHEDULER, "sr-busy")
    case = await service.verify_eligibility(SCHEDULER, case.id)
    assert case.status is CaseStatus.ELIGIBILITY_PENDING
    assert case.next_eligibility_attempt_at == clock.now() + timedelta(minutes=15)
    for _ in range(6):
        clock.advance(hours=5)
        await service.run_due_work()
    case = await service.get_case(SCHEDULER, case.id)
    assert case.eligibility_attempts == 5
    assert case.next_eligibility_attempt_at is None
    reasons = {e.reason for e in await service.list_escalations(SCHEDULER, case_id=case.id)}
    assert EscalationReason.ELIGIBILITY_UNAVAILABLE in reasons


async def test_dependent_coverage_uses_dependent_loop(
    service: PatientAccessService, world: World
) -> None:
    case, _ = await service.intake(SCHEDULER, "sr-leo")
    case = await service.verify_eligibility(SCHEDULER, case.id)
    assert case.preferences.language == "es"
    inquiry = world.clearinghouse.requests[-1]
    assert "HL*4*3*23*0" in inquiry
    assert "NM1*03*1*RIVERA*LEO" in inquiry
    assert "NM1*IL*1*RIVERA*ANA LUCIA****MI*SHP000123" in inquiry


async def test_no_capacity_and_availability_conflict(
    service: PatientAccessService, world: World
) -> None:
    from datetime import time

    from patient_access_agents.domain.models import AvailabilityWindow, SchedulingPreferences

    case = await service.process_referral(SCHEDULER, "sr-cardio")
    prefs = SchedulingPreferences(
        timezone="America/New_York",
        availability=(AvailabilityWindow(weekdays=frozenset({6, 7}), start=time(9), end=time(12)),),
    )
    case = await service.update_preferences(PATIENT_ANA, case.id, prefs)
    case, result = await service.propose_slots(PATIENT_ANA, case.id)
    assert case.status is CaseStatus.NO_CAPACITY
    assert result.binding_constraint is Constraint.AVAILABILITY
    reasons = [e.reason for e in await service.list_escalations(SCHEDULER, case_id=case.id)]
    assert EscalationReason.AVAILABILITY_CONFLICT in reasons

    prefs = SchedulingPreferences(
        timezone="America/New_York", modalities=frozenset({Modality.TELEHEALTH})
    )
    case = await service.update_preferences(PATIENT_ANA, case.id, prefs)
    case, result = await service.propose_slots(PATIENT_ANA, case.id)
    assert case.status is CaseStatus.SLOTS_PROPOSED
    assert {p.modality for p in case.proposals} == {Modality.TELEHEALTH}
    items = await service.list_escalations(SCHEDULER, case_id=case.id)
    assert all(e.status is EscalationStatus.RESOLVED for e in items)


async def test_stale_slot_is_detected_and_released(
    service: PatientAccessService, world: World
) -> None:
    case = await service.process_referral(SCHEDULER, "sr-cardio")
    proposal = case.proposals[0]
    taken = world.fhir.resources[("Slot", proposal.slot_ids[0])]
    world.fhir.put({**taken, "status": "busy"})
    with pytest.raises(ConflictError, match="no longer free"):
        await service.book(SCHEDULER, case.id, proposal.proposal_id)
    # A concurrent writer between our read and our write: If-Match fails, nothing stays busy.
    case = await service.get_case(SCHEDULER, case.id)
    second = case.proposals[1]
    original = world.fhir.handle

    def racing(request):  # type: ignore[no-untyped-def]
        if request.method == "PUT" and second.slot_ids[1] in request.url.path:
            world.fhir.put(dict(world.fhir.resources[("Slot", second.slot_ids[1])]))
        return original(request)

    world.fhir.handle = racing  # type: ignore[method-assign]
    with pytest.raises(ConflictError):
        await service.book(SCHEDULER, case.id, second.proposal_id)
    assert world.fhir.resources[("Slot", second.slot_ids[0])]["status"] == "free"


async def test_close_case_cancels_follow_up(service: PatientAccessService) -> None:
    case = await service.process_referral(SCHEDULER, "sr-cardio")
    with pytest.raises(ForbiddenError):
        await service.close(SCHEDULER, case.id, "duplicate referral")
    closed = await service.close(SUPERVISOR, case.id, "duplicate referral")
    assert closed.status is CaseStatus.CLOSED
    reminders = await service.reminders.for_case(case.id)
    assert all(r.status is ReminderStatus.CANCELLED for r in reminders)
    with pytest.raises(InvalidStateError):
        await service.close(SUPERVISOR, case.id, "again please")
    with pytest.raises(ForbiddenError):
        await service.propose_slots(SCHEDULER, case.id)


async def test_reminders_are_delivered_and_acknowledged(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    case = await service.process_referral(SCHEDULER, "sr-cardio")
    await service.run_due_work()  # outreach goes out now
    inbox = await service.messages(PATIENT_ANA)
    assert len(inbox) == 1
    assert "cardiology consultation" in inbox[0].body
    assert inbox[0].body.startswith("Hello Ana,")
    outcome = await service.book(PATIENT_ANA, case.id, case.proposals[0].proposal_id)
    assert outcome.outcome == "booked"
    clock.advance(days=30)
    await service.run_due_work()
    reminders = await service.reminders.for_case(case.id)
    sent = [r for r in reminders if r.kind is ReminderKind.APPOINTMENT_REMINDER]
    assert sent
    assert all(r.status is ReminderStatus.SENT for r in sent)
    assert all(r.composer == "template" for r in sent)
    acknowledged = await service.acknowledge(PATIENT_ANA, sent[0].id)
    assert acknowledged.status is ReminderStatus.ACKNOWLEDGED
    with pytest.raises(InvalidStateError):
        await service.acknowledge(PATIENT_ANA, sent[0].id)


async def test_sms_reminders_use_the_messaging_webhook(
    service: PatientAccessService, world: World, clock: FakeClock
) -> None:
    from patient_access_agents.domain.models import SchedulingPreferences

    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    await service.update_preferences(
        SCHEDULER, case.id, SchedulingPreferences(reminder_channel="sms")
    )
    await service.verify_eligibility(SCHEDULER, case.id)
    await service.propose_slots(SCHEDULER, case.id)
    await service.run_due_work()
    assert world.messaging.sent[0]["channel"] == "sms"
    assert world.messaging.sent[0]["to"] == "+1-555-0100"
    headers = world.messaging.headers[0]
    assert headers["x-signature-256"].startswith("sha256=")
    assert headers["idempotency-key"] == world.messaging.sent[0]["id"]
    body = json.dumps(world.messaging.sent[0], separators=(",", ":"), sort_keys=True).encode()
    import hashlib
    import hmac

    expected = "sha256=" + hmac.new(b"k" * 40, body, hashlib.sha256).hexdigest()
    assert headers["x-signature-256"] == expected


async def test_sla_escalations(service: PatientAccessService, clock: FakeClock) -> None:
    case, _ = await service.intake(SCHEDULER, "sr-cardio-stat")
    clock.advance(hours=3, minutes=10)
    assert await service.evaluate_slas() == 1
    clock.advance(hours=1)
    assert await service.evaluate_slas() == 1
    assert await service.evaluate_slas() == 0
    reasons = {e.reason: e for e in await service.list_escalations(SUPERVISOR, case_id=case.id)}
    assert reasons[EscalationReason.SLA_BREACH].priority.value == "critical"
    assert reasons[EscalationReason.SLA_BREACH].queue == "supervisor"


async def test_escalation_queue_work(service: PatientAccessService, world: World) -> None:
    add_referral(world.fhir, "sr-x", member="SHP000INACT")
    case, _ = await service.intake(SCHEDULER, "sr-x")
    await service.verify_eligibility(SCHEDULER, case.id)
    item = (await service.list_escalations(ELIGIBILITY, queue="eligibility"))[0]
    with pytest.raises(ForbiddenError):
        await service.claim_escalation(SCHEDULER, item.id)
    claimed = await service.claim_escalation(ELIGIBILITY, item.id)
    assert claimed.status is EscalationStatus.CLAIMED
    with pytest.raises(InvalidStateError):
        await service.claim_escalation(ELIGIBILITY, item.id)
    other = ELIGIBILITY.model_copy(update={"subject": "erin@corp.test"})
    with pytest.raises(ForbiddenError, match="claimed by someone else"):
        await service.resolve_escalation(other, item.id, "not mine to close")
    resolved = await service.resolve_escalation(SUPERVISOR, item.id, "patient self-pay agreed")
    assert resolved.status is EscalationStatus.RESOLVED
    with pytest.raises(InvalidStateError):
        await service.resolve_escalation(SUPERVISOR, item.id, "twice over")
    with pytest.raises(NotFoundError):
        await service.claim_escalation(SUPERVISOR, "missing")
