"""Google Cloud workload identity: short-lived access tokens from the GKE metadata server."""

from __future__ import annotations

import httpx

from patient_access_agents.domain.errors import DependencyUnavailableError

# The metadata server is link-local and only reachable from inside the node;
# it is served over plain HTTP by design and requires the Metadata-Flavor header.
SERVICE_ACCOUNT_ENDPOINT = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


async def metadata_access_token(
    http: httpx.AsyncClient, url: str = SERVICE_ACCOUNT_ENDPOINT
) -> str:
    """Access token for the pod's Google service account (GKE Workload Identity)."""
    try:
        response = await http.get(url, headers={"Metadata-Flavor": "Google"}, timeout=5.0)
    except httpx.HTTPError as exc:
        raise DependencyUnavailableError(
            f"metadata server unreachable: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise DependencyUnavailableError(f"metadata server returned {response.status_code}")
    try:
        token = response.json()["access_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DependencyUnavailableError("metadata server returned no access token") from exc
    if not isinstance(token, str) or not token:
        raise DependencyUnavailableError("metadata server returned no access token")
    return token
