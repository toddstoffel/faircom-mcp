import json

from faircom_mcp.logging_utils import redact_sensitive


def test_redact_sensitive_redacts_nested_secrets() -> None:
    payload = {
        "username": "alice",
        "password": "p@ss",
        "nested": {
            "api_key": "xyz",
            "safe": "value",
        },
        "statement": "SELECT * FROM customers WHERE token = 'abc'",
        "query": "DELETE FROM customers",
        "headers": [{"Authorization": "Bearer token"}],
    }

    redacted = redact_sensitive(payload)

    assert redacted["username"] == "alice"
    assert redacted["password"] == "***REDACTED***"
    assert redacted["nested"]["api_key"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "value"
    assert redacted["statement"] == "***REDACTED***"
    assert redacted["query"] == "***REDACTED***"
    assert redacted["headers"][0]["Authorization"] == "***REDACTED***"


def test_redact_sensitive_result_is_json_serializable() -> None:
    payload = {"token": "secret", "count": 3}

    redacted = redact_sensitive(payload)

    serialized = json.dumps(redacted)
    assert "***REDACTED***" in serialized
