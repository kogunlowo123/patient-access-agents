"""Patient access use cases: Referral -> Eligibility -> Scheduling -> Follow-up -> Escalation.

Every operation is authorized through the AccessController, every decision
and state change is written to the signed audit chain before the new state
is persisted (an audit failure fails the operation), and anything the agents
cannot or must not decide goes to a human queue as an escalation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision
from agentic_core.observability import correlation_id, get_logger
from pydantic import BaseModel, ConfigDict, Field

from patient_access_agents.application.eligibility import EligibilityVerifier
from patient_access_agents.application.messaging import (
    MessageComposer,
    MessageContext,
    Recipient,
)
from patient_access_agents.domain.errors import (
    AccessError,
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
)
from patient_access_agents.domain.models import (
    AccessCase,
    CaseStatus,
    CaseTransition,
    EligibilityOutcome,
    EligibilityResult,
    Escalation,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    PendingBooking,
    PortalMessage,
    Reminder,
    ReminderKind,
    ReminderStatus,
    SchedulingPreferences,
    SlotProposal,
)
from patient_access_agents.domain.ports import (
    CaseRepository,
    Clock,
    EscalationRepository,
    PortalInbox,
    ReminderRepository,
)
from patient_access_agents.domain.rules import AccessRules, ServiceProfile
from patient_access_agents.domain.scheduling import (
    Constraint,
    SchedulingRequest,
    SlotSolver,
    SolverResult,
)
from patient_access_agents.domain.timing import (
    Shift,
    next_retry,
    outside_quiet_hours,
    plan_appointment_reminders,
    sla_elapsed_fraction,
)
from patient_access_agents.integrations.fhir import (
    APPOINTMENT_IDENTIFIER,
    ORDER_INTENTS,
    FhirClient,
    appointment_ref,
    appointment_resource,
    demographics_from_patient,
    referral_from_service_request,
    slots_from_bundle,
)
from patient_access_agents.integrations.notifications import DeliveryError, Notifier
from patient_access_agents.observability.metrics import AccessMetrics
from patient_access_agents.security.access import AccessController, AccessDecision
from patient_access_agents.security.principals import (
    SYSTEM,
    Principal,
    PrincipalKind,
    StaffRole,
)

log = get_logger("patient_access_agents.service")

UNROUTED = "unrouted"
_PRIORITY_ORDER = list(EscalationPriority)


class BookingOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    case: AccessCase
    outcome: str = Field(description="booked or pending_approval")
    decision: Decision
    reason: str


class WorkSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligibility_retried: int = 0
    reminders_processed: int = 0
    escalations_raised: int = 0


def _bump(priority: EscalationPriority) -> EscalationPriority:
    index = _PRIORITY_ORDER.index(priority)
    return _PRIORITY_ORDER[min(index + 1, len(_PRIORITY_ORDER) - 1)]


def _case_attributes(case: AccessCase) -> dict[str, Any]:
    return {
        "patient_id": case.patient_id,
        "status": case.status.value,
        "prior_auth_pending": case.prior_auth_pending,
        "service": case.service,
    }


class PatientAccessService:
    def __init__(
        self,
        *,
        rules: AccessRules,
        cases: CaseRepository,
        reminders: ReminderRepository,
        escalations: EscalationRepository,
        inbox: PortalInbox,
        fhir: FhirClient,
        verifier: EligibilityVerifier,
        notifier: Notifier | None,
        composer: MessageComposer,
        access: AccessController,
        audit: AuditLog,
        clock: Clock,
        metrics: AccessMetrics,
        default_timezone: str = "America/New_York",
        worker_id: str = "worker",
    ) -> None:
        self.rules = rules
        self.cases = cases
        self.reminders = reminders
        self.escalations = escalations
        self.inbox = inbox
        self.fhir = fhir
        self.verifier = verifier
        self.notifier = notifier
        self.composer = composer
        self.access = access
        self._audit = audit
        self.clock = clock
        self.metrics = metrics
        self.solver = SlotSolver(rules.solver)
        self.default_timezone = default_timezone
        self.worker_id = worker_id

    # ------------------------------------------------------------- plumbing

    async def record(
        self,
        principal: Principal,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"actor_kind": principal.kind.value, **(details or {})}
        if principal.delegation_chain:
            payload["delegation_chain"] = list(principal.delegation_chain)
        await self._audit.record(
            actor=principal.subject,
            action=action,
            resource=resource,
            outcome=outcome,
            correlation_id=correlation_id(),
            details=payload,
        )

    async def _authorize(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str = "*",
        attributes: dict[str, Any] | None = None,
        conceal: bool = False,
    ) -> AccessDecision:
        try:
            decision = await self.access.require(
                principal,
                action,
                resource_type=resource_type,
                resource_id=resource_id,
                attributes=attributes,
                conceal=conceal,
            )
        except (ForbiddenError, NotFoundError) as exc:
            self.metrics.authz.labels(action, Decision.DENY.value).inc()
            await self.record(
                principal,
                f"authz.{action}",
                f"{resource_type}/{resource_id}",
                "denied",
                {"reason": exc.message},
            )
            raise
        self.metrics.authz.labels(action, decision.decision.value).inc()
        return decision

    async def _load(
        self, principal: Principal, case_id: str, action: str
    ) -> tuple[AccessCase, AccessDecision]:
        case = await self.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"access_case {case_id} not found")
        decision = await self._authorize(
            principal,
            action,
            resource_type="access_case",
            resource_id=case.id,
            attributes=_case_attributes(case),
            conceal=action == "case:read" or principal.kind is PrincipalKind.PATIENT,
        )
        return case, decision

    async def _save(self, case: AccessCase, previous: CaseStatus | None) -> AccessCase:
        stored = await self.cases.save(case)
        if previous is not stored.status:
            self.metrics.transitions.labels(stored.status.value).inc()
        return stored

    def _profile(self, case: AccessCase) -> ServiceProfile:
        if case.service == UNROUTED:
            raise InvalidStateError(
                f"case {case.id} is not routed to a bookable service; route it first"
            )
        return self.rules.service(case.service)

    # ---------------------------------------------------------- escalations

    async def escalate(
        self,
        case: AccessCase,
        reason: EscalationReason,
        summary: str,
        details: dict[str, Any] | None = None,
        *,
        actor: Principal = SYSTEM,
    ) -> tuple[Escalation, bool]:
        """Open an escalation unless one is already open for this case and reason."""
        base = self.rules.sla[case.referral.priority].escalation_priority
        priority = _bump(base) if reason is EscalationReason.SLA_BREACH else base
        now = self.clock.now()
        escalation, created = await self.escalations.open(
            Escalation(
                case_id=case.id,
                patient_id=case.patient_id,
                reason=reason,
                queue=self.rules.escalation_queues[reason],
                priority=priority,
                summary=summary,
                details=details or {},
                created_at=now,
                updated_at=now,
            )
        )
        if created:
            self.metrics.escalations.labels(reason.value, escalation.queue).inc()
            await self.record(
                actor,
                "escalation.open",
                f"escalation/{escalation.id}",
                "opened",
                {
                    "case_id": case.id,
                    "reason": reason.value,
                    "queue": escalation.queue,
                    "priority": priority.value,
                },
            )
        return escalation, created

    async def _resolve_open(
        self, case: AccessCase, reason: EscalationReason, actor: Principal, resolution: str
    ) -> None:
        for item in await self.escalations.list(case_id=case.id):
            if item.reason is reason and item.status is not EscalationStatus.RESOLVED:
                await self._finish(item, actor, resolution)

    async def _finish(self, item: Escalation, actor: Principal, resolution: str) -> Escalation:
        done = item.model_copy(
            update={
                "status": EscalationStatus.RESOLVED,
                "resolved_by": actor.subject,
                "resolution": resolution,
                "updated_at": self.clock.now(),
            }
        )
        await self.record(
            actor,
            "escalation.resolve",
            f"escalation/{item.id}",
            "resolved",
            {"case_id": item.case_id, "reason": item.reason.value},
        )
        await self.escalations.save(done)
        return done

    async def list_escalations(
        self,
        principal: Principal,
        *,
        queue: str | None = None,
        status: EscalationStatus | None = None,
        case_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Escalation]:
        await self._authorize(principal, "escalation:read", resource_type="escalation")
        return await self.escalations.list(
            queues=[queue] if queue else None,
            status=status,
            case_id=case_id,
            limit=limit,
            offset=offset,
        )

    async def _escalation(
        self, principal: Principal, escalation_id: str, action: str
    ) -> Escalation:
        item = await self.escalations.get(escalation_id)
        if item is None:
            raise NotFoundError(f"escalation {escalation_id} not found")
        await self._authorize(
            principal,
            action,
            resource_type="escalation",
            resource_id=item.id,
            attributes={"queue": item.queue, "patient_id": item.patient_id},
        )
        return item

    async def claim_escalation(self, principal: Principal, escalation_id: str) -> Escalation:
        item = await self._escalation(principal, escalation_id, "escalation:claim")
        if item.status is not EscalationStatus.OPEN:
            raise InvalidStateError(f"escalation {item.id} is {item.status.value}")
        claimed = item.model_copy(
            update={
                "status": EscalationStatus.CLAIMED,
                "claimed_by": principal.subject,
                "updated_at": self.clock.now(),
            }
        )
        await self.record(
            principal,
            "escalation.claim",
            f"escalation/{item.id}",
            "claimed",
            {"case_id": item.case_id, "queue": item.queue},
        )
        await self.escalations.save(claimed)
        return claimed

    async def resolve_escalation(
        self, principal: Principal, escalation_id: str, resolution: str
    ) -> Escalation:
        item = await self._escalation(principal, escalation_id, "escalation:resolve")
        if item.status is EscalationStatus.RESOLVED:
            raise InvalidStateError(f"escalation {item.id} is already resolved")
        if (
            item.status is EscalationStatus.CLAIMED
            and item.claimed_by != principal.subject
            and StaffRole.SUPERVISOR not in principal.roles
        ):
            raise ForbiddenError("escalation is claimed by someone else")
        return await self._finish(item, principal, resolution)

    async def refresh_queue_gauges(self) -> None:
        counts = await self.escalations.count_open()
        for queue in set(self.rules.queue_roles):
            self.metrics.open_escalations.labels(queue).set(counts.get(queue, 0))

    # --------------------------------------------------------------- intake

    async def intake(
        self, principal: Principal, service_request_id: str
    ) -> tuple[AccessCase, bool]:
        await self._authorize(principal, "case:create", resource_type="access_case")
        existing = await self.cases.by_service_request(service_request_id)
        if existing is not None:
            case, _ = await self._load(principal, existing.id, "case:read")
            return case, False
        referral = referral_from_service_request(
            await self.fhir.read("ServiceRequest", service_request_id)
        )
        if referral.status != "active":
            raise InvalidStateError(
                f"ServiceRequest/{service_request_id} is {referral.status}; "
                "only active referrals are accepted"
            )
        if referral.intent not in ORDER_INTENTS:
            raise InvalidStateError(
                f"ServiceRequest/{service_request_id} intent {referral.intent} is not an order"
            )
        patient = demographics_from_patient(await self.fhir.read("Patient", referral.patient_id))
        profile = self.rules.route(referral.codes)
        now = self.clock.now()
        sla = self.rules.sla[referral.priority]
        deadline = now + sla.appointment_within
        if referral.desired_end is not None and referral.desired_end > now:
            deadline = min(deadline, referral.desired_end)
        case = AccessCase(
            service_request_id=referral.service_request_id,
            patient_id=referral.patient_id,
            referral=referral,
            service=profile.name if profile else UNROUTED,
            sla_due_at=now + sla.schedule_within,
            appointment_deadline=deadline,
            preferences=SchedulingPreferences(
                timezone=self.default_timezone,
                language=patient.language,
            ),
            history=(
                CaseTransition(
                    at=now,
                    from_status=None,
                    to_status=CaseStatus.RECEIVED,
                    actor=principal.subject,
                    reason=f"referral ServiceRequest/{referral.service_request_id} received",
                ),
            ),
            created_at=now,
            updated_at=now,
        )
        await self.record(
            principal,
            "case.create",
            f"access_case/{case.id}",
            "created",
            {
                "service_request": f"ServiceRequest/{referral.service_request_id}",
                "patient": f"Patient/{referral.patient_id}",
                "priority": referral.priority.value,
                "service": case.service,
                "rules_version": self.rules.version,
            },
        )
        try:
            case = await self.cases.add(case)
        except ConflictError:
            raced = await self.cases.by_service_request(service_request_id)
            if raced is None:  # pragma: no cover - conflict implies the row exists
                raise
            return raced, False
        self.metrics.transitions.labels(CaseStatus.RECEIVED.value).inc()
        if profile is None:
            await self.escalate(
                case,
                EscalationReason.UNROUTABLE_REFERRAL,
                "Referral codes do not match any bookable service",
                {"codes": [c.token for c in referral.codes]},
            )
        return case, True

    async def route(self, principal: Principal, case_id: str, service: str) -> AccessCase:
        case, _ = await self._load(principal, case_id, "case:route")
        if case.status is not CaseStatus.RECEIVED:
            raise InvalidStateError("only cases that have not started eligibility can be routed")
        try:
            self.rules.service(service)
        except KeyError as exc:
            raise NotFoundError(f"service {service} is not configured") from exc
        updated = case.model_copy(update={"service": service, "updated_at": self.clock.now()})
        await self.record(
            principal,
            "case.route",
            f"access_case/{case.id}",
            "routed",
            {"from": case.service, "to": service},
        )
        stored = await self._save(updated, case.status)
        await self._resolve_open(
            stored, EscalationReason.UNROUTABLE_REFERRAL, principal, f"routed to {service}"
        )
        return stored

    async def get_case(self, principal: Principal, case_id: str) -> AccessCase:
        case, _ = await self._load(principal, case_id, "case:read")
        return case

    async def list_cases(
        self,
        principal: Principal,
        *,
        patient_id: str | None = None,
        status: CaseStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AccessCase]:
        if principal.kind is PrincipalKind.PATIENT:
            patient_id = principal.patient_id
        attributes = {"patient_id": patient_id} if patient_id else {}
        await self._authorize(
            principal, "case:read", resource_type="access_case", attributes=attributes
        )
        return await self.cases.list(
            patient_id=patient_id, status=status, limit=limit, offset=offset
        )

    async def update_preferences(
        self, principal: Principal, case_id: str, preferences: SchedulingPreferences
    ) -> AccessCase:
        case, _ = await self._load(principal, case_id, "case:preferences")
        if case.status in {CaseStatus.SCHEDULED, CaseStatus.CLOSED}:
            raise InvalidStateError(f"case {case.id} is {case.status.value}")
        updated = case.model_copy(
            update={"preferences": preferences, "updated_at": self.clock.now()}
        )
        await self.record(
            principal,
            "case.preferences",
            f"access_case/{case.id}",
            "updated",
            {
                "windows": len(preferences.availability),
                "modalities": sorted(m.value for m in preferences.modalities),
                "timezone": preferences.timezone,
            },
        )
        return await self._save(updated, case.status)

    async def close(self, principal: Principal, case_id: str, reason: str) -> AccessCase:
        case, _ = await self._load(principal, case_id, "case:close")
        closed = case.transition(
            CaseStatus.CLOSED,
            at=self.clock.now(),
            actor=principal.subject,
            reason=reason,
            closed_reason=reason,
            pending_booking=None,
        )
        await self.record(
            principal, "case.close", f"access_case/{case.id}", "closed", {"reason": reason}
        )
        stored = await self._save(closed, case.status)
        await self._cancel_reminders(stored, kinds=set(ReminderKind))
        return stored

    # ---------------------------------------------------------- eligibility

    async def verify_eligibility(self, principal: Principal, case_id: str) -> AccessCase:
        case, decision = await self._load(principal, case_id, "eligibility:verify")
        allowed = {
            CaseStatus.RECEIVED,
            CaseStatus.ELIGIBILITY_PENDING,
            CaseStatus.ELIGIBILITY_DENIED,
        }
        if case.status not in allowed:
            raise InvalidStateError(
                f"eligibility cannot be verified while the case is {case.status.value}"
            )
        profile = self._profile(case)
        now = self.clock.now()
        verification = await self.verifier.verify(
            case.referral,
            profile.x12_service_types,
            now=now,
            service_date=now.date(),
        )
        result = verification.result
        attempts = case.eligibility_attempts + 1
        self.metrics.eligibility.labels(result.outcome.value).inc()
        details = {
            "outcome": result.outcome.value,
            "trace_number": result.trace_number,
            "coverage": (
                f"Coverage/{verification.coverage_id}" if verification.coverage_id else None
            ),
            "rejection_code": result.rejection_code,
            "attempt": attempts,
            **decision.audit(),
        }
        await self.record(
            principal, "eligibility.verify", f"access_case/{case.id}", result.outcome.value, details
        )
        return await self._apply_eligibility(
            case, result, principal, attempts=attempts, retryable=verification.retryable
        )

    async def _apply_eligibility(
        self,
        case: AccessCase,
        result: EligibilityResult,
        actor: Principal,
        *,
        attempts: int,
        retryable: bool,
    ) -> AccessCase:
        now = self.clock.now()
        common: dict[str, Any] = {"eligibility": result, "eligibility_attempts": attempts}
        escalation: tuple[EscalationReason, str] | None = None
        match result.outcome:
            case EligibilityOutcome.ACTIVE:
                updated = case.transition(
                    CaseStatus.ELIGIBILITY_VERIFIED,
                    at=now,
                    actor=actor.subject,
                    reason="coverage active",
                    next_eligibility_attempt_at=None,
                    **common,
                )
                if result.prior_authorization_required:
                    escalation = (
                        EscalationReason.PRIOR_AUTHORIZATION,
                        "Payer indicates prior authorization is required for this service",
                    )
            case EligibilityOutcome.INACTIVE | EligibilityOutcome.REJECTED:
                reason = result.rejection_reason or result.outcome.value
                updated = case.transition(
                    CaseStatus.ELIGIBILITY_DENIED,
                    at=now,
                    actor=actor.subject,
                    reason=reason,
                    next_eligibility_attempt_at=None,
                    **common,
                )
                escalation = (
                    EscalationReason.ELIGIBILITY_DENIED,
                    f"Eligibility not confirmed: {reason}",
                )
            case EligibilityOutcome.MANUAL_REQUIRED:
                updated = case.transition(
                    CaseStatus.ELIGIBILITY_PENDING,
                    at=now,
                    actor=actor.subject,
                    reason=result.rejection_reason or "manual verification required",
                    next_eligibility_attempt_at=None,
                    **common,
                )
                escalation = (
                    EscalationReason.MANUAL_VERIFICATION,
                    result.rejection_reason or "Eligibility must be verified manually",
                )
            case EligibilityOutcome.UNAVAILABLE:
                budget = self.rules.eligibility.retry
                next_at = None
                if retryable and attempts < budget.max_attempts:
                    next_at = now + budget.delay(attempts)
                updated = case.transition(
                    CaseStatus.ELIGIBILITY_PENDING,
                    at=now,
                    actor=actor.subject,
                    reason=result.note or result.rejection_reason or "payer unavailable",
                    next_eligibility_attempt_at=next_at,
                    **common,
                )
                if next_at is None:
                    escalation = (
                        EscalationReason.ELIGIBILITY_UNAVAILABLE,
                        "Eligibility could not be obtained from the payer",
                    )
        stored = await self._save(updated, case.status)
        if escalation is not None:
            await self.escalate(
                stored,
                escalation[0],
                escalation[1],
                {
                    "outcome": result.outcome.value,
                    "rejection_code": result.rejection_code,
                    "follow_up_action": result.follow_up_action,
                    "attempts": attempts,
                },
                actor=actor,
            )
        if result.outcome is EligibilityOutcome.ACTIVE:
            for reason in (
                EscalationReason.ELIGIBILITY_DENIED,
                EscalationReason.ELIGIBILITY_UNAVAILABLE,
                EscalationReason.MANUAL_VERIFICATION,
            ):
                await self._resolve_open(stored, reason, actor, "coverage verified active")
        return stored

    async def record_manual_eligibility(
        self,
        principal: Principal,
        case_id: str,
        *,
        active: bool,
        note: str,
        payer_reference: str,
        prior_authorization_required: bool = False,
    ) -> AccessCase:
        case, decision = await self._load(principal, case_id, "eligibility:override")
        if case.status not in {
            CaseStatus.RECEIVED,
            CaseStatus.ELIGIBILITY_PENDING,
            CaseStatus.ELIGIBILITY_DENIED,
        }:
            raise InvalidStateError(f"case {case.id} is {case.status.value}")
        result = EligibilityResult(
            outcome=EligibilityOutcome.ACTIVE if active else EligibilityOutcome.INACTIVE,
            checked_at=self.clock.now(),
            source="manual",
            trace_number=payer_reference,
            service_types=self._profile(case).x12_service_types,
            prior_authorization_required=prior_authorization_required,
            rejection_reason=None if active else note,
            note=note,
            payer_id=case.eligibility.payer_id if case.eligibility else None,
            member_id=case.eligibility.member_id if case.eligibility else None,
        )
        await self.record(
            principal,
            "eligibility.manual",
            f"access_case/{case.id}",
            result.outcome.value,
            {"payer_reference": payer_reference, **decision.audit()},
        )
        return await self._apply_eligibility(
            case, result, principal, attempts=case.eligibility_attempts, retryable=False
        )

    # ----------------------------------------------------------- scheduling

    def _scheduling_request(self, case: AccessCase, profile: ServiceProfile) -> SchedulingRequest:
        now = self.clock.now()
        prefs = case.preferences
        earliest = now + timedelta(hours=profile.min_lead_hours)
        if case.referral.desired_start is not None and case.referral.desired_start > earliest:
            earliest = case.referral.desired_start
        latest = min(
            case.appointment_deadline,
            now + timedelta(days=self.rules.solver.search_horizon_days),
        )
        allowed = [m for m in profile.modalities if not prefs.modalities or m in prefs.modalities]
        return SchedulingRequest(
            duration=timedelta(minutes=profile.duration_minutes),
            earliest=earliest,
            latest=latest,
            modalities=frozenset(allowed or profile.modalities),
            practitioners=frozenset(case.referral.practitioner_ids),
            locations=frozenset(case.referral.location_ids),
            excluded_locations=frozenset(prefs.excluded_locations),
            availability=prefs.availability,
            timezone=prefs.timezone,
            preferred_practitioners=(
                *prefs.preferred_practitioners,
                *case.referral.practitioner_ids,
            ),
            preferred_locations=prefs.preferred_locations,
            preferred_modalities=tuple(allowed),
        )

    async def propose_slots(
        self, principal: Principal, case_id: str
    ) -> tuple[AccessCase, SolverResult]:
        case, decision = await self._load(principal, case_id, "slots:search")
        if case.status not in {
            CaseStatus.ELIGIBILITY_VERIFIED,
            CaseStatus.SLOTS_PROPOSED,
            CaseStatus.NO_CAPACITY,
        }:
            raise InvalidStateError(f"slots cannot be proposed while case is {case.status.value}")
        profile = self._profile(case)
        request = self._scheduling_request(case, profile)
        if request.latest <= request.earliest:
            result = SolverResult(
                options=(), considered=0, eliminated={}, binding_constraint=Constraint.DEADLINE
            )
        else:
            slots, included = await self.fhir.search(
                "Slot",
                {
                    "status": "free",
                    "specialty": profile.slot_specialty,
                    "start": [f"ge{_instant(request.earliest)}", f"le{_instant(request.latest)}"],
                    "_include": "Slot:schedule",
                    "_include:iterate": "Schedule:actor",
                    "_sort": "start",
                    "_count": "100",
                },
            )
            result = self.solver.solve(slots_from_bundle(slots, included), request)
        self.metrics.slot_options.observe(len(result.options))
        now = self.clock.now()
        await self.record(
            principal,
            "slots.propose",
            f"access_case/{case.id}",
            "proposed" if result.feasible else "infeasible",
            {
                "options": len(result.options),
                "considered": result.considered,
                "binding_constraint": result.binding_constraint,
                **decision.audit(),
            },
        )
        if result.feasible:
            updated = case.transition(
                CaseStatus.SLOTS_PROPOSED,
                at=now,
                actor=principal.subject,
                reason=f"{len(result.options)} options proposed",
                proposals=result.options,
            )
            stored = await self._save(updated, case.status)
            await self._resolve_open(
                stored, EscalationReason.NO_CAPACITY, principal, "appointment options found"
            )
            await self._resolve_open(
                stored,
                EscalationReason.AVAILABILITY_CONFLICT,
                principal,
                "appointment options found",
            )
            await self._schedule_outreach(stored)
            return stored, result
        updated = case.transition(
            CaseStatus.NO_CAPACITY,
            at=now,
            actor=principal.subject,
            reason=f"no feasible slot (binding constraint: {result.binding_constraint})",
            proposals=(),
        )
        stored = await self._save(updated, case.status)
        reason = (
            EscalationReason.AVAILABILITY_CONFLICT
            if result.binding_constraint is Constraint.AVAILABILITY
            else EscalationReason.NO_CAPACITY
        )
        await self.escalate(
            stored,
            reason,
            f"No appointment satisfies the constraints ({result.binding_constraint})",
            {
                "binding_constraint": result.binding_constraint,
                "eliminated": {k.value: v for k, v in result.eliminated.items() if v},
                "deadline": stored.appointment_deadline.isoformat(),
            },
            actor=principal,
        )
        return stored, result

    async def book(self, principal: Principal, case_id: str, proposal_id: str) -> BookingOutcome:
        case, decision = await self._load(principal, case_id, "appointment:book")
        if case.status is not CaseStatus.SLOTS_PROPOSED:
            raise InvalidStateError(f"case {case.id} has no open proposals ({case.status.value})")
        proposal = case.proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"proposal {proposal_id} not found on case {case.id}")
        if decision.decision is Decision.REQUIRE_APPROVAL:
            now = self.clock.now()
            pending = case.transition(
                CaseStatus.BOOKING_PENDING_APPROVAL,
                at=now,
                actor=principal.subject,
                reason=decision.reason,
                pending_booking=PendingBooking(
                    proposal_id=proposal.proposal_id,
                    requested_by=principal.subject,
                    requested_at=now,
                    reason=decision.reason,
                    matched_rules=decision.matched_rules,
                ),
            )
            await self.record(
                principal,
                "appointment.book",
                f"access_case/{case.id}",
                "pending_approval",
                {"proposal_id": proposal.proposal_id, **decision.audit()},
            )
            stored = await self._save(pending, case.status)
            await self.escalate(
                stored,
                EscalationReason.BOOKING_APPROVAL,
                "Booking requested; a person must approve it",
                {
                    "proposal_id": proposal.proposal_id,
                    "matched_rules": list(decision.matched_rules),
                },
                actor=principal,
            )
            self.metrics.bookings.labels("pending_approval").inc()
            return BookingOutcome(
                case=stored,
                outcome="pending_approval",
                decision=decision.decision,
                reason=decision.reason,
            )
        stored = await self._commit_booking(
            case, proposal, booked_by=principal, approved_by=None, decision=decision
        )
        return BookingOutcome(
            case=stored, outcome="booked", decision=decision.decision, reason=decision.reason
        )

    async def decide_booking(
        self, principal: Principal, case_id: str, *, approve: bool, note: str
    ) -> BookingOutcome:
        case, decision = await self._load(principal, case_id, "appointment:approve")
        pending = case.pending_booking
        if case.status is not CaseStatus.BOOKING_PENDING_APPROVAL or pending is None:
            raise InvalidStateError(f"case {case.id} has no booking awaiting approval")
        if pending.requested_by == principal.subject:
            raise ForbiddenError("a booking cannot be approved by the person who requested it")
        if case.prior_auth_pending and not principal.roles & {
            StaffRole.ELIGIBILITY,
            StaffRole.SUPERVISOR,
        }:
            raise ForbiddenError("prior authorization holds are cleared by eligibility staff")
        proposal = case.proposal(pending.proposal_id)
        if proposal is None:  # pragma: no cover - proposals are immutable while pending
            raise NotFoundError("pending proposal is missing")
        if not approve:
            reverted = case.transition(
                CaseStatus.SLOTS_PROPOSED,
                at=self.clock.now(),
                actor=principal.subject,
                reason=f"booking declined: {note}",
                pending_booking=None,
            )
            await self.record(
                principal,
                "appointment.decide",
                f"access_case/{case.id}",
                "declined",
                {"note": note, **decision.audit()},
            )
            stored = await self._save(reverted, case.status)
            await self._resolve_open(
                stored, EscalationReason.BOOKING_APPROVAL, principal, f"declined: {note}"
            )
            self.metrics.bookings.labels("declined").inc()
            return BookingOutcome(
                case=stored, outcome="declined", decision=Decision.DENY, reason=note
            )
        requester = Principal(subject=pending.requested_by, kind=PrincipalKind.AGENT)
        stored = await self._commit_booking(
            case, proposal, booked_by=requester, approved_by=principal, decision=decision
        )
        await self._resolve_open(
            stored, EscalationReason.BOOKING_APPROVAL, principal, f"approved: {note}"
        )
        await self._resolve_open(
            stored, EscalationReason.PRIOR_AUTHORIZATION, principal, f"approved: {note}"
        )
        return BookingOutcome(case=stored, outcome="booked", decision=Decision.ALLOW, reason=note)

    async def _commit_booking(
        self,
        case: AccessCase,
        proposal: SlotProposal,
        *,
        booked_by: Principal,
        approved_by: Principal | None,
        decision: AccessDecision,
    ) -> AccessCase:
        identifier = f"{APPOINTMENT_IDENTIFIER}|{case.id}"
        existing, _ = await self.fhir.search("Appointment", {"identifier": identifier})
        appointment = next((a for a in existing if a.get("status") == "booked"), None)
        if appointment is None:
            slots = [await self.fhir.read("Slot", sid) for sid in proposal.slot_ids]
            if any(s.get("status") != "free" for s in slots):
                self.metrics.bookings.labels("conflict").inc()
                raise ConflictError("the chosen slot is no longer free; search again")
            claimed: list[dict[str, Any]] = []
            try:
                for slot in slots:
                    version = (slot.get("meta") or {}).get("versionId")
                    claimed.append(
                        await self.fhir.update({**slot, "status": "busy"}, version_id=version)
                    )
                appointment = await self.fhir.create(
                    appointment_resource(
                        case_id=case.id,
                        referral=case.referral,
                        proposal=proposal,
                        service_display=self.rules.service(case.service).display,
                    ),
                    if_none_exist=f"identifier={identifier}",
                )
            except AccessError:
                await self._release_slots(claimed)
                self.metrics.bookings.labels("conflict").inc()
                raise
        now = self.clock.now()
        ref = appointment_ref(
            appointment,
            proposal,
            booked_by=booked_by.subject,
            approved_by=approved_by.subject if approved_by else None,
            booked_at=now,
        )
        actor = approved_by or booked_by
        scheduled = case.transition(
            CaseStatus.SCHEDULED,
            at=now,
            actor=actor.subject,
            reason=f"Appointment/{ref.appointment_id} booked",
            appointment=ref,
            pending_booking=None,
        )
        await self.record(
            actor,
            "appointment.book",
            f"access_case/{case.id}",
            "booked",
            {
                "appointment": f"Appointment/{ref.appointment_id}",
                "slots": [f"Slot/{s}" for s in proposal.slot_ids],
                "booked_by": booked_by.subject,
                "approved_by": approved_by.subject if approved_by else None,
                **decision.audit(),
            },
        )
        stored = await self._save(scheduled, case.status)
        self.metrics.bookings.labels("booked").inc()
        await self._cancel_reminders(stored, kinds={ReminderKind.SCHEDULING_OUTREACH})
        await self._plan_reminders(stored)
        for reason in (
            EscalationReason.SLA_AT_RISK,
            EscalationReason.SLA_BREACH,
            EscalationReason.NO_CAPACITY,
        ):
            await self._resolve_open(stored, reason, actor, "appointment booked")
        return stored

    async def _release_slots(self, claimed: list[dict[str, Any]]) -> None:
        for slot in claimed:
            try:
                await self.fhir.update(
                    {**slot, "status": "free"}, version_id=(slot.get("meta") or {}).get("versionId")
                )
            except AccessError:
                log.error("slot_release_failed", slot=slot.get("id"))

    # -------------------------------------------------------------- follow-up

    async def _schedule_outreach(self, case: AccessCase) -> None:
        existing = await self.reminders.for_case(case.id)
        if any(
            r.kind is ReminderKind.SCHEDULING_OUTREACH and r.status is ReminderStatus.PENDING
            for r in existing
        ):
            return
        now = self.clock.now()
        due = now + timedelta(minutes=self.rules.reminders.outreach_delay_minutes)
        due = outside_quiet_hours(
            due, self.rules.reminders.quiet_hours, ZoneInfo(case.preferences.timezone), Shift.LATER
        )
        await self.reminders.add(
            Reminder(
                case_id=case.id,
                patient_id=case.patient_id,
                kind=ReminderKind.SCHEDULING_OUTREACH,
                channel=case.preferences.reminder_channel,
                due_at=due,
                created_at=now,
            )
        )

    async def _plan_reminders(self, case: AccessCase) -> None:
        if case.appointment is None:  # pragma: no cover - only called after booking
            return
        now = self.clock.now()
        for due in plan_appointment_reminders(
            case.appointment.start, now, self.rules.reminders, ZoneInfo(case.preferences.timezone)
        ):
            await self.reminders.add(
                Reminder(
                    case_id=case.id,
                    patient_id=case.patient_id,
                    kind=ReminderKind.APPOINTMENT_REMINDER,
                    channel=case.preferences.reminder_channel,
                    due_at=due,
                    appointment_start=case.appointment.start,
                    created_at=now,
                )
            )

    async def _cancel_reminders(self, case: AccessCase, *, kinds: set[ReminderKind]) -> None:
        for reminder in await self.reminders.for_case(case.id):
            if reminder.kind in kinds and reminder.status is ReminderStatus.PENDING:
                await self.reminders.save(
                    reminder.model_copy(update={"status": ReminderStatus.CANCELLED})
                )

    async def reminders_for(self, principal: Principal, case_id: str) -> Sequence[Reminder]:
        case, _ = await self._load(principal, case_id, "case:read")
        return await self.reminders.for_case(case.id)

    async def acknowledge(self, principal: Principal, reminder_id: str) -> Reminder:
        reminder = await self.reminders.get(reminder_id)
        if reminder is None:
            raise NotFoundError(f"reminder {reminder_id} not found")
        await self._authorize(
            principal,
            "reminder:acknowledge",
            resource_type="reminder",
            resource_id=reminder.id,
            attributes={"patient_id": reminder.patient_id},
            conceal=True,
        )
        if reminder.status is not ReminderStatus.SENT:
            raise InvalidStateError(f"reminder {reminder.id} is {reminder.status.value}")
        acknowledged = reminder.model_copy(
            update={"status": ReminderStatus.ACKNOWLEDGED, "acknowledged_at": self.clock.now()}
        )
        await self.record(
            principal,
            "reminder.acknowledge",
            f"reminder/{reminder.id}",
            "acknowledged",
            {"case_id": reminder.case_id},
        )
        await self.reminders.save(acknowledged)
        return acknowledged

    async def messages(self, principal: Principal) -> Sequence[PortalMessage]:
        if principal.patient_id is None:
            raise ForbiddenError("the portal inbox belongs to a patient")
        await self._authorize(
            principal,
            "message:read",
            resource_type="message",
            attributes={"patient_id": principal.patient_id},
        )
        return await self.inbox.for_patient(principal.patient_id)

    async def _deliver(self, reminder: Reminder) -> None:
        case = await self.cases.get(reminder.case_id)
        relevant = case is not None and (
            (
                reminder.kind is ReminderKind.SCHEDULING_OUTREACH
                and case.status in {CaseStatus.SLOTS_PROPOSED, CaseStatus.NO_CAPACITY}
            )
            or (
                reminder.kind is ReminderKind.APPOINTMENT_REMINDER
                and case.status is CaseStatus.SCHEDULED
                and case.appointment is not None
                and case.appointment.start == reminder.appointment_start
            )
        )
        if case is None or not relevant:
            await self.reminders.save(
                reminder.model_copy(update={"status": ReminderStatus.CANCELLED})
            )
            return
        now = self.clock.now()
        try:
            patient = demographics_from_patient(await self.fhir.read("Patient", case.patient_id))
            profile = self._profile(case)
            modality = case.appointment.modality if case.appointment else profile.modalities[0]
            message = await self.composer.compose(
                MessageContext(
                    kind=reminder.kind,
                    service=profile.display,
                    modality=modality,
                    preparation=profile.preparation,
                    language=case.preferences.language,
                ),
                Recipient(
                    first_name=patient.given.split()[0],
                    appointment_start=case.appointment.start if case.appointment else None,
                    location_name=case.appointment.location_name if case.appointment else None,
                    timezone=case.preferences.timezone,
                ),
            )
            channel = reminder.channel
            recipient = {"sms": patient.phone, "email": patient.email}.get(channel)
            if channel == "portal" or self.notifier is None or not recipient:
                channel = "portal"
                await self.inbox.deliver(
                    PortalMessage(
                        patient_id=case.patient_id,
                        case_id=case.id,
                        reminder_id=reminder.id,
                        body=message.text,
                        created_at=now,
                    )
                )
            else:
                await self.notifier.send(
                    reminder_id=reminder.id,
                    channel=channel,
                    recipient=recipient,
                    body=message.text,
                )
        except (DeliveryError, DependencyUnavailableError) as exc:
            retryable = getattr(exc, "retryable", True)
            await self._delivery_failed(case, reminder, str(exc), retryable=retryable)
            return
        sent = reminder.model_copy(
            update={
                "status": ReminderStatus.SENT,
                "sent_at": now,
                "attempts": reminder.attempts + 1,
                "composer": message.composer,
                "channel": channel,
                "last_error": None,
            }
        )
        self.metrics.reminders.labels(channel, "sent").inc()
        await self.record(
            SYSTEM,
            "reminder.send",
            f"reminder/{reminder.id}",
            "sent",
            {
                "case_id": case.id,
                "channel": channel,
                "composer": message.composer,
                "fallback_reason": message.fallback_reason,
            },
        )
        await self.reminders.save(sent)

    async def _delivery_failed(
        self, case: AccessCase, reminder: Reminder, error: str, *, retryable: bool
    ) -> None:
        attempts = reminder.attempts + 1
        self.metrics.reminders.labels(reminder.channel, "failed").inc()
        next_at = (
            next_retry(
                self.clock.now(),
                attempts,
                self.rules.reminders,
                ZoneInfo(case.preferences.timezone),
                reminder.appointment_start,
            )
            if retryable
            else None
        )
        if next_at is not None:
            await self.reminders.save(
                reminder.model_copy(
                    update={"attempts": attempts, "due_at": next_at, "last_error": error[:300]}
                )
            )
            return
        failed = reminder.model_copy(
            update={
                "attempts": attempts,
                "status": ReminderStatus.FAILED,
                "last_error": error[:300],
            }
        )
        await self.record(
            SYSTEM,
            "reminder.send",
            f"reminder/{reminder.id}",
            "failed",
            {"case_id": case.id, "attempts": attempts, "error": error[:300]},
        )
        await self.reminders.save(failed)
        await self.escalate(
            case,
            EscalationReason.REMINDER_FAILED,
            f"Could not reach the patient by {reminder.channel} after {attempts} attempt(s)",
            {"reminder_id": reminder.id, "kind": reminder.kind.value},
        )

    # ------------------------------------------------------------------ SLAs

    def _pending_since(self, case: AccessCase) -> datetime | None:
        since = None
        for entry in case.history:
            if entry.to_status is CaseStatus.ELIGIBILITY_PENDING:
                if entry.from_status is not CaseStatus.ELIGIBILITY_PENDING:
                    since = entry.at
            else:
                since = None
        return since

    async def evaluate_slas(self) -> int:
        raised = 0
        now = self.clock.now()
        for case in await self.cases.open_cases():
            if case.status in {CaseStatus.SCHEDULED, CaseStatus.CLOSED}:
                continue  # pragma: no cover - open_cases excludes these
            fraction = sla_elapsed_fraction(case.created_at, case.sla_due_at, now)
            if now > case.sla_due_at:
                _, created = await self.escalate(
                    case,
                    EscalationReason.SLA_BREACH,
                    f"Referral not scheduled within the {case.referral.priority.value} SLA",
                    {"sla_due_at": case.sla_due_at.isoformat(), "status": case.status.value},
                )
                raised += int(created)
            elif fraction >= self.rules.sla_at_risk_fraction:
                _, created = await self.escalate(
                    case,
                    EscalationReason.SLA_AT_RISK,
                    f"{int(fraction * 100)}% of the scheduling SLA has elapsed",
                    {"sla_due_at": case.sla_due_at.isoformat(), "status": case.status.value},
                )
                raised += int(created)
            since = self._pending_since(case)
            if (
                case.status is CaseStatus.ELIGIBILITY_PENDING
                and since is not None
                and now - since > timedelta(hours=self.rules.eligibility.pending_max_hours)
            ):
                _, created = await self.escalate(
                    case,
                    EscalationReason.ELIGIBILITY_UNAVAILABLE,
                    "Eligibility has been pending longer than allowed",
                    {"pending_since": since.isoformat()},
                )
                raised += int(created)
        return raised

    # ----------------------------------------------------------- the workflow

    async def process_referral(self, principal: Principal, service_request_id: str) -> AccessCase:
        """Run the agentic flow as far as it can go without a person.

        Intake, eligibility and slot proposal run in sequence; the flow stops at
        the first step that needs a human (denial, manual verification, no
        capacity) because that step has already escalated.
        """
        case, _ = await self.intake(principal, service_request_id)
        if case.service == UNROUTED:
            return case
        if case.status in {CaseStatus.RECEIVED, CaseStatus.ELIGIBILITY_DENIED} or (
            case.status is CaseStatus.ELIGIBILITY_PENDING
            and case.next_eligibility_attempt_at is not None
        ):
            case = await self.verify_eligibility(principal, case.id)
        if case.status in {CaseStatus.ELIGIBILITY_VERIFIED, CaseStatus.NO_CAPACITY}:
            case, _ = await self.propose_slots(principal, case.id)
        return case

    async def run_due_work(self) -> WorkSummary:
        """One cycle of background work; each item fails independently."""
        retried = 0
        for case in await self.cases.eligibility_due(self.clock.now()):
            try:
                updated = await self.verify_eligibility(SYSTEM, case.id)
                retried += 1
                if updated.status is CaseStatus.ELIGIBILITY_VERIFIED:
                    await self.propose_slots(SYSTEM, updated.id)
            except AccessError as exc:
                log.warning("eligibility_retry_failed", case_id=case.id, error=exc.code)
        processed = 0
        for reminder in await self.reminders.claim_due(
            self.clock.now(), owner=self.worker_id, lease_seconds=120
        ):
            try:
                await self._deliver(reminder)
                processed += 1
            except AccessError as exc:
                log.warning("reminder_delivery_error", reminder_id=reminder.id, error=exc.code)
                await self.reminders.release(reminder.id)
        raised = await self.evaluate_slas()
        await self.refresh_queue_gauges()
        return WorkSummary(
            eligibility_retried=retried, reminders_processed=processed, escalations_raised=raised
        )


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
