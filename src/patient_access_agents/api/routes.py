"""HTTP routes.

Every ``/v1`` route requires a bearer token: an OIDC token for patients and
staff, or an agent-identity-fabric credential for agents. Authorization and
audit happen in the application service, so the same rules apply to REST,
MCP and A2A callers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from patient_access_agents.api.schemas import (
    FHIR_ID,
    OPAQUE_ID,
    BookingDecisionRequest,
    BookingRequest,
    BookingView,
    CloseRequest,
    IntakeRequest,
    ManualEligibilityRequest,
    MessageList,
    ReminderList,
    ResolveRequest,
    RouteRequest,
    SlotSearchView,
)
from patient_access_agents.application.service import BookingOutcome
from patient_access_agents.application.views import (
    CaseList,
    CaseView,
    EscalationList,
    EscalationView,
    MessageView,
    ReminderView,
)
from patient_access_agents.domain.models import CaseStatus, EscalationStatus, SchedulingPreferences
from patient_access_agents.runtime.container import Container
from patient_access_agents.security.principals import Principal

router = APIRouter()


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    return await container(request).authn.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
Caller = Annotated[Principal, Depends(principal)]
CaseId = Annotated[str, Path(pattern=OPAQUE_ID)]


def _booking(outcome: BookingOutcome, who: Principal, response: Response) -> BookingView:
    if outcome.outcome == "pending_approval":
        response.status_code = 202
    return BookingView(
        outcome=outcome.outcome,
        decision=outcome.decision,
        reason=outcome.reason,
        case=CaseView.of(outcome.case, who),
    )


# ------------------------------------------------------------------ referrals


@router.post("/v1/referrals", status_code=201, tags=["referrals"])
async def intake(body: IntakeRequest, c: Deps, who: Caller, response: Response) -> CaseView:
    """Accept a FHIR ServiceRequest as a patient access case (idempotent per ServiceRequest)."""
    case, created = await c.service.intake(who, body.service_request_id)
    if not created:
        response.status_code = 200
    return CaseView.of(case, who)


@router.post("/v1/referrals/process", tags=["referrals"])
async def process(body: IntakeRequest, c: Deps, who: Caller) -> CaseView:
    """Run intake, eligibility and slot proposal until a person is needed."""
    return CaseView.of(await c.service.process_referral(who, body.service_request_id), who)


# ---------------------------------------------------------------------- cases


@router.get("/v1/cases", tags=["cases"])
async def list_cases(
    *,
    c: Deps,
    who: Caller,
    patient_id: Annotated[str | None, Query(pattern=FHIR_ID)] = None,
    status: CaseStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> CaseList:
    cases = await c.service.list_cases(
        who, patient_id=patient_id, status=status, limit=limit, offset=offset
    )
    return CaseList(items=[CaseView.of(x, who) for x in cases], limit=limit, offset=offset)


@router.get("/v1/cases/{case_id}", tags=["cases"])
async def get_case(case_id: CaseId, c: Deps, who: Caller) -> CaseView:
    return CaseView.of(await c.service.get_case(who, case_id), who)


@router.post("/v1/cases/{case_id}/route", tags=["cases"])
async def route_case(case_id: CaseId, body: RouteRequest, c: Deps, who: Caller) -> CaseView:
    return CaseView.of(await c.service.route(who, case_id, body.service), who)


@router.put("/v1/cases/{case_id}/preferences", tags=["cases"])
async def preferences(
    case_id: CaseId, body: SchedulingPreferences, c: Deps, who: Caller
) -> CaseView:
    return CaseView.of(await c.service.update_preferences(who, case_id, body), who)


@router.post("/v1/cases/{case_id}/close", tags=["cases"])
async def close_case(case_id: CaseId, body: CloseRequest, c: Deps, who: Caller) -> CaseView:
    return CaseView.of(await c.service.close(who, case_id, body.reason), who)


# ---------------------------------------------------------------- eligibility


@router.post("/v1/cases/{case_id}/eligibility", tags=["eligibility"])
async def verify_eligibility(case_id: CaseId, c: Deps, who: Caller) -> CaseView:
    """Real-time X12 270/271 eligibility inquiry for the case's service."""
    return CaseView.of(await c.service.verify_eligibility(who, case_id), who)


