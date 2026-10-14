"""Patient access aggregate, value objects and lifecycle state machines."""

from __future__ import annotations

import re
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentic_core.domain.identifiers import new_id
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patient_access_agents.domain.errors import InvalidStateError

_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def fhir_id(value: str) -> str:
    """Validate a FHIR logical id (R4: [A-Za-z0-9\\-\\.]{1,64})."""
    if not _FHIR_ID.fullmatch(value):
        raise ValueError(f"invalid FHIR id {value!r}")
    return value


_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


def valid_timezone(value: str) -> str:
    """Validate an IANA time zone name without touching the filesystem for junk input."""
    if not _TZ_NAME.fullmatch(value):
        raise ValueError(f"invalid IANA timezone {value!r}")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unknown IANA timezone {value!r}") from exc
    return value


class Priority(StrEnum):
    """FHIR R4 request-priority value set."""

    ROUTINE = "routine"
    URGENT = "urgent"
    ASAP = "asap"
    STAT = "stat"


class Modality(StrEnum):
    IN_PERSON = "in_person"
    TELEHEALTH = "telehealth"
    HOME = "home"


class CaseStatus(StrEnum):
    RECEIVED = "received"
    ELIGIBILITY_PENDING = "eligibility_pending"
    ELIGIBILITY_VERIFIED = "eligibility_verified"
    ELIGIBILITY_DENIED = "eligibility_denied"
    SLOTS_PROPOSED = "slots_proposed"
    NO_CAPACITY = "no_capacity"
    BOOKING_PENDING_APPROVAL = "booking_pending_approval"
    SCHEDULED = "scheduled"
    CLOSED = "closed"


TERMINAL = frozenset({CaseStatus.CLOSED})
SCHEDULED_STATES = frozenset({CaseStatus.SCHEDULED, CaseStatus.CLOSED})

_ALLOWED: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.RECEIVED: frozenset(
        {
            CaseStatus.ELIGIBILITY_PENDING,
            CaseStatus.ELIGIBILITY_VERIFIED,
            CaseStatus.ELIGIBILITY_DENIED,
        }
    ),
    CaseStatus.ELIGIBILITY_PENDING: frozenset(
        {
            CaseStatus.ELIGIBILITY_PENDING,
            CaseStatus.ELIGIBILITY_VERIFIED,
            CaseStatus.ELIGIBILITY_DENIED,
        }
    ),
    # A denial is never final on the agent's word: a human may correct the
    # coverage and re-verify, or record a manual verification.
    CaseStatus.ELIGIBILITY_DENIED: frozenset(
        {
            CaseStatus.ELIGIBILITY_PENDING,
            CaseStatus.ELIGIBILITY_VERIFIED,
            CaseStatus.ELIGIBILITY_DENIED,
        }
    ),
    CaseStatus.ELIGIBILITY_VERIFIED: frozenset(
        {CaseStatus.SLOTS_PROPOSED, CaseStatus.NO_CAPACITY, CaseStatus.ELIGIBILITY_VERIFIED}
    ),
    CaseStatus.SLOTS_PROPOSED: frozenset(
        {
            CaseStatus.SLOTS_PROPOSED,
            CaseStatus.NO_CAPACITY,
            CaseStatus.BOOKING_PENDING_APPROVAL,
            CaseStatus.SCHEDULED,
        }
    ),
    CaseStatus.NO_CAPACITY: frozenset({CaseStatus.SLOTS_PROPOSED, CaseStatus.NO_CAPACITY}),
    CaseStatus.BOOKING_PENDING_APPROVAL: frozenset(
        {CaseStatus.SCHEDULED, CaseStatus.SLOTS_PROPOSED}
    ),
    CaseStatus.SCHEDULED: frozenset(),
    CaseStatus.CLOSED: frozenset(),
}


