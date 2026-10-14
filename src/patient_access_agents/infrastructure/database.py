"""SQLAlchemy Core persistence. PostgreSQL in production; any async dialect works.

Aggregates are stored as JSON documents with the columns needed for queries
promoted alongside. Cases use optimistic concurrency (a version column) so
two replicas or two people cannot overwrite each other's changes silently.
Reminders are claimed with a lease so that only one replica sends each one.
Open escalations are de-duplicated by a unique key per case and reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from patient_access_agents.domain.errors import ConflictError
from patient_access_agents.domain.models import (
    SCHEDULED_STATES,
    AccessCase,
    CaseStatus,
    Escalation,
    EscalationStatus,
    PortalMessage,
    Reminder,
    ReminderStatus,
)

metadata = MetaData()

access_cases = Table(
    "access_cases",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("service_request_id", String(64), nullable=False, unique=True),
    Column("patient_id", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("sla_due_at", DateTime(timezone=True), nullable=False),
    Column("next_eligibility_attempt_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_cases_patient", "patient_id"),
    Index("ix_cases_status", "status"),
    Index("ix_cases_eligibility_due", "next_eligibility_attempt_at"),
)

reminders = Table(
    "reminders",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("case_id", String(64), nullable=False),
    Column("patient_id", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("document", Text, nullable=False),
    Index("ix_reminders_due", "status", "due_at"),
    Index("ix_reminders_case", "case_id"),
)

escalations = Table(
    "escalations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("dedupe_key", String(160), nullable=False, unique=True),
    Column("case_id", String(64), nullable=False),
    Column("queue", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_escalations_queue", "queue", "status"),
    Index("ix_escalations_case", "case_id"),
)

portal_messages = Table(
    "portal_messages",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("patient_id", String(64), nullable=False),
    Column("reminder_id", String(64), nullable=False, unique=True),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_portal_patient", "patient_id", "created_at"),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def _aware(value: datetime) -> datetime:
    """Normalise to UTC so instants compare correctly on dialects without time zones."""
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


def _maybe(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


class SqlCaseRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _columns(case: AccessCase) -> dict[str, object]:
        return {
            "patient_id": case.patient_id,
            "status": case.status.value,
            "sla_due_at": _aware(case.sla_due_at),
            "next_eligibility_attempt_at": _maybe(case.next_eligibility_attempt_at),
            "version": case.version,
            "document": case.model_dump_json(),
            "updated_at": _aware(case.updated_at),
        }

    async def add(self, case: AccessCase) -> AccessCase:
        stored = case.model_copy(update={"version": 1})
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(access_cases).values(
                        id=stored.id,
                        service_request_id=stored.service_request_id,
                        created_at=_aware(stored.created_at),
                        **self._columns(stored),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(
                f"a case for ServiceRequest/{case.service_request_id} already exists"
            ) from exc
        return stored

    async def get(self, case_id: str) -> AccessCase | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(access_cases.c.document).where(access_cases.c.id == case_id)
                )
            ).first()
        return AccessCase.model_validate_json(row[0]) if row else None

    async def by_service_request(self, service_request_id: str) -> AccessCase | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(access_cases.c.document).where(
                        access_cases.c.service_request_id == service_request_id
                    )
                )
            ).first()
        return AccessCase.model_validate_json(row[0]) if row else None

    async def save(self, case: AccessCase) -> AccessCase:
        stored = case.model_copy(update={"version": case.version + 1})
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(access_cases)
                .where(and_(access_cases.c.id == case.id, access_cases.c.version == case.version))
                .values(**self._columns(stored))
            )
        if result.rowcount != 1:
            raise ConflictError(f"case {case.id} was changed concurrently; reload and retry")
        return stored

    async def list(
        self,
        *,
        patient_id: str | None = None,
        status: CaseStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AccessCase]:
        query = (
            select(access_cases.c.document)
            .order_by(access_cases.c.created_at.desc(), access_cases.c.id)
            .limit(limit)
            .offset(offset)
        )
        if patient_id is not None:
            query = query.where(access_cases.c.patient_id == patient_id)
        if status is not None:
            query = query.where(access_cases.c.status == status.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [AccessCase.model_validate_json(r[0]) for r in rows]

    async def open_cases(self, *, limit: int = 500) -> Sequence[AccessCase]:
        query = (
            select(access_cases.c.document)
            .where(access_cases.c.status.not_in([s.value for s in SCHEDULED_STATES]))
            .order_by(access_cases.c.sla_due_at)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [AccessCase.model_validate_json(r[0]) for r in rows]

    async def eligibility_due(self, now: datetime, *, limit: int = 100) -> Sequence[AccessCase]:
        query = (
            select(access_cases.c.document)
            .where(
                and_(
                    access_cases.c.status == CaseStatus.ELIGIBILITY_PENDING.value,
                    access_cases.c.next_eligibility_attempt_at.is_not(None),
                    access_cases.c.next_eligibility_attempt_at <= _aware(now),
                )
            )
            .order_by(access_cases.c.next_eligibility_attempt_at)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [AccessCase.model_validate_json(r[0]) for r in rows]


class SqlReminderRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, reminder: Reminder) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(reminders).values(
                    id=reminder.id,
                    case_id=reminder.case_id,
                    patient_id=reminder.patient_id,
                    status=reminder.status.value,
                    due_at=_aware(reminder.due_at),
                    document=reminder.model_dump_json(),
                )
            )

    async def get(self, reminder_id: str) -> Reminder | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(reminders.c.document).where(reminders.c.id == reminder_id)
                )
            ).first()
        return Reminder.model_validate_json(row[0]) if row else None

    async def save(self, reminder: Reminder) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(reminders)
                .where(reminders.c.id == reminder.id)
                .values(
                    status=reminder.status.value,
                    due_at=_aware(reminder.due_at),
                    document=reminder.model_dump_json(),
                    lease_owner=None,
                    lease_until=None,
                )
            )

    async def for_case(self, case_id: str) -> Sequence[Reminder]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(reminders.c.document)
                    .where(reminders.c.case_id == case_id)
                    .order_by(reminders.c.due_at)
                )
            ).all()
        return [Reminder.model_validate_json(r[0]) for r in rows]

    async def claim_due(
        self, now: datetime, *, owner: str, lease_seconds: int, limit: int = 50
    ) -> Sequence[Reminder]:
        """Lease due reminders to ``owner``; a reminder is never leased to two workers."""
        now = _aware(now)
        available = or_(reminders.c.lease_until.is_(None), reminders.c.lease_until < now)
        async with self._engine.connect() as conn:
            ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(reminders.c.id)
                        .where(
                            and_(
                                reminders.c.status == ReminderStatus.PENDING.value,
                                reminders.c.due_at <= now,
                                available,
                            )
                        )
                        .order_by(reminders.c.due_at)
                        .limit(limit)
                    )
                ).all()
            ]
        claimed: list[Reminder] = []
        until = now + timedelta(seconds=lease_seconds)
        for reminder_id in ids:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    update(reminders)
                    .where(
                        and_(
                            reminders.c.id == reminder_id,
                            reminders.c.status == ReminderStatus.PENDING.value,
                            available,
                        )
                    )
                    .values(lease_owner=owner, lease_until=until)
                )
                if result.rowcount != 1:
                    continue
                row = (
                    await conn.execute(
                        select(reminders.c.document).where(reminders.c.id == reminder_id)
                    )
                ).first()
            if row:
                claimed.append(Reminder.model_validate_json(row[0]))
        return claimed

    async def release(self, reminder_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(reminders)
                .where(reminders.c.id == reminder_id)
                .values(lease_owner=None, lease_until=None)
            )

    async def count(self, status: ReminderStatus) -> int:
        async with self._engine.connect() as conn:
            value = (
                await conn.execute(
                    select(func.count())
                    .select_from(reminders)
                    .where(reminders.c.status == status.value)
                )
            ).scalar()
        return int(value or 0)


class SqlEscalationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def open(self, escalation: Escalation) -> tuple[Escalation, bool]:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(escalations).values(
                        id=escalation.id,
                        dedupe_key=escalation.dedupe_key,
                        case_id=escalation.case_id,
                        queue=escalation.queue,
                        status=escalation.status.value,
                        priority=escalation.priority.value,
                        document=escalation.model_dump_json(),
                        created_at=_aware(escalation.created_at),
                    )
                )
        except IntegrityError:
            async with self._engine.connect() as conn:
                row = (
                    await conn.execute(
                        select(escalations.c.document).where(
                            escalations.c.dedupe_key == escalation.dedupe_key
                        )
                    )
                ).first()
            if row is None:  # pragma: no cover - only if the row vanished between statements
                raise
            return Escalation.model_validate_json(row[0]), False
        return escalation, True

    async def get(self, escalation_id: str) -> Escalation | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(escalations.c.document).where(escalations.c.id == escalation_id)
                )
            ).first()
        return Escalation.model_validate_json(row[0]) if row else None

    async def save(self, escalation: Escalation) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(escalations)
                .where(escalations.c.id == escalation.id)
                .values(
                    dedupe_key=escalation.dedupe_key,
                    status=escalation.status.value,
                    priority=escalation.priority.value,
                    document=escalation.model_dump_json(),
                )
            )

    async def list(
        self,
        *,
        queues: Sequence[str] | None = None,
        status: EscalationStatus | None = None,
        case_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Escalation]:
        query = (
            select(escalations.c.document)
            .order_by(escalations.c.created_at, escalations.c.id)
            .limit(limit)
            .offset(offset)
        )
        if queues is not None:
            query = query.where(escalations.c.queue.in_(list(queues)))
        if status is not None:
            query = query.where(escalations.c.status == status.value)
        if case_id is not None:
            query = query.where(escalations.c.case_id == case_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Escalation.model_validate_json(r[0]) for r in rows]

    async def count_open(self) -> dict[str, int]:
        query = (
            select(escalations.c.queue, func.count())
            .where(escalations.c.status != EscalationStatus.RESOLVED.value)
            .group_by(escalations.c.queue)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return {str(r[0]): int(r[1]) for r in rows}


class SqlPortalInbox:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def deliver(self, message: PortalMessage) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(portal_messages).values(
                        id=message.id,
                        patient_id=message.patient_id,
                        reminder_id=message.reminder_id,
                        document=message.model_dump_json(),
                        created_at=_aware(message.created_at),
                    )
                )
        except IntegrityError:
            return  # already delivered for this reminder: idempotent

    async def for_patient(self, patient_id: str, *, limit: int = 50) -> Sequence[PortalMessage]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(portal_messages.c.document)
                    .where(portal_messages.c.patient_id == patient_id)
                    .order_by(portal_messages.c.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return [PortalMessage.model_validate_json(r[0]) for r in rows]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
