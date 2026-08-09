"""Application error types and their HTTP mapping.

Service and repository layers raise these; a single exception handler in
`main.py` renders them. That keeps HTTP concerns out of business logic — a
service never imports HTTPException.
"""

from __future__ import annotations

from typing import Any


class DecisionFlowError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class NotFoundError(DecisionFlowError):
    status_code = 404
    code = "not_found"
    message = "The requested resource does not exist."


class ConflictError(DecisionFlowError):
    status_code = 409
    code = "conflict"
    message = "The resource already exists."


class ValidationError(DecisionFlowError):
    status_code = 422
    code = "validation_error"
    message = "The request payload is invalid."


class AuthenticationError(DecisionFlowError):
    status_code = 401
    code = "unauthenticated"
    message = "Authentication credentials are missing or invalid."


class PermissionDeniedError(DecisionFlowError):
    status_code = 403
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class RateLimitedError(DecisionFlowError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests."


class UnsafeQueryError(DecisionFlowError):
    """Raised when generated SQL fails the safety gate.

    Its own type because it is the one error the LLM layer is allowed to see
    and retry against — the agent feeds `message` back to the model as
    correction context.
    """

    status_code = 400
    code = "unsafe_query"
    message = "The generated query was rejected by the safety checker."


class LLMUnavailableError(DecisionFlowError):
    status_code = 503
    code = "llm_unavailable"
    message = "The language model is not configured or is unreachable."
