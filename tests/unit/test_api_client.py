from __future__ import annotations

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
