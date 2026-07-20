from faircom_mcp.errors import (
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
