"""Bearer-token authentication for people and agents.

People (patients and access-centre staff) present tokens from the
organisation's OIDC provider. A patient is identified by the SMART
``patient`` launch-context claim or a ``fhirUser`` of ``Patient/<id>``; staff
by roles in the configured roles claim.

Agents present credentials issued by agent-identity-fabric. When the fabric
URL is configured the credential is introspected there (revocation and
suspension take effect immediately); otherwise it is verified locally
against the configured agent issuer's JWKS. With neither configured, agent
credentials are refused: there is no allow-all fallback.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from patient_access_agents.domain.errors import ForbiddenError, UnauthorizedError
from patient_access_agents.integrations.identity_fabric import FabricClient
from patient_access_agents.security.jwks import KeyResolver, verify_jwt
from patient_access_agents.security.principals import Principal, PrincipalKind, StaffRole


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("bearer token required")
    token = authorization[7:].strip()
    if not token or len(token) > 8192:
        raise UnauthorizedError("bearer token required")
    return token


def claimed_issuer(token: str) -> str | None:
    """The ``iss`` a JWT claims, used only to choose which verifier checks it.

    Nothing from the payload is trusted until the chosen verifier has checked
    the signature, issuer, audience and lifetime.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise UnauthorizedError("malformed token")
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UnauthorizedError("malformed token") from exc
    issuer = payload.get("iss") if isinstance(payload, dict) else None
    return issuer if isinstance(issuer, str) else None


def delegation_chain(subject: str, act: Any) -> tuple[str, ...]:
    chain = [subject]
    current = act
    while isinstance(current, dict) and isinstance(current.get("sub"), str) and len(chain) < 10:
        chain.append(current["sub"])
        current = current.get("act")
    return tuple(chain)


class Authenticator:
    def __init__(
        self,
        *,
        user_resolver: KeyResolver,
        user_issuer: str,
        user_audience: str,
        clock: Callable[[], datetime],
        roles_claim: str = "roles",
        patient_claim: str = "patient",
        fabric: FabricClient | None = None,
        agent_resolver: KeyResolver | None = None,
        agent_issuer: str | None = None,
        agent_audience: str | None = None,
    ) -> None:
        self._user_resolver = user_resolver
        self._user_issuer = user_issuer
        self._user_audience = user_audience
        self._clock = clock
        self._roles_claim = roles_claim
        self._patient_claim = patient_claim
        self._fabric = fabric
        self._agent_resolver = agent_resolver
        self._agent_issuer = agent_issuer
        self._agent_audience = agent_audience

    @property
    def agents_enabled(self) -> bool:
        return self._fabric is not None or self._agent_resolver is not None

    async def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        if claimed_issuer(token) == self._user_issuer:
            return await self._person(token)
        return await self._agent(token)

    async def _person(self, token: str) -> Principal:
        claims = await verify_jwt(
            token,
            self._user_resolver,
            issuer=self._user_issuer,
            audience=self._user_audience,
            now=self._clock(),
        )
        subject = str(claims["sub"])
        patient = claims.get(self._patient_claim)
        fhir_user = claims.get("fhirUser")
        if not isinstance(patient, str) and isinstance(fhir_user, str):
            parts = fhir_user.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "Patient":
                patient = parts[-1]
        if isinstance(patient, str) and patient:
            return Principal(subject=subject, kind=PrincipalKind.PATIENT, patient_id=patient)
        raw = claims.get(self._roles_claim, [])
        roles = (
            frozenset(r for r in raw if isinstance(r, str))
            if isinstance(raw, list)
            else frozenset()
        )
        staff_roles = (roles or frozenset()) & StaffRole.ALL
        if not staff_roles:
            raise ForbiddenError("token carries neither a patient context nor an access role")
        return Principal(subject=subject, kind=PrincipalKind.STAFF, roles=staff_roles)

    async def _agent(self, token: str) -> Principal:
        if self._fabric is not None:
            info = await self._fabric.introspect(token)
            if not info.active or not info.sub:
                raise UnauthorizedError("agent credential is not active")
            return Principal(
                subject=info.sub,
                kind=PrincipalKind.AGENT,
                scopes=info.scopes,
                delegated=info.act is not None,
                delegation_chain=delegation_chain(info.sub, info.act),
                credential=token,
            )
        if self._agent_resolver is None or not self._agent_issuer or not self._agent_audience:
            raise UnauthorizedError(
                "agent credentials are not accepted: no identity fabric or agent issuer configured"
            )
        claims = await verify_jwt(
            token,
            self._agent_resolver,
            issuer=self._agent_issuer,
            audience=self._agent_audience,
            now=self._clock(),
            algorithms=("EdDSA", "ES256", "RS256"),
        )
        scope = claims.get("scope")
        subject = str(claims["sub"])
        return Principal(
            subject=subject,
            kind=PrincipalKind.AGENT,
            scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            delegated="act" in claims,
            delegation_chain=delegation_chain(subject, claims.get("act")),
            credential=token,
        )
