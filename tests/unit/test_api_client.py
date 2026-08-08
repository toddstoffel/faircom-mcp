from __future__ import annotations

import json

import httpx
import pytest

from faircom_mcp.api.client import FaircomAPIClient
from faircom_mcp.config import AuthConfig
from faircom_mcp.errors import TransportError, UpstreamAPIError


def _response(status_code: int, payload: object, request: httpx.Request) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload, request=request)


def test_get_retries_on_server_error_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(status_code=502, json={"error": "bad gateway"}, request=request)
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        max_read_retries=1,
        transport=httpx.MockTransport(handler),
    )

    result = client.request_json("GET", "/api/v1/resource")

    assert result == {"ok": True}
    assert attempts["count"] == 2


def test_post_does_not_retry_by_default() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(status_code=500, json={"error": "server"}, request=request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        max_read_retries=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamAPIError) as exc:
        client.request_json("POST", "/api/v1/action", json_body={"x": 1})

    assert exc.value.details["status_code"] == 500
    assert attempts["count"] == 1


def test_auth_failure_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401, text="unauthorized", request=request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamAPIError) as exc:
        client.request_json("GET", "/api/v1/resource")

    assert exc.value.details["status_code"] == 401
    assert exc.value.retryable is False


def test_timeout_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        max_read_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TransportError) as exc:
        client.request_json("GET", "/api/v1/resource")

    assert exc.value.details["path"] == "/api/v1/resource"


def test_malformed_json_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, text="not-json", request=request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamAPIError) as exc:
        client.request_json("GET", "/api/v1/resource")

    assert exc.value.message == "FairCom API returned non-JSON response"


@pytest.mark.parametrize(
    ("auth", "expected_token"),
    [
        (AuthConfig(token="abc"), "abc"),
        (AuthConfig(username="user", password="pass"), "session-token"),
    ],
)
def test_client_applies_expected_auth_headers(
    auth: AuthConfig,
    expected_token: str,
) -> None:
    create_session_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        if '"action":"createSession"' in body:
            create_session_calls["count"] += 1
            return _response(
                200,
                {
                    "authToken": "session-token",
                    "result": {"authToken": "session-token"},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        assert f'"authToken":"{expected_token}"' in body

        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=auth,
        transport=httpx.MockTransport(handler),
    )

    result = client.post_action("listTables")

    assert result == {"ok": True}
    if auth.token:
        assert create_session_calls["count"] == 0
    else:
        assert create_session_calls["count"] == 1


def test_non_zero_error_code_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            {
                "errorCode": 12025,
                "errorMessage": "Missing authToken",
            },
            request,
        )

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamAPIError) as exc:
        client.request_json("POST", "/api/v1/action", json_body={"action": "x"})

    assert exc.value.details["errorCode"] == 12025


def test_connector_application_error_adds_connector_remediation_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            {
                "errorCode": 12042,
                "errorMessage": "Application error",
                "debugInfo": {
                    "request": {
                        "api": "hub",
                        "action": "listInputs",
                    }
                },
            },
            request,
        )

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(token="tkn"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamAPIError) as exc:
        client.request_json("POST", "/api/v1/action", json_body={"action": "x"})

    assert exc.value.details["request_action"] == "listInputs"
    assert exc.value.details["request_api"] == "hub"
    assert "Connector action failed upstream" in (exc.value.hint or "")


