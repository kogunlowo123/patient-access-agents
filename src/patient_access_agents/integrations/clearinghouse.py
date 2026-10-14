"""Real-time eligibility exchange with a clearinghouse over HTTPS.

The 270 is posted as ``application/EDI-X12`` and the synchronous response
body is the 271 (or a 999/TA1 acknowledgement, which the parser reports).
Timeouts, 429 and 5xx are reported as temporarily unavailable so the case is
retried with backoff; other errors are upstream rejections.
"""

from __future__ import annotations

import httpx

from patient_access_agents.domain.errors import DependencyUnavailableError, UpstreamError

EDI_X12 = "application/EDI-X12"


class ClearinghouseGateway:
    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient,
        api_key: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._url = url
        self._http = http
        self._api_key = api_key
        self._timeout = timeout_s

    async def exchange(self, interchange: str) -> str:
        headers = {"Content-Type": EDI_X12, "Accept": EDI_X12}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._http.post(
                self._url,
                content=interchange.encode("ascii"),
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise DependencyUnavailableError("clearinghouse timed out") from exc
        except httpx.TransportError as exc:
            raise DependencyUnavailableError(
                f"clearinghouse unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise DependencyUnavailableError(f"clearinghouse returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"clearinghouse rejected the inquiry: HTTP {response.status_code}")
        return response.text
