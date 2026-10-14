"""Typed client for agent-identity-fabric.

- ``token``: RFC 7523 jwt-bearer grant using this service's workload token,
  cached until shortly before expiry. Used for outbound calls that require
  an agent credential (FHIR, when configured).
- ``introspect``: RFC 7662; identifies the calling agent and its scopes.
- ``authorize``: the fabric's policy decision point for an agent action.

Every failure fails closed: an unreachable fabric means the agent request is
refused, never allowed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, ValidationError

from patient_access_agents.domain.errors import DependencyUnavailableError, UnauthorizedError

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class FabricIntrospection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    act: dict[str, Any] | None = None
    agent_status: str | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset((self.scope or "").split())


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...] = ()
    policy_version: str = ""
    policy_digest: str = ""
    reason: str = ""
    delegation_chain: tuple[str, ...] = ()


class FabricClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient,
        clock: Callable[[], datetime],
        client_id: str | None = None,
        workload_token: Callable[[], str] | None = None,
        refresh_margin: timedelta = timedelta(seconds=60),
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._clock = clock
        self._client_id = client_id
        self._workload_token = workload_token
        self._margin = refresh_margin
        self._cached: dict[tuple[str, ...], tuple[str, datetime]] = {}

    async def _post(
        self,
        path: str,
        *,
        data: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.post(
                f"{self._base}{path}", data=data, json=json, headers=dict(headers or {})
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                f"identity fabric unreachable: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise DependencyUnavailableError(f"identity fabric returned {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DependencyUnavailableError("identity fabric returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise DependencyUnavailableError("identity fabric returned an unexpected body")
        return body

    async def token(self, scopes: Sequence[str]) -> str:
        if self._client_id is None or self._workload_token is None:
            raise UnauthorizedError("this service has no workload identity configured")
        key = tuple(sorted(scopes))
        cached = self._cached.get(key)
        now = self._clock()
        if cached is not None and cached[1] - self._margin > now:
            return cached[0]
        response = await self._post(
            "/oauth2/token",
            data={
                "grant_type": JWT_BEARER,
                "client_id": self._client_id,
                "assertion": self._workload_token(),
                "scope": " ".join(key),
            },
        )
        body = self._json(response)
        if response.status_code != 200 or "access_token" not in body:
            raise UnauthorizedError(
                f"identity fabric refused credential: {body.get('error', response.status_code)}"
            )
        expires_in = int(body.get("expires_in", 300))
        token = str(body["access_token"])
        self._cached[key] = (token, now + timedelta(seconds=expires_in))
        return token

    async def introspect(self, token: str) -> FabricIntrospection:
        response = await self._post("/oauth2/introspect", data={"token": token})
        body = self._json(response)
        try:
            return FabricIntrospection.model_validate(body)
        except ValidationError as exc:
            raise DependencyUnavailableError("identity fabric introspection malformed") from exc

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        classification: DataClassification,
        attributes: Mapping[str, Any],
        environment: Mapping[str, Any],
    ) -> FabricDecision:
        response = await self._post(
            "/v1/authorize",
            json={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_classification": int(classification),
                "resource_attributes": dict(attributes),
                "environment": dict(environment),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        body = self._json(response)
        if response.status_code in {401, 403}:
            detail = body.get("error_description", "")
            raise UnauthorizedError(f"identity fabric rejected the agent credential: {detail}")
        if response.status_code != 200:
            raise DependencyUnavailableError(
                f"identity fabric authorization failed: {response.status_code}"
            )
        try:
            return FabricDecision.model_validate(body)
        except ValidationError as exc:
            raise DependencyUnavailableError("identity fabric decision malformed") from exc
