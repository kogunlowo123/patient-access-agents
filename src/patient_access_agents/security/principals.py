"""Who is calling: patients, access-centre staff, AI agents, and this service itself."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PrincipalKind(StrEnum):
    PATIENT = "patient"
    STAFF = "staff"
    AGENT = "agent"
    SERVICE = "service"


class StaffRole:
    SCHEDULER = "access-scheduler"
    ELIGIBILITY = "eligibility-specialist"
    NAVIGATOR = "care-navigator"
    SUPERVISOR = "access-supervisor"
    AUDITOR = "access-auditor"

    ALL = frozenset({SCHEDULER, ELIGIBILITY, NAVIGATOR, SUPERVISOR, AUDITOR})


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    kind: PrincipalKind
    roles: frozenset[str] = frozenset()
    patient_id: str | None = None
    scopes: frozenset[str] = frozenset()
    delegated: bool = False
    delegation_chain: tuple[str, ...] = ()
    credential: str | None = Field(default=None, exclude=True, repr=False)

    @property
    def is_human(self) -> bool:
        return self.kind in {PrincipalKind.PATIENT, PrincipalKind.STAFF}

    def in_scope(self, action: str) -> bool:
        """True when a scope grants ``action``; ``case:*`` grants every ``case:`` action."""
        for scope in self.scopes:
            if scope == action:
                return True
            if scope.endswith(":*") and action.startswith(scope[:-1]):
                return True
        return False


SYSTEM = Principal(subject="service:patient-access-agents", kind=PrincipalKind.SERVICE)
