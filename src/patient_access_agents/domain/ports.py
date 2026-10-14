"""Ports the application layer depends on. Adapters live in integrations and infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from patient_access_agents.domain.models import (
    AccessCase,
    CaseStatus,
    Escalation,
    EscalationStatus,
    PortalMessage,
    Reminder,
    ReminderStatus,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class CaseRepository(Protocol):
    async def add(self, case: AccessCase) -> AccessCase: ...

    async def get(self, case_id: str) -> AccessCase | None: ...

    async def by_service_request(self, service_request_id: str) -> AccessCase | None: ...

    async def save(self, case: AccessCase) -> AccessCase:
        """Persist with optimistic concurrency; raises ConflictError on a stale version."""
        ...

    async def list(
        self,
        *,
        patient_id: str | None = None,
        status: CaseStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AccessCase]: ...

    async def open_cases(self, *, limit: int = 500) -> Sequence[AccessCase]: ...

    async def eligibility_due(self, now: datetime, *, limit: int = 100) -> Sequence[AccessCase]: ...


class ReminderRepository(Protocol):
    async def add(self, reminder: Reminder) -> None: ...

    async def get(self, reminder_id: str) -> Reminder | None: ...

    async def save(self, reminder: Reminder) -> None: ...

    async def for_case(self, case_id: str) -> Sequence[Reminder]: ...

    async def claim_due(
        self, now: datetime, *, owner: str, lease_seconds: int, limit: int = 50
    ) -> Sequence[Reminder]: ...

    async def release(self, reminder_id: str) -> None: ...

    async def count(self, status: ReminderStatus) -> int: ...


class EscalationRepository(Protocol):
    async def open(self, escalation: Escalation) -> tuple[Escalation, bool]:
        """Insert unless an open escalation for the same case and reason exists.

        Returns the stored escalation and whether it was newly created.
        """
        ...

    async def get(self, escalation_id: str) -> Escalation | None: ...

    async def save(self, escalation: Escalation) -> None: ...

    async def list(
        self,
        *,
        queues: Sequence[str] | None = None,
        status: EscalationStatus | None = None,
        case_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Escalation]: ...

    async def count_open(self) -> dict[str, int]: ...


class PortalInbox(Protocol):
    async def deliver(self, message: PortalMessage) -> None: ...

    async def for_patient(self, patient_id: str, *, limit: int = 50) -> Sequence[PortalMessage]: ...
