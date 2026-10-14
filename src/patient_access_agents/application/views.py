"""Minimum-necessary projections of the aggregate, per caller.

The same case looks different to different callers (HIPAA minimum
necessary, 45 CFR 164.502(b)):

- the patient sees their whole case, including benefit details;
- eligibility specialists, supervisors and auditors see full eligibility
  detail including the member id;
- schedulers, navigators and agents see eligibility status but a masked
  member id and no benefit amounts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from patient_access_agents.domain.models import (
    AccessCase,
    AppointmentRef,
    Benefit,
    CaseStatus,
    CaseTransition,
    EligibilityOutcome,
    Escalation,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    PendingBooking,
    PortalMessage,
    Priority,
    Reminder,
    ReminderKind,
    ReminderStatus,
    SchedulingPreferences,
    SlotProposal,
)
from patient_access_agents.security.principals import Principal, PrincipalKind, StaffRole

_FULL_ELIGIBILITY = frozenset({StaffRole.ELIGIBILITY, StaffRole.SUPERVISOR, StaffRole.AUDITOR})


def mask(value: str | None) -> str | None:
    if value is None:
        return None
    return "*" * max(len(value) - 4, 0) + value[-4:]


def sees_full_eligibility(principal: Principal) -> bool:
    return principal.kind is PrincipalKind.PATIENT or bool(principal.roles & _FULL_ELIGIBILITY)


class EligibilityView(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: EligibilityOutcome
    checked_at: datetime
    source: str
    payer_id: str | None
    member_id: str | None
    prior_authorization_required: bool
    rejection_code: str | None
    rejection_reason: str | None
    follow_up_action: str | None
    plan_begin: str | None
    plan_end: str | None
    benefits: tuple[Benefit, ...]
    note: str | None


class CaseView(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    service_request_id: str
    patient_id: str
    service: str
    priority: Priority
    status: CaseStatus
    sla_due_at: datetime
    appointment_deadline: datetime
    preferences: SchedulingPreferences
    eligibility: EligibilityView | None
    eligibility_attempts: int
    next_eligibility_attempt_at: datetime | None
    proposals: tuple[SlotProposal, ...]
    pending_booking: PendingBooking | None
    appointment: AppointmentRef | None
    history: tuple[CaseTransition, ...]
    closed_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, case: AccessCase, principal: Principal) -> CaseView:
        eligibility = None
        if case.eligibility is not None:
            full = sees_full_eligibility(principal)
            e = case.eligibility
            eligibility = EligibilityView(
                outcome=e.outcome,
                checked_at=e.checked_at,
                source=e.source,
                payer_id=e.payer_id,
                member_id=e.member_id if full else mask(e.member_id),
                prior_authorization_required=e.prior_authorization_required,
                rejection_code=e.rejection_code,
                rejection_reason=e.rejection_reason,
                follow_up_action=e.follow_up_action,
                plan_begin=e.plan_begin,
                plan_end=e.plan_end,
                benefits=e.benefits if full else (),
                note=e.note if full else None,
            )
        return cls(
            case_id=case.id,
            service_request_id=case.service_request_id,
            patient_id=case.patient_id,
            service=case.service,
            priority=case.referral.priority,
            status=case.status,
            sla_due_at=case.sla_due_at,
            appointment_deadline=case.appointment_deadline,
            preferences=case.preferences,
            eligibility=eligibility,
            eligibility_attempts=case.eligibility_attempts,
            next_eligibility_attempt_at=case.next_eligibility_attempt_at,
            proposals=case.proposals,
            pending_booking=case.pending_booking,
            appointment=case.appointment,
            history=case.history,
            closed_reason=case.closed_reason,
            version=case.version,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )


class CaseList(BaseModel):
    items: list[CaseView]
    limit: int
    offset: int


class ReminderView(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str
    case_id: str
    kind: ReminderKind
    channel: str
    status: ReminderStatus
    due_at: datetime
    attempts: int
    sent_at: datetime | None
    acknowledged_at: datetime | None
    composer: str | None

    @classmethod
    def of(cls, reminder: Reminder) -> ReminderView:
        return cls(
            reminder_id=reminder.id,
            case_id=reminder.case_id,
            kind=reminder.kind,
            channel=reminder.channel,
            status=reminder.status,
            due_at=reminder.due_at,
            attempts=reminder.attempts,
            sent_at=reminder.sent_at,
            acknowledged_at=reminder.acknowledged_at,
            composer=reminder.composer,
        )


class EscalationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    escalation_id: str
    case_id: str
    patient_id: str
    reason: EscalationReason
    queue: str
    priority: EscalationPriority
    status: EscalationStatus
    summary: str
    details: dict[str, object]
    claimed_by: str | None
    resolved_by: str | None
    resolution: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, escalation: Escalation) -> EscalationView:
        return cls(
            escalation_id=escalation.id,
            case_id=escalation.case_id,
            patient_id=escalation.patient_id,
            reason=escalation.reason,
            queue=escalation.queue,
            priority=escalation.priority,
            status=escalation.status,
            summary=escalation.summary,
            details=escalation.details,
            claimed_by=escalation.claimed_by,
            resolved_by=escalation.resolved_by,
            resolution=escalation.resolution,
            created_at=escalation.created_at,
            updated_at=escalation.updated_at,
        )


class EscalationList(BaseModel):
    items: list[EscalationView]
    limit: int
    offset: int


class MessageView(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    case_id: str
    body: str
    created_at: datetime

    @classmethod
    def of(cls, message: PortalMessage) -> MessageView:
        return cls(
            message_id=message.id,
            case_id=message.case_id,
            body=message.body,
            created_at=message.created_at,
        )
