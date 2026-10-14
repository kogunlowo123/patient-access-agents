"""Outbound patient messaging.

``WebhookNotifier`` posts SMS/e-mail requests to the organisation's
messaging service, signing the body with HMAC-SHA256 and sending the reminder
id as the idempotency key so a retried delivery is not sent twice. The
portal channel needs no gateway: messages land in the patient's portal inbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Protocol

import httpx


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Notifier(Protocol):
    async def send(self, *, reminder_id: str, channel: str, recipient: str, body: str) -> None: ...


class WebhookNotifier:
    def __init__(self, url: str, *, http: httpx.AsyncClient, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("webhook signing key must be at least 32 bytes")
        self._url = url
        self._http = http
        self._key = signing_key

    def signature(self, payload: bytes) -> str:
        return "sha256=" + hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    async def send(self, *, reminder_id: str, channel: str, recipient: str, body: str) -> None:
        payload = json.dumps(
            {"id": reminder_id, "channel": channel, "to": recipient, "body": body},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        try:
            response = await self._http.post(
                self._url,
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": reminder_id,
                    "X-Signature-256": self.signature(payload),
                },
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"messaging service unreachable: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(
                f"messaging service returned {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise DeliveryError(
                f"messaging service refused the message: {response.status_code}", retryable=False
            )
