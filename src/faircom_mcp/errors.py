from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
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

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ValidationFailure(FaircomError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.VALIDATION_ERROR,
            message=message,
            details=details or {},
            retryable=False,
        )


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
        )


class ConfigurationError(FaircomError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.CONFIGURATION_ERROR,
            message=message,
            details=details or {},
            retryable=False,
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
    )
