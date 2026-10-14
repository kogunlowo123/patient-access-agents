"""Authorization for every read and state change.

Local decisions come from the agentic-core PolicyEngine evaluating
``policies/patient-access.yaml`` (deny overrides, then require_approval,
then allow; default deny). For agents, when agent-identity-fabric is
configured, the fabric's decision for the same action is obtained as well
and the most restrictive of the two wins, so the fabric's agent-wide rules
(delegation limits, classification ceilings, suspension) and this
service's domain rules (patients see only their own data, bookings by
agents need human approval) both hold.
"""

from __future__ import annotations

from typing import Any

from agentic_core.domain.classification import DataClassification, Decision, RiskLevel
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict

from patient_access_agents.domain.errors import ForbiddenError, NotFoundError
from patient_access_agents.integrations.identity_fabric import FabricClient, FabricDecision
from patient_access_agents.security.principals import Principal, PrincipalKind

_STRICTNESS = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}

ACTION_RISK: dict[str, RiskLevel] = {
    "case:read": RiskLevel.LOW,
    "case:create": RiskLevel.MODERATE,
    "case:route": RiskLevel.MODERATE,
    "case:preferences": RiskLevel.LOW,
    "case:close": RiskLevel.HIGH,
    "eligibility:verify": RiskLevel.MODERATE,
    "eligibility:override": RiskLevel.HIGH,
    "slots:search": RiskLevel.LOW,
    "appointment:book": RiskLevel.MODERATE,
    "appointment:approve": RiskLevel.HIGH,
    "reminder:acknowledge": RiskLevel.LOW,
    "message:read": RiskLevel.LOW,
    "escalation:read": RiskLevel.LOW,
    "escalation:claim": RiskLevel.MODERATE,
    "escalation:resolve": RiskLevel.HIGH,
}


def purpose_of_use(action: str) -> str:
    """HL7 v3 PurposeOfUse: eligibility is payment, everything else care coordination."""
    return "HPAYMT" if action.startswith("eligibility:") else "TREAT"


class AccessDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    action: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    fabric: FabricDecision | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def audit(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "decision": self.decision.value,
            "matched_rules": list(self.matched_rules),
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
        }
        if self.fabric is not None:
            details["fabric_decision"] = self.fabric.decision.value
            details["fabric_policy_digest"] = self.fabric.policy_digest
        return details


class AccessController:
    def __init__(self, engine: PolicyEngine, *, fabric: FabricClient | None = None) -> None:
        self._engine = engine
        self._fabric = fabric

    @property
    def policy_digest(self) -> str:
        return self._engine.policy.digest

    async def decide(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str = "*",
        attributes: dict[str, Any] | None = None,
    ) -> AccessDecision:
        subject_attributes: dict[str, Any] = {
            "scopes": sorted(principal.scopes),
            "action_in_scope": principal.in_scope(action),
            "delegated": principal.delegated,
        }
        if principal.patient_id is not None:
            subject_attributes["patient_id"] = principal.patient_id
        environment = {"purpose_of_use": purpose_of_use(action)}
        request = PolicyRequest(
            subject=Subject(
                id=principal.subject,
                kind=principal.kind.value,
                roles=principal.roles,
                attributes=subject_attributes,
            ),
            action=action,
            resource=Resource(
                type=resource_type,
                id=resource_id,
                classification=DataClassification.RESTRICTED,
                attributes=dict(attributes or {}),
            ),
            risk=ACTION_RISK.get(action, RiskLevel.HIGH),
            environment=environment,
        )
        local = self._engine.evaluate(request)
        decision = AccessDecision(
            decision=local.decision,
            action=action,
            matched_rules=local.matched_rules,
            policy_version=local.policy_version,
            policy_digest=local.policy_digest,
            reason=local.reason,
        )
        if (
            principal.kind is PrincipalKind.AGENT
            and self._fabric is not None
            and principal.credential is not None
            and decision.decision is not Decision.DENY
        ):
            remote = await self._fabric.authorize(
                principal.credential,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                classification=DataClassification.RESTRICTED,
                attributes={
                    k: v for k, v in (attributes or {}).items() if isinstance(v, str | int | bool)
                },
                environment=environment,
            )
            if _STRICTNESS[remote.decision] > _STRICTNESS[decision.decision]:
                decision = decision.model_copy(
                    update={
                        "decision": remote.decision,
                        "reason": f"identity fabric: {remote.reason}",
                        "fabric": remote,
                    }
                )
            else:
                decision = decision.model_copy(update={"fabric": remote})
        return decision

    async def require(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str = "*",
        attributes: dict[str, Any] | None = None,
        conceal: bool = False,
    ) -> AccessDecision:
        """Raise unless the action is allowed or needs approval.

        ``conceal`` turns a denial into "not found" so callers cannot probe for
        other patients' records.
        """
        decision = await self.decide(
            principal,
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            attributes=attributes,
        )
        if decision.decision is Decision.DENY:
            if conceal:
                raise NotFoundError(f"{resource_type} {resource_id} not found")
            raise ForbiddenError(f"{action} denied: {decision.reason}")
        return decision
