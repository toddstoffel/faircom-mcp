from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    POLICY_VIOLATION = "policy_violation"
    UPSTREAM_API_ERROR = "upstream_api_error"
    TRANSPORT_ERROR = "transport_error"
    CONFIGURATION_ERROR = "configuration_error"
    INTERNAL_ERROR = "internal_error"


@dataclass(slots=True)
class FaircomError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    category: str = "internal"
    hint: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category,
            "retryable": self.retryable,
            "hint": self.hint or "No remediation guidance available.",
            "details": self.details,
        }


class ValidationFailure(FaircomError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.VALIDATION_ERROR,
            message=message,
            details=details or {},
            retryable=False,
            category="validation",
            hint="Review the input values and try again.",
        )


class PolicyViolation(ValidationFailure):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        category: str = "authorization",
        hint: str | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = ErrorCode.POLICY_VIOLATION
        self.category = category
        self.hint = hint or "Adjust the policy or request a role with write access."


class UpstreamAPIError(FaircomError):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(
            code=ErrorCode.UPSTREAM_API_ERROR,
            message=message,
            details=details or {},
            retryable=retryable,
            category="upstream_failure",
            hint="The upstream FairCom service is unavailable or timed out. Retry with backoff if appropriate.",
        )


class TransportError(FaircomError):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            code=ErrorCode.TRANSPORT_ERROR,
            message=message,
            details=details or {},
            retryable=retryable,
            category="transport",
            hint="Check the network connection and service endpoint configuration.",
        )


class ConfigurationError(FaircomError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.CONFIGURATION_ERROR,
            message=message,
            details=details or {},
            retryable=False,
            category="configuration",
            hint="Review the server configuration and required environment variables.",
        )


def normalize_exception(exc: Exception) -> FaircomError:
    if isinstance(exc, FaircomError):
        return exc

    if isinstance(exc, ValueError):
        return ValidationFailure(str(exc))

    return FaircomError(
        code=ErrorCode.INTERNAL_ERROR,
        message=str(exc) or exc.__class__.__name__,
        retryable=False,
        category="internal",
        hint="Unexpected server error. Please retry or contact support if it persists.",
    )
