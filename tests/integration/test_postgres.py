"""Repository behaviour against a real PostgreSQL (CI service container).

Runs when PAA_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://paa:paa@localhost:5432/paa
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from patient_access_agents.domain.errors import ConflictError
from patient_access_agents.domain.models import (
    Escalation,
    EscalationPriority,
    EscalationReason,
    Reminder,
    ReminderKind,
)
from patient_access_agents.infrastructure.database import (
    SqlCaseRepository,
    SqlEscalationRepository,
    SqlReminderRepository,
    create_schema,
    make_engine,
)
from tests.conftest import SCHEDULER

URL = os.environ.get("PAA_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_repositories_on_postgres(service) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    stamp = f"{datetime.now(UTC).timestamp():.0f}"
    cases = SqlCaseRepository(engine)
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    copy = case.model_copy(
        update={"id": f"pg-{stamp}", "service_request_id": f"sr-pg-{stamp}", "version": 0}
    )
    stored = await cases.add(copy)
    assert (await cases.get(stored.id)) == stored
    saved = await cases.save(stored)
    with pytest.raises(ConflictError):
        await cases.save(stored)
    assert saved.version == 2
    assert any(c.id == stored.id for c in await cases.open_cases())

    reminders = SqlReminderRepository(engine)
    now = datetime.now(UTC)
    reminder = Reminder(
        case_id=stored.id,
        patient_id="pat-ana",
        kind=ReminderKind.SCHEDULING_OUTREACH,
        channel="portal",
        due_at=now - timedelta(minutes=1),
        created_at=now,
    )
    await reminders.add(reminder)
    first = await reminders.claim_due(now, owner="a", lease_seconds=60, limit=500)
    second = await reminders.claim_due(now, owner="b", lease_seconds=60, limit=500)
    assert reminder.id in {r.id for r in first}
    assert reminder.id not in {r.id for r in second}

    escalations = SqlEscalationRepository(engine)
    item = Escalation(
        case_id=stored.id,
        patient_id="pat-ana",
        reason=EscalationReason.SLA_AT_RISK,
        queue="scheduling",
        priority=EscalationPriority.NORMAL,
        summary="s",
        created_at=now,
        updated_at=now,
    )
    _, created = await escalations.open(item)
    _, again = await escalations.open(item.model_copy(update={"id": f"dup-{stamp}"}))
    assert created
    assert not again
    await engine.dispose()
