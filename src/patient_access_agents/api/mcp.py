"""MCP server: the patient access tools for agent hosts (official ``mcp`` SDK, streamable HTTP).

Mounted at ``/mcp`` in stateless JSON-response mode so any replica can serve
any call. Each tool:

- authenticates the caller from the HTTP ``Authorization`` header (an
  agent-identity-fabric credential, or a person's OIDC token),
- runs the same application use case as the REST API, which enforces the
  authorization policy and writes the decision to the audit chain,
- records an ``mcp.tool`` audit event with the tool name and outcome,
- has typed, validated inputs and a structured output schema.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from patient_access_agents import __version__
from patient_access_agents.api.schemas import FHIR_ID, OPAQUE_ID, BookingView, SlotSearchView
from patient_access_agents.application.views import (
    CaseView,
    EscalationList,
    EscalationView,
)
from patient_access_agents.domain.errors import AccessError
from patient_access_agents.domain.models import EscalationStatus
from patient_access_agents.runtime.container import Container
from patient_access_agents.security.principals import Principal

T = TypeVar("T")

INSTRUCTIONS = (
    "Patient access tools: take a FHIR ServiceRequest referral through eligibility "
    "verification and appointment scheduling. Bookings requested by agents are held for "
    "human approval; denials and capacity problems are escalated to staff queues."
)

ServiceRequestId = Annotated[
    str, Field(pattern=FHIR_ID, description="FHIR ServiceRequest logical id")
]
CaseIdArg = Annotated[str, Field(pattern=OPAQUE_ID, description="Patient access case id")]
ProposalIdArg = Annotated[
    str, Field(pattern=OPAQUE_ID, description="Proposal id from propose_slots")
]


def build_mcp(container_of: Callable[[], Container]) -> MCPServer:
    server: MCPServer = MCPServer(
        name="patient-access-agents",
        title="Patient Access Agents",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    async def call(
        ctx: Context, tool: str, operation: Callable[[Container, Principal], Awaitable[T]]
    ) -> T:
        container = container_of()
        headers = ctx.headers or {}
        authorization = headers.get("authorization") or headers.get("Authorization")
        try:
            principal = await container.authn.authenticate(authorization)
        except AccessError as exc:
            container.metrics.agent_calls.labels("mcp", tool, exc.code).inc()
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        try:
            result = await operation(container, principal)
        except AccessError as exc:
            container.metrics.agent_calls.labels("mcp", tool, exc.code).inc()
            await container.service.record(
                principal, "mcp.tool", f"mcp/{tool}", exc.code, {"error": exc.message}
            )
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        container.metrics.agent_calls.labels("mcp", tool, "ok").inc()
        await container.service.record(principal, "mcp.tool", f"mcp/{tool}", "ok")
        return result

    @server.tool(
        name="process_referral",
        annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
    )
    async def process_referral(service_request_id: ServiceRequestId, ctx: Context) -> CaseView:
        """Take a referral (FHIR ServiceRequest) through intake, eligibility and slot proposal.

        Stops where a person is needed; the returned case shows its status and any proposals.
        """

        async def op(c: Container, who: Principal) -> CaseView:
            return CaseView.of(await c.service.process_referral(who, service_request_id), who)

        return await call(ctx, "process_referral", op)

    @server.tool(name="get_case", annotations=ToolAnnotations(read_only_hint=True))
    async def get_case(case_id: CaseIdArg, ctx: Context) -> CaseView:
        """Read a patient access case (fields are limited to what the caller may see)."""

        async def op(c: Container, who: Principal) -> CaseView:
            return CaseView.of(await c.service.get_case(who, case_id), who)

        return await call(ctx, "get_case", op)

    @server.tool(
        name="verify_eligibility",
        annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
    )
    async def verify_eligibility(case_id: CaseIdArg, ctx: Context) -> CaseView:
        """Run a real-time X12 270/271 eligibility inquiry for the case."""

        async def op(c: Container, who: Principal) -> CaseView:
            return CaseView.of(await c.service.verify_eligibility(who, case_id), who)

        return await call(ctx, "verify_eligibility", op)

    @server.tool(
        name="propose_slots",
        annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
    )
    async def propose_slots(case_id: CaseIdArg, ctx: Context) -> SlotSearchView:
        """Search free FHIR Slots and return ranked options that satisfy every constraint."""

        async def op(c: Container, who: Principal) -> SlotSearchView:
            case, result = await c.service.propose_slots(who, case_id)
            return SlotSearchView(
                case=CaseView.of(case, who),
                feasible=result.feasible,
                considered=result.considered,
                eliminated=result.eliminated,
                binding_constraint=result.binding_constraint,
            )

        return await call(ctx, "propose_slots", op)

    @server.tool(
        name="request_booking",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    async def request_booking(
        case_id: CaseIdArg, proposal_id: ProposalIdArg, ctx: Context
    ) -> BookingView:
        """Request a booking for a proposed option. Agent requests await human approval."""

        async def op(c: Container, who: Principal) -> BookingView:
            outcome = await c.service.book(who, case_id, proposal_id)
            return BookingView(
                outcome=outcome.outcome,
                decision=outcome.decision,
                reason=outcome.reason,
                case=CaseView.of(outcome.case, who),
            )

        return await call(ctx, "request_booking", op)

    @server.tool(name="list_escalations", annotations=ToolAnnotations(read_only_hint=True))
    async def list_escalations(
        ctx: Context,
        queue: Annotated[str | None, Field(pattern=r"^[a-z\-]{1,64}$")] = None,
        status: EscalationStatus | None = None,
    ) -> EscalationList:
        """List escalations raised to human queues."""

        async def op(c: Container, who: Principal) -> EscalationList:
            items = await c.service.list_escalations(who, queue=queue, status=status)
            return EscalationList(items=[EscalationView.of(e) for e in items], limit=100, offset=0)

        return await call(ctx, "list_escalations", op)

    return server
