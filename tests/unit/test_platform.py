"""Authentication, authorization, configuration, wiring, persistence and the ADK adapter."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from agentic_core.domain.classification import Decision
from agentic_core.policy import PolicyEngine, PolicySet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from patient_access_agents.adapters.adk import AdkToolset, build_adk_agent
from patient_access_agents.api.app import run_workers_forever
from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from patient_access_agents.domain.models import (
    Escalation,
    EscalationPriority,
    EscalationReason,
    PortalMessage,
    Reminder,
    ReminderKind,
    ReminderStatus,
)
from patient_access_agents.infrastructure.database import (
    SqlEscalationRepository,
    SqlPortalInbox,
    SqlReminderRepository,
    create_schema,
    make_engine,
)
from patient_access_agents.integrations.identity_fabric import FabricClient
from patient_access_agents.runtime.container import Container
from patient_access_agents.security.access import AccessController, purpose_of_use
from patient_access_agents.security.authn import Authenticator, bearer_token, delegation_chain
from patient_access_agents.security.jwks import RemoteJwks, StaticJwks, verify_jwt
from patient_access_agents.security.principals import Principal, PrincipalKind
from tests.conftest import (
    AGENT,
    AGENT_AUDIENCE,
    AGENT_ISSUER,
    AGENT_PRINCIPAL,
    PATIENT_ANA,
    ROOT,
    SCHEDULER,
    USER_AUDIENCE,
    USER_ISSUER,
    agent_token,
    build,
    make_settings,
)
from tests.fakes import FABRIC_URL, START, FakeClock, FakeIdp, World

POLICY = PolicyEngine(PolicySet.from_file(ROOT / "policies" / "patient-access.yaml"))


def authenticator(user_idp: FakeIdp, clock: FakeClock, **kwargs: Any) -> Authenticator:
    return Authenticator(
        user_resolver=StaticJwks(user_idp.jwks()),
        user_issuer=USER_ISSUER,
        user_audience=USER_AUDIENCE,
        clock=clock.now,
        **kwargs,
    )


class TestAuthentication:
    async def test_people(self, user_idp: FakeIdp, clock: FakeClock) -> None:
        authn = authenticator(user_idp, clock)
        patient = await authn.authenticate(
            "Bearer "
            + user_idp.mint(sub="u1", aud=USER_AUDIENCE, fhirUser="https://fhir/Patient/pat-9")
        )
        assert patient.kind is PrincipalKind.PATIENT
        assert patient.patient_id == "pat-9"
        staff = await authn.authenticate(
            "Bearer "
            + user_idp.mint(sub="u2", aud=USER_AUDIENCE, roles=["access-scheduler", "other"])
        )
        assert staff.kind is PrincipalKind.STAFF
        assert staff.roles == {"access-scheduler"}
        for claims in ({"roles": "access-scheduler"}, {"fhirUser": "Practitioner/x"}):
            with pytest.raises(ForbiddenError):
                await authn.authenticate(
                    "Bearer " + user_idp.mint(sub="u", aud=USER_AUDIENCE, **claims)
                )
        assert not authn.agents_enabled

    async def test_agents_without_configuration_are_refused(
        self, user_idp: FakeIdp, agent_idp: FakeIdp, clock: FakeClock
    ) -> None:
        authn = authenticator(user_idp, clock)
        with pytest.raises(UnauthorizedError, match="not accepted"):
            await authn.authenticate("Bearer " + agent_token(agent_idp))

    async def test_local_agent_verification(
        self, user_idp: FakeIdp, agent_idp: FakeIdp, clock: FakeClock
    ) -> None:
        authn = authenticator(
            user_idp,
            clock,
            agent_resolver=StaticJwks(agent_idp.jwks()),
            agent_issuer=AGENT_ISSUER,
            agent_audience=AGENT_AUDIENCE,
        )
        token = agent_token(agent_idp, act={"sub": "spiffe://a/agent/x/orchestrator"})
        agent = await authn.authenticate(f"Bearer {token}")
        assert agent.kind is PrincipalKind.AGENT
        assert agent.delegated
        assert agent.delegation_chain == (AGENT, "spiffe://a/agent/x/orchestrator")
        assert agent.in_scope("case:read")
        assert agent.in_scope("case:create")
        assert not agent.in_scope("eligibility:override")

    async def test_fabric_introspection(
        self, user_idp: FakeIdp, clock: FakeClock, world: World
    ) -> None:
        world.fabric.issue("cred-1", AGENT, "case:read", act={"sub": "spiffe://a/agent/x/o"})
        async with httpx.AsyncClient(transport=world.transport()) as http:
            fabric = FabricClient(FABRIC_URL, http=http, clock=clock.now)
            authn = authenticator(user_idp, clock, fabric=fabric)
            assert authn.agents_enabled
            token = FakeIdp("https://identity.corp.test", clock).mint(sub=AGENT, aud="x")
            world.fabric.tokens[token] = world.fabric.tokens["cred-1"]
            principal = await authn.authenticate(f"Bearer {token}")
            assert principal.delegated
            assert principal.credential == token
            other = FakeIdp("https://identity.corp.test", clock).mint(sub=AGENT, aud="y")
            with pytest.raises(UnauthorizedError, match="not active"):
                await authn.authenticate(f"Bearer {other}")

    async def test_token_shapes(self, user_idp: FakeIdp, clock: FakeClock) -> None:
        for header in (None, "Basic abc", "Bearer ", "Bearer " + "x" * 9000):
            with pytest.raises(UnauthorizedError):
                bearer_token(header)
        with pytest.raises(UnauthorizedError, match="malformed"):
            await authenticator(user_idp, clock).authenticate("Bearer not.a.jwt")
        assert delegation_chain("a", {"sub": "b", "act": {"sub": "c", "act": "junk"}}) == (
            "a",
            "b",
            "c",
        )

    async def test_jwt_time_checks_use_injected_clock(
        self, user_idp: FakeIdp, clock: FakeClock
    ) -> None:
        token = user_idp.mint(sub="u", aud=USER_AUDIENCE, ttl=60)
        resolver = StaticJwks(user_idp.jwks())
        await verify_jwt(
            token, resolver, issuer=USER_ISSUER, audience=USER_AUDIENCE, now=clock.now()
        )
        with pytest.raises(UnauthorizedError, match="Expired"):
            await verify_jwt(
                token,
                resolver,
                issuer=USER_ISSUER,
                audience=USER_AUDIENCE,
                now=clock.now() + timedelta(minutes=5),
            )
        with pytest.raises(UnauthorizedError, match="Immature"):
            await verify_jwt(
                token,
                resolver,
                issuer=USER_ISSUER,
                audience=USER_AUDIENCE,
                now=clock.now() - timedelta(minutes=5),
            )
        odd = user_idp.mint(sub="u", aud=USER_AUDIENCE, exp="soon")
        with pytest.raises(UnauthorizedError):
            await verify_jwt(
                odd, resolver, issuer=USER_ISSUER, audience=USER_AUDIENCE, now=clock.now()
            )

    async def test_remote_jwks_cache(self, user_idp: FakeIdp) -> None:
        calls: list[str] = []
        ticks = iter([0.0, 0.0, 10.0, 400.0, 400.0])

        def serve(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=user_idp.jwks())

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            jwks = RemoteJwks(
                "https://login.corp.test/jwks", client=http, clock=lambda: next(ticks)
            )
            await jwks.key_for("k1")
            with pytest.raises(UnauthorizedError, match="unknown signing key"):
                await jwks.key_for("nope")  # refresh rate-limited
            await jwks.key_for("k1")  # stale after ttl: refresh
        assert len(calls) == 2


class TestAccessControl:
    async def test_policy_matrix(self) -> None:
        access = AccessController(POLICY)
        own = {"patient_id": "pat-ana", "status": "slots_proposed", "prior_auth_pending": False}
        other = {**own, "patient_id": "pat-leo"}

        async def decide(who: Principal, action: str, attrs: dict[str, Any]) -> Decision:
            decision = await access.decide(
                who, action, resource_type="access_case", attributes=attrs
            )
            return decision.decision

        assert await decide(PATIENT_ANA, "case:read", own) is Decision.ALLOW
        assert await decide(PATIENT_ANA, "case:read", other) is Decision.DENY
        assert await decide(PATIENT_ANA, "appointment:book", own) is Decision.ALLOW
        assert await decide(PATIENT_ANA, "appointment:approve", own) is Decision.DENY
        assert await decide(SCHEDULER, "appointment:book", own) is Decision.ALLOW
        pending = {**own, "prior_auth_pending": True}
        assert await decide(SCHEDULER, "appointment:book", pending) is Decision.REQUIRE_APPROVAL
        assert await decide(AGENT_PRINCIPAL, "appointment:book", own) is Decision.REQUIRE_APPROVAL
        assert await decide(AGENT_PRINCIPAL, "case:close", own) is Decision.DENY
        closed = {**own, "status": "closed"}
        assert await decide(SCHEDULER, "slots:search", closed) is Decision.DENY
        assert purpose_of_use("eligibility:verify") == "HPAYMT"
        assert purpose_of_use("slots:search") == "TREAT"

    async def test_fabric_decision_can_only_tighten(self, world: World, clock: FakeClock) -> None:
        world.fabric.issue("cred", AGENT, "case:*")
        agent = AGENT_PRINCIPAL.model_copy(update={"credential": "cred"})
        async with httpx.AsyncClient(transport=world.transport()) as http:
            access = AccessController(
                POLICY, fabric=FabricClient(FABRIC_URL, http=http, clock=clock.now)
            )
            attrs = {"patient_id": "pat-ana", "status": "received", "flags": ["x"]}
            allowed = await access.decide(
                agent, "case:read", resource_type="access_case", attributes=attrs
            )
            assert allowed.decision is Decision.ALLOW
            assert allowed.fabric is not None
            assert "fabric_policy_digest" in allowed.audit()
            world.fabric.decision = "require_approval"
            tightened = await access.decide(
                agent, "case:read", resource_type="access_case", attributes=attrs
            )
            assert tightened.decision is Decision.REQUIRE_APPROVAL
            assert tightened.reason.startswith("identity fabric")
            world.fabric.decision = "deny"
            with pytest.raises(NotFoundError):
                await access.require(
                    agent, "case:read", resource_type="access_case", attributes=attrs, conceal=True
                )
            body = world.fabric.calls
            assert body.count("/v1/authorize") == 3


class TestSettings:
    def test_validation(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="production"):
            make_settings(tmp_path, environment="production")
        with pytest.raises(ValidationError, match="AUDIT_KEY_FILE"):
            make_settings(tmp_path, dev_ephemeral_audit_key=False)
        with pytest.raises(ValidationError, match="fhir_auth=fabric"):
            make_settings(tmp_path, fhir_auth="fabric")
        with pytest.raises(ValidationError, match="go together"):
            make_settings(tmp_path, agent_jwks_url=None)
        with pytest.raises(ValidationError, match="clearinghouse configuration incomplete"):
            make_settings(tmp_path, provider_npi=None)
        with pytest.raises(ValidationError, match="SIGNING_KEY"):
            make_settings(tmp_path, notification_signing_key="short")
        with pytest.raises(ValidationError, match="timezone"):
            make_settings(tmp_path, default_timezone="Mars/Olympus")
        with pytest.raises(ValidationError, match="timezone"):
            make_settings(tmp_path, default_timezone='"')
        assert make_settings(tmp_path).public_url_str == "https://access.corp.test"


def write_pem(path: Path, key: Any) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


class TestContainer:
    async def test_production_wiring(
        self, tmp_path: Path, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp
    ) -> None:
        audit_key = tmp_path / "audit-2026-10.pem"
        write_pem(audit_key, Ed25519PrivateKey.generate())
        token_file = tmp_path / "workload-token"
        token_file.write_text("workload-token-value\n", encoding="utf-8")
        settings = make_settings(
            tmp_path,
            environment="production",
            dev_ephemeral_audit_key=False,
            audit_key_file=audit_key,
            fhir_auth="fabric",
            fabric_url=FABRIC_URL,
            fabric_client_id="spiffe://agents.corp.test/agent/access/service",
            workload_token_file=token_file,
            agent_issuer=None,
            agent_audience=None,
            agent_jwks_url=None,
        )
        container = await build(settings, world, clock, user_idp, agent_idp)
        try:
            assert container.service._audit._key_id == "audit-2026-10"  # type: ignore[attr-defined]
            case, _ = await container.service.intake(SCHEDULER, "sr-cardio")
            assert world.fhir.requests[0].headers["authorization"] == "Bearer fhir-credential"
            assert case.service == "cardiology-consult"
        finally:
            await container.aclose()

    async def test_rejects_non_ed25519_audit_key(
        self, tmp_path: Path, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp
    ) -> None:
        audit_key = tmp_path / "audit.pem"
        write_pem(audit_key, generate_private_key(SECP256R1()))
        settings = make_settings(tmp_path, dev_ephemeral_audit_key=False, audit_key_file=audit_key)
        with pytest.raises(ValueError, match="Ed25519"):
            await build(settings, world, clock, user_idp, agent_idp)

    async def test_default_resolvers_are_remote(self, tmp_path: Path) -> None:
        from patient_access_agents.runtime.container import build_container

        settings = make_settings(tmp_path, clearinghouse_url=None, notification_webhook_url=None)
        container = await build_container(settings)
        try:
            assert isinstance(container.authn._user_resolver, RemoteJwks)  # type: ignore[attr-defined]
            assert isinstance(container.authn._agent_resolver, RemoteJwks)  # type: ignore[attr-defined]
            assert container.service.notifier is None
        finally:
            await container.aclose()


class TestPersistence:
    async def test_reminder_leases_and_escalation_dedupe(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        await create_schema(engine)
        await create_schema(engine)
        reminders = SqlReminderRepository(engine)
        reminder = Reminder(
            case_id="c1",
            patient_id="p1",
            kind=ReminderKind.SCHEDULING_OUTREACH,
            channel="portal",
            due_at=START,
            created_at=START,
        )
        await reminders.add(reminder)
        first = await reminders.claim_due(START, owner="a", lease_seconds=60)
        second = await reminders.claim_due(START, owner="b", lease_seconds=60)
        assert [r.id for r in first] == [reminder.id]
        assert second == []
        later = await reminders.claim_due(START + timedelta(minutes=2), owner="b", lease_seconds=60)
        assert [r.id for r in later] == [reminder.id]
        await reminders.release(reminder.id)
        await reminders.save(reminder.model_copy(update={"status": ReminderStatus.SENT}))
        assert await reminders.count(ReminderStatus.SENT) == 1
        assert await reminders.get("missing") is None

        escalations = SqlEscalationRepository(engine)
        item = Escalation(
            case_id="c1",
            patient_id="p1",
            reason=EscalationReason.NO_CAPACITY,
            queue="scheduling",
            priority=EscalationPriority.NORMAL,
            summary="s",
            created_at=START,
            updated_at=START,
        )
        stored, created = await escalations.open(item)
        duplicate, created_again = await escalations.open(item.model_copy(update={"id": "other"}))
        assert created
        assert not created_again
        assert duplicate.id == stored.id
        assert await escalations.count_open() == {"scheduling": 1}
        assert await escalations.get("missing") is None

        inbox = SqlPortalInbox(engine)
        message = PortalMessage(
            patient_id="p1", case_id="c1", reminder_id="r1", body="hi", created_at=START
        )
        await inbox.deliver(message)
        await inbox.deliver(message.model_copy(update={"id": "dup"}))
        assert len(await inbox.for_patient("p1")) == 1
        await engine.dispose()

    async def test_optimistic_concurrency(self, service: PatientAccessService) -> None:
        case, _ = await service.intake(SCHEDULER, "sr-cardio")
        await service.cases.save(case)
        with pytest.raises(ConflictError, match="concurrently"):
            await service.cases.save(case)
        with pytest.raises(ConflictError, match="already exists"):
            await service.cases.add(case.model_copy(update={"id": "another"}))


class TestAdk:
    async def test_toolset_runs_as_the_agent(self, service: PatientAccessService) -> None:
        tools = AdkToolset(service, AGENT_PRINCIPAL)
        names = [f.__name__ for f in tools.functions()]
        assert names == [
            "process_referral",
            "get_case_status",
            "verify_eligibility",
            "propose_slots",
            "request_booking",
        ]
        assert all(f.__doc__ for f in tools.functions())
        result = await tools.process_referral("sr-cardio")
        assert result["status"] == "ok"
        assert result["case"]["status"] == "slots_proposed"
        case_id = result["case"]["case_id"]
        assert (await tools.get_case_status(case_id))["status"] == "ok"
        assert (await tools.verify_eligibility(case_id))["error"] == "invalid_state"
        searched = await tools.propose_slots(case_id)
        assert searched["binding_constraint"] is None
        proposal = searched["case"]["proposals"][0]["proposal_id"]
        booked = await tools.request_booking(case_id, proposal)
        assert booked["outcome"] == "pending_approval"
        assert (await tools.request_booking(case_id, proposal))["error"] == "invalid_state"
        assert (await tools.get_case_status("missing"))["error"] == "not_found"
        assert (await tools.process_referral("sr-draft"))["error"] == "invalid_state"
        assert (await tools.propose_slots("missing"))["error"] == "not_found"

    def test_build_agent(
        self, monkeypatch: pytest.MonkeyPatch, service: PatientAccessService
    ) -> None:
        toolset = AdkToolset(service, AGENT_PRINCIPAL)
        monkeypatch.setitem(sys.modules, "google.adk.agents", None)
        with pytest.raises(RuntimeError, match="google-adk is not installed"):
            build_adk_agent(toolset)

        class LlmAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class FunctionTool:
            def __init__(self, func: Any) -> None:
                self.func = func

        agents = types.ModuleType("google.adk.agents")
        agents.LlmAgent = LlmAgent  # type: ignore[attr-defined]
        tools = types.ModuleType("google.adk.tools")
        tools.FunctionTool = FunctionTool  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "google.adk.agents", agents)
        monkeypatch.setitem(sys.modules, "google.adk.tools", tools)
        agent = build_adk_agent(toolset, model="gemini-2.5-flash")
        assert agent.kwargs["model"] == "gemini-2.5-flash"
        assert len(agent.kwargs["tools"]) == 5


class TestWorkers:
    async def test_worker_loop_survives_failures(self, container: Container) -> None:
        cycles = 0

        async def stop_after_two(_: float) -> None:
            nonlocal cycles
            cycles += 1
            if cycles == 2:
                raise asyncio.CancelledError

        original = container.service.run_due_work

        async def flaky() -> Any:
            if cycles == 0:
                raise DependencyUnavailableError("database blip")
            return await original()

        container.service.run_due_work = flaky  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await run_workers_forever(container, 0.01, sleep=stop_after_two)
        registry = container.metrics.registry
        assert registry.get_sample_value("paa_worker_runs_total", {"outcome": "failed"}) == 1
        assert registry.get_sample_value("paa_worker_runs_total", {"outcome": "ok"}) == 1
