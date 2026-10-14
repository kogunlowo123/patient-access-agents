"""A2A (Agent2Agent) interface: Agent Card and JSON-RPC ``message/send``.

A calling agent sends a Message whose parts carry either a DataPart
``{"skill": "process-referral", "service_request_id": "..."}`` /
``{"skill": "case-status", "case_id": "..."}`` or a TextPart naming
``ServiceRequest/<id>``. The request is routed to the same workflow as the
REST and MCP interfaces and answered synchronously with a Task whose state
says whether the referral completed, needs a person (``input-required``), is
still retrying (``working``), or was refused.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from agentic_core.domain.identifiers import new_id
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from patient_access_agents import __version__
from patient_access_agents.application.views import CaseView
from patient_access_agents.domain.errors import AccessError, ForbiddenError, InvalidStateError
from patient_access_agents.domain.models import CaseStatus
from patient_access_agents.runtime.container import Container
from patient_access_agents.security.principals import Principal

router = APIRouter()

PROTOCOL_VERSION = "0.3.0"
_SERVICE_REQUEST = re.compile(r"ServiceRequest/([A-Za-z0-9\-.]{1,64})")
_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_CASE_ID = re.compile(r"^[A-Za-z0-9\-]{1,64}$")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_STATE = {
    CaseStatus.SCHEDULED: "completed",
    CaseStatus.CLOSED: "completed",
    CaseStatus.SLOTS_PROPOSED: "input-required",
    CaseStatus.BOOKING_PENDING_APPROVAL: "input-required",
    CaseStatus.ELIGIBILITY_DENIED: "input-required",
    CaseStatus.NO_CAPACITY: "input-required",
    CaseStatus.RECEIVED: "input-required",
    CaseStatus.ELIGIBILITY_VERIFIED: "working",
}


def agent_card(base_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Patient Access Agents",
        "description": (
            "Takes FHIR ServiceRequest referrals through insurance eligibility (X12 270/271), "
            "constraint-based appointment scheduling, follow-up reminders and escalation to "
            "human queues. Bookings requested by agents require human approval."
        ),
        "url": f"{base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "provider": {
            "organization": "kogunlowo123",
            "url": "https://github.com/kogunlowo123/patient-access-agents",
        },
        "documentationUrl": "https://github.com/kogunlowo123/patient-access-agents",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "agent-identity-fabric credential (or OIDC token for people)",
            }
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "process-referral",
                "name": "Process referral",
                "description": (
                    "Intake a FHIR ServiceRequest, verify eligibility and propose appointment "
                    "options. Stops and escalates where a person must decide."
                ),
                "tags": ["referral", "eligibility", "scheduling", "fhir", "x12"],
                "examples": [
                    '{"skill": "process-referral", "service_request_id": "<ServiceRequest id>"}'
                ],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "case-status",
                "name": "Case status",
                "description": "Current status of a patient access case.",
                "tags": ["status"],
                "examples": ['{"skill": "case-status", "case_id": "<case id>"}'],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def card(request: Request) -> dict[str, Any]:
    return agent_card(container(request).settings.public_url_str)


def _error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
    )


def _intent(message: dict[str, Any]) -> tuple[str, str] | None:
    """Extract (skill, argument) from the message parts."""
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            data = part["data"]
            skill = data.get("skill", "process-referral")
            argument = data.get("service_request_id" if skill == "process-referral" else "case_id")
            pattern = _FHIR_ID if skill == "process-referral" else _CASE_ID
            if (
                skill in {"process-referral", "case-status"}
                and isinstance(argument, str)
                and pattern.fullmatch(argument)
            ):
                return skill, argument
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            match = _SERVICE_REQUEST.search(part["text"])
            if match:
                return "process-referral", match.group(1)
    return None


def _task(message: dict[str, Any], state: str, text: str, case: CaseView | None) -> dict[str, Any]:
    context_id = message.get("contextId")
    task: dict[str, Any] = {
        "kind": "task",
        "id": new_id(),
        "contextId": context_id if isinstance(context_id, str) else new_id(),
        "status": {
            "state": state,
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {
                "kind": "message",
                "messageId": new_id(),
                "role": "agent",
                "parts": [{"kind": "text", "text": text}],
            },
        },
        "history": [message],
        "artifacts": [],
    }
    if case is not None:
        task["artifacts"].append(
            {
                "artifactId": new_id(),
                "name": "access-case",
                "parts": [{"kind": "data", "data": case.model_dump(mode="json")}],
            }
        )
    return task


def _summary(case: CaseView) -> tuple[str, str]:
    if case.status is CaseStatus.ELIGIBILITY_PENDING:
        if case.next_eligibility_attempt_at is not None:
            return (
                "working",
                f"Case {case.case_id}: payer unavailable, eligibility will be retried.",
            )
        return "input-required", f"Case {case.case_id}: eligibility needs manual verification."
    state = _STATE.get(case.status, "working")
    text = f"Case {case.case_id} is {case.status.value}."
    if case.status is CaseStatus.SLOTS_PROPOSED:
        text += f" {len(case.proposals)} appointment options await the patient's choice."
    elif state == "input-required":
        text += " A person has been asked to act on it."
    return state, text


class _RpcError(Exception):
    def __init__(self, rpc_id: Any, code: int, message: str) -> None:
        super().__init__(message)
        self.rpc_id = rpc_id
        self.code = code
        self.message = message


def _parse(body: Any) -> tuple[Any, dict[str, Any], str, str]:
    """Validate the JSON-RPC envelope; return (id, message, skill, argument)."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        rpc_id = body.get("id") if isinstance(body, dict) else None
        raise _RpcError(rpc_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    rpc_id = body.get("id")
    if body["method"] != "message/send":
        raise _RpcError(rpc_id, METHOD_NOT_FOUND, f"method {body['method']!r} is not supported")
    params = body.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or message.get("role") != "user":
        raise _RpcError(rpc_id, INVALID_PARAMS, "params.message with role 'user' is required")
    intent = _intent(message)
    if intent is None:
        raise _RpcError(rpc_id, INVALID_PARAMS, "no supported skill request found in message parts")
    return rpc_id, message, intent[0], intent[1]


async def _execute(
    c: Container, principal: Principal, message: dict[str, Any], skill: str, argument: str
) -> dict[str, Any]:
    try:
        if skill == "process-referral":
            case = await c.service.process_referral(principal, argument)
        else:
            case = await c.service.get_case(principal, argument)
    except AccessError as exc:
        state = "rejected" if isinstance(exc, ForbiddenError | InvalidStateError) else "failed"
        await c.service.record(principal, "a2a.message", f"a2a/{skill}", exc.code)
        c.metrics.agent_calls.labels("a2a", skill, exc.code).inc()
        return _task(message, state, exc.message, None)
    view = CaseView.of(case, principal)
    state, text = _summary(view)
    await c.service.record(
        principal, "a2a.message", f"a2a/{skill}", "ok", {"case_id": case.id, "state": state}
    )
    c.metrics.agent_calls.labels("a2a", skill, "ok").inc()
    return _task(message, state, text, view)


@router.post("/a2a", tags=["a2a"])
async def a2a(
    request: Request,
    c: Annotated[Container, Depends(container)],
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error(None, PARSE_ERROR, "invalid JSON")
    try:
        rpc_id, message, skill, argument = _parse(body)
    except _RpcError as exc:
        c.metrics.agent_calls.labels("a2a", "invalid", str(exc.code)).inc()
        return _error(exc.rpc_id, exc.code, exc.message)
    try:
        principal = await c.authn.authenticate(authorization)
    except AccessError as exc:
        c.metrics.agent_calls.labels("a2a", skill, exc.code).inc()
        return JSONResponse(
            {"error": exc.code, "error_description": exc.message},
            status_code=exc.status,
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        )
    result = await _execute(c, principal, message, skill, argument)
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})
