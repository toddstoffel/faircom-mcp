from faircom_mcp.errors import (
    ConfigurationError,
    ErrorCode,
    FaircomError,
    ValidationFailure,
    normalize_exception,
)


def test_normalize_exception_returns_existing_faircom_error() -> None:
    existing = ValidationFailure("bad input")

    normalized = normalize_exception(existing)

    assert normalized is existing


def test_normalize_exception_maps_value_error() -> None:
    normalized = normalize_exception(ValueError("oops"))

    assert normalized.code == ErrorCode.VALIDATION_ERROR
    assert normalized.message == "oops"


def test_normalize_exception_maps_unknown_error() -> None:
    normalized = normalize_exception(RuntimeError("boom"))

    assert isinstance(normalized, FaircomError)
    assert normalized.code == ErrorCode.INTERNAL_ERROR
    assert normalized.message == "boom"


def test_validation_failure_exposes_structured_payload() -> None:
    error = ValidationFailure("bad input", details={"field": "name"})

    assert error.category == "validation"
    assert error.hint == "Review the input values and try again."
    assert error.to_payload() == {
        "code": ErrorCode.VALIDATION_ERROR,
        "message": "bad input",
        "category": "validation",
        "retryable": False,
        "hint": "Review the input values and try again.",
        "details": {"field": "name"},
    }


def test_configuration_error_exposes_structured_payload() -> None:
    error = ConfigurationError("missing env", details={"var": "FAIRCOM_API_BASE_URL"})

    assert error.category == "configuration"
    assert error.hint == "Review the server configuration and required environment variables."
    assert error.to_payload() == {
        "code": ErrorCode.CONFIGURATION_ERROR,
        "message": "missing env",
        "category": "configuration",
        "retryable": False,
        "hint": "Review the server configuration and required environment variables.",
        "details": {"var": "FAIRCOM_API_BASE_URL"},
    }


def test_normalize_exception_adds_default_hint_for_internal_errors() -> None:
    normalized = normalize_exception(RuntimeError("boom"))

    assert normalized.category == "internal"
    assert normalized.hint == "Unexpected server error. Please retry or contact support if it persists."