@router.post("/v1/cases/{case_id}/eligibility/manual", tags=["eligibility"])
async def manual_eligibility(
    case_id: CaseId, body: ManualEligibilityRequest, c: Deps, who: Caller
) -> CaseView:
    """Record a verification made directly with the payer (eligibility staff only)."""
    case = await c.service.record_manual_eligibility(
        who,
        case_id,
        active=body.active,
        note=body.note,
        payer_reference=body.payer_reference,
        prior_authorization_required=body.prior_authorization_required,
    )
    return CaseView.of(case, who)


# ----------------------------------------------------------------- scheduling


@router.post("/v1/cases/{case_id}/slot-search", tags=["scheduling"])
async def slot_search(case_id: CaseId, c: Deps, who: Caller) -> SlotSearchView:
    """Search FHIR Slots and rank options with the constraint solver."""
    case, result = await c.service.propose_slots(who, case_id)
    return SlotSearchView(
        case=CaseView.of(case, who),
        feasible=result.feasible,
        considered=result.considered,
        eliminated=result.eliminated,
        binding_constraint=result.binding_constraint,
    )


@router.post("/v1/cases/{case_id}/booking", tags=["scheduling"])
async def book(
    case_id: CaseId, body: BookingRequest, c: Deps, who: Caller, response: Response
) -> BookingView:
    """Book a proposed option; returns 202 when a person must approve it first."""
    outcome = await c.service.book(who, case_id, body.proposal_id)
    return _booking(outcome, who, response)


@router.post("/v1/cases/{case_id}/booking/decision", tags=["scheduling"])
async def decide_booking(
    case_id: CaseId, body: BookingDecisionRequest, c: Deps, who: Caller, response: Response
) -> BookingView:
    outcome = await c.service.decide_booking(who, case_id, approve=body.approve, note=body.note)
    return _booking(outcome, who, response)


# ------------------------------------------------------------------ follow-up


@router.get("/v1/cases/{case_id}/reminders", tags=["follow-up"])
async def case_reminders(case_id: CaseId, c: Deps, who: Caller) -> ReminderList:
    reminders = await c.service.reminders_for(who, case_id)
    return ReminderList(items=[ReminderView.of(r) for r in reminders])


@router.post("/v1/reminders/{reminder_id}/acknowledge", tags=["follow-up"])
async def acknowledge(
    reminder_id: Annotated[str, Path(pattern=OPAQUE_ID)], c: Deps, who: Caller
) -> ReminderView:
    return ReminderView.of(await c.service.acknowledge(who, reminder_id))


@router.get("/v1/messages", tags=["follow-up"])
async def messages(c: Deps, who: Caller) -> MessageList:
    """The calling patient's portal inbox."""
    return MessageList(items=[MessageView.of(m) for m in await c.service.messages(who)])


# ---------------------------------------------------------------- escalations


@router.get("/v1/escalations", tags=["escalations"])
async def list_escalations(
    *,
    c: Deps,
    who: Caller,
    queue: Annotated[str | None, Query(pattern=r"^[a-z\-]{1,64}$")] = None,
    status: EscalationStatus | None = None,
    case_id: Annotated[str | None, Query(pattern=OPAQUE_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> EscalationList:
    items = await c.service.list_escalations(
        who, queue=queue, status=status, case_id=case_id, limit=limit, offset=offset
    )
    return EscalationList(items=[EscalationView.of(e) for e in items], limit=limit, offset=offset)


@router.post("/v1/escalations/{escalation_id}/claim", tags=["escalations"])
async def claim(
    escalation_id: Annotated[str, Path(pattern=OPAQUE_ID)], c: Deps, who: Caller
) -> EscalationView:
    return EscalationView.of(await c.service.claim_escalation(who, escalation_id))


@router.post("/v1/escalations/{escalation_id}/resolve", tags=["escalations"])
async def resolve(
    escalation_id: Annotated[str, Path(pattern=OPAQUE_ID)],
    body: ResolveRequest,
    c: Deps,
    who: Caller,
) -> EscalationView:
    return EscalationView.of(
        await c.service.resolve_escalation(who, escalation_id, body.resolution)
    )


# ----------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