def test_client_accepts_direct_username_password_constructor() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        calls.append({"headers": dict(request.headers), "body": body})
        if '"action":"createSession"' in body:
            return _response(
                200,
                {
                    "authToken": "session-token",
                    "result": {"authToken": "session-token"},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        username="user",
        password="pass",
        transport=httpx.MockTransport(handler),
    )

    result = client.post_action("listTables")

    assert result == {"ok": True}
    assert any('"action":"createSession"' in call["body"] for call in calls)
    assert any('"authToken":"session-token"' in call["body"] for call in calls)


def test_admin_action_uses_admin_api_surface() -> None:
    seen_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        seen_bodies.append(body)
        if '"action":"createSession"' in body:
            return _response(
                200,
                {
                    "authToken": "session-token",
                    "result": {"authToken": "session-token"},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(username="user", password="pass"),
        transport=httpx.MockTransport(handler),
    )

    result = client.admin_action("describeSessions")

    assert result == {"ok": True}
    assert any('"api":"admin"' in body for body in seen_bodies)


def test_json_action_reauths_and_retries_on_error_code_12031() -> None:
    create_session_calls = {"count": 0}
    action_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        action = body.get("action")
        if action == "createSession":
            create_session_calls["count"] += 1
            token = f"session-{create_session_calls['count']}"
            return _response(
                200,
                {
                    "authToken": token,
                    "result": {"authToken": token},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        action_calls["count"] += 1
        if action_calls["count"] == 1:
            assert body.get("authToken") == "session-1"
            return _response(
                200,
                {
                    "errorCode": 12031,
                    "errorMessage": "Session expired",
                },
                request,
            )

        assert body.get("authToken") == "session-2"
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(username="user", password="pass"),
        transport=httpx.MockTransport(handler),
    )

    result = client.hub_action("listInputs", {"inputNameLike": "demo%"})

    assert result == {"ok": True}
    assert create_session_calls["count"] == 2
    assert action_calls["count"] == 2


def test_json_action_reauths_when_12031_arrives_with_http_401() -> None:
    create_session_calls = {"count": 0}
    action_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        action = body.get("action")
        if action == "createSession":
            create_session_calls["count"] += 1
            token = f"session-{create_session_calls['count']}"
            return _response(
                200,
                {
                    "authToken": token,
                    "result": {"authToken": token},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        action_calls["count"] += 1
        if action_calls["count"] == 1:
            assert body.get("authToken") == "session-1"
            return _response(
                401,
                {
                    "errorCode": "12031",
                    "errorMessage": "Session expired",
                },
                request,
            )

        assert body.get("authToken") == "session-2"
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(username="user", password="pass"),
        transport=httpx.MockTransport(handler),
    )

    result = client.hub_action("listInputs", {"inputNameLike": "demo%"})

    assert result == {"ok": True}
    assert create_session_calls["count"] == 2
    assert action_calls["count"] == 2


def test_json_action_reauths_when_12031_is_nested_in_response_body() -> None:
    create_session_calls = {"count": 0}
    action_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        action = body.get("action")
        if action == "createSession":
            create_session_calls["count"] += 1
            token = f"session-{create_session_calls['count']}"
            return _response(
                200,
                {
                    "authToken": token,
                    "result": {"authToken": token},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        action_calls["count"] += 1
        if action_calls["count"] == 1:
            assert body.get("authToken") == "session-1"
            return _response(
                401,
                {
                    "error": {
                        "errorCode": "12031",
                        "errorMessage": "Session expired",
                    }
                },
                request,
            )

        assert body.get("authToken") == "session-2"
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(username="user", password="pass"),
        transport=httpx.MockTransport(handler),
    )

    result = client.hub_action("listInputs", {"inputNameLike": "demo%"})

    assert result == {"ok": True}
    assert create_session_calls["count"] == 2
    assert action_calls["count"] == 2


def test_json_action_reauths_when_error_body_is_text_session_invalidation() -> None:
    create_session_calls = {"count": 0}
    action_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        action = body.get("action")
        if action == "createSession":
            create_session_calls["count"] += 1
            token = f"session-{create_session_calls['count']}"
            return _response(
                200,
                {
                    "authToken": token,
                    "result": {"authToken": token},
                    "errorCode": 0,
                    "errorMessage": "",
                },
                request,
            )

        action_calls["count"] += 1
        if action_calls["count"] == 1:
            assert body.get("authToken") == "session-1"
            return httpx.Response(
                401,
                text="authToken did not match an existing session (errorCode=12031)",
                request=request,
            )

        assert body.get("authToken") == "session-2"
        return _response(200, {"ok": True}, request)

    client = FaircomAPIClient(
        base_url="https://example.test",
        auth=AuthConfig(username="user", password="pass"),
        transport=httpx.MockTransport(handler),
    )

    result = client.hub_action("listInputs", {"inputNameLike": "demo%"})

    assert result == {"ok": True}
    assert create_session_calls["count"] == 2
    assert action_calls["count"] == 2
