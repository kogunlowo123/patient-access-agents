"""Request and response bodies for the HTTP API."""

from __future__ import annotations

from agentic_core.domain.classification import Decision
from pydantic import BaseModel, ConfigDict, Field

from patient_access_agents.application.views import CaseView, MessageView, ReminderView
from patient_access_agents.domain.scheduling import Constraint

FHIR_ID = r"^[A-Za-z0-9\-.]{1,64}$"
OPAQUE_ID = r"^[A-Za-z0-9\-]{1,64}$"


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_request_id: str = Field(pattern=FHIR_ID, description="FHIR ServiceRequest logical id")


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")


class ManualEligibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    note: str = Field(min_length=5, max_length=1000)
    payer_reference: str = Field(
        min_length=3, max_length=64, description="Payer call reference or portal confirmation"
    )
    prior_authorization_required: bool = False


class BookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(pattern=OPAQUE_ID)


class BookingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str = Field(min_length=3, max_length=1000)


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: str = Field(min_length=5, max_length=1000)


class SlotSearchView(BaseModel):
    model_config = ConfigDict(frozen=True)

    case: CaseView
    feasible: bool
    considered: int
    eliminated: dict[Constraint, int]
    binding_constraint: Constraint | None


class BookingView(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    decision: Decision
    reason: str
    case: CaseView


class ReminderList(BaseModel):
    items: list[ReminderView]


class MessageList(BaseModel):
    items: list[MessageView]
