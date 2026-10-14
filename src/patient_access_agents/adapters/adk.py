"""Google Agent Development Kit (ADK) adapter.

ADK turns plain Python callables into tools by reading their name, type
hints and docstring (``google.adk.tools.FunctionTool``). ``AdkToolset``
exposes the patient access use cases in that shape: JSON-compatible
arguments and results, errors returned as data rather than raised, and every
call made as the agent principal the toolset was created for, so the same
authorization policy, approvals and audit apply as for REST, MCP and A2A
callers.

``build_adk_agent`` assembles an ADK ``LlmAgent`` (Gemini by default) from
the toolset. ADK is imported lazily so this service does not depend on it;
install ``google-adk`` in the agent runtime image that hosts the agent.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.application.views import CaseView
from patient_access_agents.domain.errors import AccessError
from patient_access_agents.security.principals import Principal

ADK_INSTRUCTION = (
    "You coordinate patient access for referrals. For a referral, call process_referral. "
    "If options are proposed, present them to the patient and call request_booking with the "
    "option they choose; agent bookings are held for human approval, so tell the patient a "
    "staff member will confirm. Never invent appointment times, coverage details or "
    "clinical advice; report tool errors plainly."
)

ToolFunction = Callable[..., Awaitable[dict[str, Any]]]


class AdkToolset:
    def __init__(self, service: PatientAccessService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    def functions(self) -> list[ToolFunction]:
        return [
            self.process_referral,
            self.get_case_status,
            self.verify_eligibility,
            self.propose_slots,
            self.request_booking,
        ]

    def _case(self, case: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "case": CaseView.of(case, self._principal).model_dump(mode="json"),
        }

    @staticmethod
    def _error(exc: AccessError) -> dict[str, Any]:
        return {"status": "error", "error": exc.code, "message": exc.message}

    async def process_referral(self, service_request_id: str) -> dict[str, Any]:
        """Take a FHIR ServiceRequest referral through intake, eligibility and slot proposal.

        Args:
            service_request_id: Logical id of the FHIR ServiceRequest.

        Returns:
            The patient access case, or an error code and message.
        """
        try:
            return self._case(
                await self._service.process_referral(self._principal, service_request_id)
            )
        except AccessError as exc:
            return self._error(exc)

    async def get_case_status(self, case_id: str) -> dict[str, Any]:
        """Read the current status of a patient access case.

        Args:
            case_id: Patient access case id.
        """
        try:
            return self._case(await self._service.get_case(self._principal, case_id))
        except AccessError as exc:
            return self._error(exc)

    async def verify_eligibility(self, case_id: str) -> dict[str, Any]:
        """Run an X12 270/271 insurance eligibility check for the case.

        Args:
            case_id: Patient access case id.
        """
        try:
            return self._case(await self._service.verify_eligibility(self._principal, case_id))
        except AccessError as exc:
            return self._error(exc)

    async def propose_slots(self, case_id: str) -> dict[str, Any]:
        """Find ranked appointment options that satisfy every scheduling constraint.

        Args:
            case_id: Patient access case id.
        """
        try:
            case, result = await self._service.propose_slots(self._principal, case_id)
        except AccessError as exc:
            return self._error(exc)
        payload = self._case(case)
        payload["binding_constraint"] = result.binding_constraint
        return payload

    async def request_booking(self, case_id: str, proposal_id: str) -> dict[str, Any]:
        """Request a booking for one of the proposed options.

        Args:
            case_id: Patient access case id.
            proposal_id: The proposal id of the chosen option.

        Returns:
            outcome "booked" or "pending_approval" with the updated case.
        """
        try:
            outcome = await self._service.book(self._principal, case_id, proposal_id)
        except AccessError as exc:
            return self._error(exc)
        payload = self._case(outcome.case)
        payload["outcome"] = outcome.outcome
        return payload


def build_adk_agent(
    toolset: AdkToolset,
    *,
    model: str = "gemini-2.5-pro",
    name: str = "patient_access_coordinator",
) -> Any:
    """Build a google.adk ``LlmAgent`` with the patient access tools."""
    try:
        agents = importlib.import_module("google.adk.agents")
        tools = importlib.import_module("google.adk.tools")
    except ImportError as exc:
        raise RuntimeError(
            "google-adk is not installed; add it to the agent runtime image to use this adapter"
        ) from exc
    return agents.LlmAgent(
        name=name,
        model=model,
        instruction=ADK_INSTRUCTION,
        tools=[tools.FunctionTool(func) for func in toolset.functions()],
    )
