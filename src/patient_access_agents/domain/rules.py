"""Operational rules owned by patient-access operations, loaded from YAML.

SLAs, reminder cadence, retry budgets, routing of referrals to bookable
services and of escalations to human queues are configuration, reviewed in
Git like policy, not constants in code.
"""

from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from patient_access_agents.domain.models import (
    Coding,
    EscalationPriority,
    EscalationReason,
    Modality,
    Priority,
)


class SlaRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_within_hours: float = Field(gt=0, description="Referral receipt to booked")
    appointment_within_days: float = Field(gt=0, description="Latest acceptable visit date")
    escalation_priority: EscalationPriority = EscalationPriority.NORMAL

    @property
    def schedule_within(self) -> timedelta:
        return timedelta(hours=self.schedule_within_hours)

    @property
    def appointment_within(self) -> timedelta:
        return timedelta(days=self.appointment_within_days)


class Backoff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(ge=1, le=20)
    base_minutes: float = Field(gt=0)
    max_minutes: float = Field(gt=0)

    def delay(self, attempt: int) -> timedelta:
        """Delay after failed attempt number ``attempt`` (1-based), capped exponential."""
        minutes = min(self.max_minutes, self.base_minutes * (2 ** max(attempt - 1, 0)))
        return timedelta(minutes=minutes)


class EligibilityRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retry: Backoff
    pending_max_hours: float = Field(gt=0)


class QuietHours(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: time
    end: time

    def contains(self, local: time) -> bool:
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


class ReminderRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    appointment_offsets_hours: tuple[float, ...] = Field(min_length=1)
    min_notice_hours: float = Field(ge=0)
    retry: Backoff
    quiet_hours: QuietHours
    outreach_delay_minutes: float = Field(ge=0)


class SolverWeights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    earliness: float = Field(ge=0)
    continuity: float = Field(ge=0)
    location: float = Field(ge=0)
    modality: float = Field(ge=0)

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.earliness + self.continuity + self.location + self.modality <= 0:
            raise ValueError("at least one solver weight must be positive")
        return self


class SolverRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_options: int = Field(ge=1, le=20)
    max_per_practitioner_day: int = Field(ge=1, le=20)
    search_horizon_days: float = Field(gt=0, le=365)
    weights: SolverWeights


class ServiceProfile(BaseModel):
    """A bookable service a referral can be routed to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")
    display: str
    codes: tuple[str, ...] = Field(min_length=1, description="system|code tokens that route here")
    slot_specialty: str = Field(description="Slot search token: system|code")
    x12_service_types: tuple[str, ...] = Field(min_length=1, max_length=99)
    duration_minutes: int = Field(ge=5, le=480)
    min_lead_hours: float = Field(ge=0)
    modalities: tuple[Modality, ...] = Field(min_length=1, description="In order of preference")
    preparation: str = ""

    def matches(self, codings: tuple[Coding, ...]) -> bool:
        tokens = {c.token for c in codings}
        return bool(tokens.intersection(self.codes))


class AccessRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    sla: dict[Priority, SlaRule]
    sla_at_risk_fraction: float = Field(gt=0, lt=1)
    eligibility: EligibilityRules
    reminders: ReminderRules
    solver: SolverRules
    escalation_queues: dict[EscalationReason, str]
    queue_roles: dict[str, str]
    services: tuple[ServiceProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        missing_sla = set(Priority) - set(self.sla)
        if missing_sla:
            raise ValueError(f"sla missing priorities: {sorted(p.value for p in missing_sla)}")
        missing_queue = set(EscalationReason) - set(self.escalation_queues)
        if missing_queue:
            raise ValueError(
                f"escalation_queues missing reasons: {sorted(r.value for r in missing_queue)}"
            )
        unknown = set(self.escalation_queues.values()) - set(self.queue_roles)
        if unknown:
            raise ValueError(f"queues without an owning role: {sorted(unknown)}")
        names = [s.name for s in self.services]
        if len(set(names)) != len(names):
            raise ValueError("service names must be unique")
        return self

    def route(self, codings: tuple[Coding, ...]) -> ServiceProfile | None:
        return next((s for s in self.services if s.matches(codings)), None)

    def service(self, name: str) -> ServiceProfile:
        for profile in self.services:
            if profile.name == name:
                return profile
        raise KeyError(name)

    @classmethod
    def from_yaml(cls, text: str) -> AccessRules:
        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def from_file(cls, path: Path) -> AccessRules:
        return cls.from_yaml(path.read_text(encoding="utf-8"))