class Coding(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str | None = None
    code: str
    display: str | None = None

    @property
    def token(self) -> str:
        return f"{self.system}|{self.code}" if self.system else self.code


class Referral(BaseModel):
    """What the agents need from a FHIR ServiceRequest (no free-text clinical notes)."""

    model_config = ConfigDict(frozen=True)

    service_request_id: str
    patient_id: str
    status: str
    intent: str
    priority: Priority
    codes: tuple[Coding, ...]
    requester: str | None = None
    performers: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    authored_on: datetime | None = None
    desired_start: datetime | None = None
    desired_end: datetime | None = None
    insurance: tuple[str, ...] = ()

    @property
    def practitioner_ids(self) -> tuple[str, ...]:
        return tuple(p.split("/", 1)[1] for p in self.performers if p.startswith("Practitioner/"))

    @property
    def location_ids(self) -> tuple[str, ...]:
        return tuple(loc.split("/", 1)[1] for loc in self.locations)


class PatientDemographics(BaseModel):
    """Identifying data needed for eligibility; held only in memory, never persisted."""

    model_config = ConfigDict(frozen=True)

    patient_id: str
    family: str
    given: str
    birth_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    gender: str = "unknown"
    phone: str | None = None
    email: str | None = None
    language: str = "en"


class CoverageInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    coverage_id: str
    status: str
    member_id: str
    payer_id: str
    payer_name: str
    relationship: str = "self"
    group_number: str | None = None
    subscriber: PatientDemographics | None = None


class EligibilityOutcome(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    MANUAL_REQUIRED = "manual_required"


class Benefit(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    description: str
    coverage_level: str | None = None
    service_types: tuple[str, ...] = ()
    insurance_type: str | None = None
    plan_description: str | None = None
    time_period: str | None = None
    amount: str | None = None
    percent: str | None = None
    authorization_required: bool | None = None
    in_network: bool | None = None
    messages: tuple[str, ...] = ()


class EligibilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: EligibilityOutcome
    checked_at: datetime
    payer_id: str | None = None
    member_id: str | None = None
    trace_number: str | None = None
    service_types: tuple[str, ...] = ()
    benefits: tuple[Benefit, ...] = ()
    rejection_code: str | None = None
    rejection_reason: str | None = None
    follow_up_action: str | None = None
    prior_authorization_required: bool = False
    plan_begin: str | None = None
    plan_end: str | None = None
    source: str = Field(default="x12-271", description="x12-271, manual, or unavailable")
    note: str | None = None


class AvailabilityWindow(BaseModel):
    """A weekly window in the patient's local time when they can attend."""

    model_config = ConfigDict(frozen=True)

    weekdays: frozenset[int] = Field(description="ISO weekdays 1=Mon..7=Sun", min_length=1)
    start: time
    end: time

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: frozenset[int]) -> frozenset[int]:
        if not value <= frozenset(range(1, 8)):
            raise ValueError("weekdays must be ISO weekday numbers 1..7")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self


class SchedulingPreferences(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str = Field(default="America/New_York", max_length=64)
    availability: tuple[AvailabilityWindow, ...] = Field(default=(), max_length=21)
    modalities: frozenset[Modality] = frozenset()
    preferred_practitioners: tuple[str, ...] = Field(default=(), max_length=10)
    preferred_locations: tuple[str, ...] = Field(default=(), max_length=10)
    excluded_locations: tuple[str, ...] = Field(default=(), max_length=10)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    reminder_channel: str = Field(default="portal", pattern=r"^(portal|sms|email)$")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        return valid_timezone(value)

    @field_validator("preferred_practitioners", "preferred_locations", "excluded_locations")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(fhir_id(v) for v in value)


class SlotCandidate(BaseModel):
    """A free FHIR Slot with the Schedule facts the solver needs."""

    model_config = ConfigDict(frozen=True)

    slot_id: str
    schedule_id: str
    start: datetime
    end: datetime
    status: str = "free"
    practitioner_ids: tuple[str, ...] = ()
    location_id: str | None = None
    location_name: str | None = None
    practitioner_names: tuple[str, ...] = ()
    modality: Modality = Modality.IN_PERSON
    version_id: str | None = None


class SlotProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(default_factory=new_id)
    slot_ids: tuple[str, ...]
    schedule_id: str
    start: datetime
    end: datetime
    practitioner_ids: tuple[str, ...]
    location_id: str | None
    location_name: str | None = None
    practitioner_names: tuple[str, ...] = ()
    modality: Modality
    score: float
    score_breakdown: dict[str, float]


class PendingBooking(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    requested_by: str
    requested_at: datetime
    reason: str
    matched_rules: tuple[str, ...] = ()


class AppointmentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    slot_ids: tuple[str, ...]
    start: datetime
    end: datetime
    practitioner_ids: tuple[str, ...]
    location_id: str | None
    location_name: str | None = None
    modality: Modality
    booked_by: str
    approved_by: str | None = None
    booked_at: datetime


class CaseTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    from_status: CaseStatus | None
    to_status: CaseStatus
    actor: str
    reason: str


class AccessCase(BaseModel):
    """Aggregate root: one referral's journey from receipt to a booked appointment."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    service_request_id: str
    patient_id: str
    referral: Referral
    service: str
    status: CaseStatus = CaseStatus.RECEIVED
    sla_due_at: datetime
    appointment_deadline: datetime
    preferences: SchedulingPreferences = SchedulingPreferences()
    eligibility: EligibilityResult | None = None
    eligibility_attempts: int = 0
    next_eligibility_attempt_at: datetime | None = None
    proposals: tuple[SlotProposal, ...] = ()
    pending_booking: PendingBooking | None = None
    appointment: AppointmentRef | None = None
    history: tuple[CaseTransition, ...] = ()
    closed_reason: str | None = None
    version: int = 0
    created_at: datetime
    updated_at: datetime

    def transition(
        self, target: CaseStatus, *, at: datetime, actor: str, reason: str, **changes: Any
    ) -> AccessCase:
        if self.status in TERMINAL:
            raise InvalidStateError(f"case {self.id} is {self.status.value}")
        if target is not CaseStatus.CLOSED and target not in _ALLOWED[self.status]:
            raise InvalidStateError(
                f"case {self.id}: cannot move from {self.status.value} to {target.value}"
            )
        entry = CaseTransition(
            at=at, from_status=self.status, to_status=target, actor=actor, reason=reason
        )
        return self.model_copy(
            update={
                "status": target,
                "history": (*self.history, entry),
                "updated_at": at,
                **changes,
            }
        )

    def proposal(self, proposal_id: str) -> SlotProposal | None:
        return next((p for p in self.proposals if p.proposal_id == proposal_id), None)

    @property
    def prior_auth_pending(self) -> bool:
        return bool(self.eligibility and self.eligibility.prior_authorization_required)


class ReminderKind(StrEnum):
    SCHEDULING_OUTREACH = "scheduling_outreach"
    APPOINTMENT_REMINDER = "appointment_reminder"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Reminder(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    case_id: str
    patient_id: str
    kind: ReminderKind
    channel: str
    due_at: datetime
    appointment_start: datetime | None = None
    status: ReminderStatus = ReminderStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    composer: str | None = None
    created_at: datetime


class EscalationReason(StrEnum):
    ELIGIBILITY_DENIED = "eligibility_denied"
    ELIGIBILITY_UNAVAILABLE = "eligibility_unavailable"
    MANUAL_VERIFICATION = "manual_verification"
    PRIOR_AUTHORIZATION = "prior_authorization"
    NO_CAPACITY = "no_capacity"
    AVAILABILITY_CONFLICT = "availability_conflict"
    UNROUTABLE_REFERRAL = "unroutable_referral"
    SLA_AT_RISK = "sla_at_risk"
    SLA_BREACH = "sla_breach"
    REMINDER_FAILED = "reminder_failed"
    BOOKING_APPROVAL = "booking_approval"


class EscalationStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"


class EscalationPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Escalation(BaseModel):
    """An item in a human work queue. Agents raise them; only people resolve them."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    case_id: str
    patient_id: str
    reason: EscalationReason
    queue: str
    priority: EscalationPriority
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: EscalationStatus = EscalationStatus.OPEN
    claimed_by: str | None = None
    resolved_by: str | None = None
    resolution: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def dedupe_key(self) -> str:
        if self.status is EscalationStatus.RESOLVED:
            return f"{self.case_id}:{self.reason.value}:{self.id}"
        return f"{self.case_id}:{self.reason.value}"


class PortalMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    patient_id: str
    case_id: str
    reminder_id: str
    body: str
    created_at: datetime
