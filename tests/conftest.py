"""Shared fixtures: a synthetic health system, a service wired to SQLite, and an app."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from patient_access_agents.api.app import create_app
from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.config import Settings
from patient_access_agents.infrastructure.database import make_engine, metadata
from patient_access_agents.runtime.container import Container, build_container
from patient_access_agents.security.jwks import StaticJwks
from patient_access_agents.security.principals import Principal, PrincipalKind, StaffRole
from tests.fakes import (
    CLEARINGHOUSE_URL,
    FHIR_BASE,
    MESSAGING_URL,
    FakeClock,
    FakeIdp,
    World,
    seed_world,
)

ROOT = Path(__file__).resolve().parents[1]
USER_ISSUER = "https://login.corp.test"
USER_AUDIENCE = "patient-access"
AGENT_ISSUER = "https://agents.corp.test"
AGENT_AUDIENCE = "patient-access-agents"
AGENT = "spiffe://agents.corp.test/agent/access/coordinator"
AGENT_SCOPES = frozenset(
    {"case:*", "eligibility:verify", "slots:search", "appointment:book", "escalation:read"}
)

SCHEDULER = Principal(
    subject="sam@corp.test", kind=PrincipalKind.STAFF, roles=frozenset({StaffRole.SCHEDULER})
)
ELIGIBILITY = Principal(
    subject="eli@corp.test", kind=PrincipalKind.STAFF, roles=frozenset({StaffRole.ELIGIBILITY})
)
NAVIGATOR = Principal(
    subject="nia@corp.test", kind=PrincipalKind.STAFF, roles=frozenset({StaffRole.NAVIGATOR})
)
SUPERVISOR = Principal(
    subject="sue@corp.test", kind=PrincipalKind.STAFF, roles=frozenset({StaffRole.SUPERVISOR})
)
AUDITOR = Principal(
    subject="aud@corp.test", kind=PrincipalKind.STAFF, roles=frozenset({StaffRole.AUDITOR})
)
PATIENT_ANA = Principal(subject="ana@mail.test", kind=PrincipalKind.PATIENT, patient_id="pat-ana")
PATIENT_LEO = Principal(
    subject="leo-guardian@mail.test", kind=PrincipalKind.PATIENT, patient_id="pat-leo"
)
AGENT_PRINCIPAL = Principal(subject=AGENT, kind=PrincipalKind.AGENT, scopes=AGENT_SCOPES)


@pytest.fixture(autouse=True)
async def _fresh_postgres() -> None:
    """Each test starts from an empty schema when running against PostgreSQL."""
    url = os.environ.get("PAA_TEST_DATABASE_URL", "")
    if url.startswith("postgresql"):
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world() -> World:
    w = World()
    seed_world(w.fhir)
    return w


@pytest.fixture
def user_idp(clock: FakeClock) -> FakeIdp:
    return FakeIdp(USER_ISSUER, clock)


@pytest.fixture
def agent_idp(clock: FakeClock) -> FakeIdp:
    return FakeIdp(AGENT_ISSUER, clock, kid="agent-1")


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    db = os.environ.get("PAA_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'paa.db'}")
    values: dict[str, Any] = {
        "environment": "test",
        "public_url": "https://access.corp.test",
        "database_url": db,
        "dev_ephemeral_audit_key": True,
        "audit_log_path": tmp_path / "audit.jsonl",
        "fhir_base_url": FHIR_BASE,
        "oidc_issuer": USER_ISSUER,
        "oidc_audience": USER_AUDIENCE,
        "oidc_jwks_url": f"{USER_ISSUER}/jwks",
        "agent_issuer": AGENT_ISSUER,
        "agent_audience": AGENT_AUDIENCE,
        "agent_jwks_url": f"{AGENT_ISSUER}/jwks",
        "clearinghouse_url": CLEARINGHOUSE_URL,
        "x12_sender_id": "SUBMITTER01",
        "x12_receiver_id": "CLEARING01",
        "x12_application_sender": "SUB01",
        "x12_application_receiver": "CH01",
        "provider_name": "Riverbend Specialty Clinic",
        "provider_npi": "1234567893",
        "provider_tax_id": "123456789",
        "notification_webhook_url": MESSAGING_URL,
        "notification_signing_key": "k" * 40,
        "policy_file": ROOT / "policies" / "patient-access.yaml",
        "rules_file": ROOT / "configs" / "access-rules.yaml",
        "workers_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


async def build(
    settings: Settings,
    world: World,
    clock: FakeClock,
    user_idp: FakeIdp,
    agent_idp: FakeIdp,
    **kwargs: Any,
) -> Container:
    return await build_container(
        settings,
        http=httpx.AsyncClient(transport=world.transport()),
        user_resolver=StaticJwks(user_idp.jwks()),
        agent_resolver=StaticJwks(agent_idp.jwks()),
        clock=clock,
        **kwargs,
    )


@pytest.fixture
async def container(
    settings: Settings, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp
) -> AsyncIterator[Container]:
    built = await build(settings, world, clock, user_idp, agent_idp)
    yield built
    await built.aclose()


@pytest.fixture
def service(container: Container) -> PatientAccessService:
    return container.service


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def staff_token(idp: FakeIdp, *roles: str, sub: str = "sam@corp.test") -> str:
    return idp.mint(sub=sub, aud=USER_AUDIENCE, roles=list(roles))


def patient_token(idp: FakeIdp, patient: str = "pat-ana", sub: str = "ana@mail.test") -> str:
    return idp.mint(sub=sub, aud=USER_AUDIENCE, patient=patient)


def agent_token(idp: FakeIdp, scopes: frozenset[str] = AGENT_SCOPES, **extra: Any) -> str:
    return idp.mint(sub=AGENT, aud=AGENT_AUDIENCE, scope=" ".join(sorted(scopes)), **extra)


@pytest.fixture
def client(
    settings: Settings, world: World, clock: FakeClock, user_idp: FakeIdp, agent_idp: FakeIdp
) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""

    async def factory() -> Container:
        return await build(settings, world, clock, user_idp, agent_idp)

    with TestClient(create_app(factory)) as test_client:
        yield test_client
