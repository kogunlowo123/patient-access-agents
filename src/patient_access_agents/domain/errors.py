"""Domain errors mapped to HTTP status codes and OAuth-style bodies by the API layer."""

from __future__ import annotations


class AccessError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AccessError):
    code = "not_found"
    status = 404


class ConflictError(AccessError):
    code = "conflict"
    status = 409


class InvalidStateError(AccessError):
    code = "invalid_state"
    status = 409


class UnauthorizedError(AccessError):
    code = "unauthorized"
    status = 401


class ForbiddenError(AccessError):
    code = "forbidden"
    status = 403


class UpstreamError(AccessError):
    """An integrated system (FHIR server, clearinghouse) returned an error."""

    code = "upstream_error"
    status = 502


class DependencyUnavailableError(AccessError):
    """A required dependency cannot be reached; the request failed closed."""

    code = "temporarily_unavailable"
    status = 503


class X12Error(AccessError):
    """An X12 interchange could not be built or parsed."""

    code = "x12_error"
    status = 502
