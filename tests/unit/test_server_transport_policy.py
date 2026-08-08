from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from faircom_mcp.errors import ValidationFailure
from tests.helpers.http import get as _get
from tests.helpers.server_harness import (
    BasicFakeSQL,
    BasicFakeTables,
    patched_adapters,
)
from tests.helpers.server_harness import (
    create_test_config as _config,
)
from tests.helpers.server_harness import (
    load_server_module as _load_server_module,
)


def test_sql_query_normalizes_select_first_to_top(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(config, client_factory=lambda _config: object())

    result = server.tools["sql_query"](
        statement="select first 5 id, status from demo_assets order by id",
    )

    assert result["statement"] == "select TOP 5 id, status from demo_assets order by id"
    assert result["compatibility"]["metadata"]["sql_rewrites"] == [{"from": "FIRST", "to": "TOP"}]
    metrics_payload = server.tools["observability_metrics"]()
    assert metrics_payload["compatibility_events"]["sql_query:sql_rewritten_first_to_top"] >= 1


def test_create_http_app_honors_transport_and_readiness(monkeypatch: object) -> None:
    fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    original_create_server = server_module.create_server
    monkeypatch.setattr(
        server_module,
        "create_server",
        lambda config, *, readiness_check=None: original_create_server(
            config,
            client_factory=lambda _config: object(),
            readiness_check=readiness_check,
        ),
    )

    app = server_module.create_http_app(
        config,
        readiness_check=lambda: False,
        transport="sse",
    )

    assert fake_class.last_instance is not None
    assert fake_class.last_instance.http_app_calls == ["sse"]

    assert _get("/readyz", app).status_code == 503
    assert _get("/readyz", app).json() == {"status": "not_ready", "reason": "not_ready"}
    assert _get("/mcp", app).json() == {"transport": "sse"}


def test_create_http_app_auto_negotiates_transport(monkeypatch: object) -> None:
    fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    original_create_server = server_module.create_server
    monkeypatch.setattr(
        server_module,
        "create_server",
        lambda config, *, readiness_check=None: original_create_server(
            config,
            client_factory=lambda _config: object(),
            readiness_check=readiness_check,
        ),
    )

    app = server_module.create_http_app(
        config,
        readiness_check=lambda: True,
        transport="auto",
    )

    assert fake_class.last_instance is not None
    assert fake_class.last_instance.http_app_calls == ["http", "sse"]

    async def _request_with_accept(accept: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/mcp", headers={"Accept": accept})

    http_response = asyncio.run(_request_with_accept("application/json"))
    sse_response = asyncio.run(_request_with_accept("text/event-stream"))

    assert http_response.status_code == 200
    assert sse_response.status_code == 200
    assert http_response.json() == {"transport": "http"}
    assert sse_response.json() == {"transport": "sse"}


def test_create_http_app_http_mode_uses_compat_wrapper(monkeypatch: object) -> None:
    fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    original_create_server = server_module.create_server
    monkeypatch.setattr(
        server_module,
        "create_server",
        lambda config, *, readiness_check=None: original_create_server(
            config,
            client_factory=lambda _config: object(),
            readiness_check=readiness_check,
        ),
    )

    app = server_module.create_http_app(
        config,
        readiness_check=lambda: True,
        transport="http",
    )

    assert fake_class.last_instance is not None
    assert fake_class.last_instance.http_app_calls == ["http", "sse"]
    assert _get("/mcp", app).status_code == 200


def test_create_http_app_adds_session_recovery_for_expired_session(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)

    class _SessionExpiryServer:
        state = types.SimpleNamespace(runtime_metrics=None)

        def http_app(self, transport: str = "http") -> object:
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Route

            _ = transport

            async def _expired_session(_request: object) -> JSONResponse:
                return JSONResponse(
                    {
                        "errorCode": 12031,
                        "errorMessage": "Session expired",
                    },
                    status_code=401,
                )

            return Starlette(routes=[Route("/mcp", endpoint=_expired_session, methods=["POST"])])

    monkeypatch.setattr(
        server_module,
        "create_server",
        lambda config, *, readiness_check=None: _SessionExpiryServer(),
    )

    app = server_module.create_http_app(
        _config(),
        readiness_check=lambda: True,
        transport="http",
    )

    async def _request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/mcp", headers={"Accept": "application/json"})

    response = asyncio.run(_request())

    assert response.status_code == 401
    payload = response.json()
    assert payload["errorCode"] == 12031
    assert payload["recovery"]["reinitialize_required"] is True
    assert payload["recovery"]["reason_code"] == "stale_session"


def test_create_server_enforces_tool_group_policy(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    config.security.tool_group_allowlist = ("metadata",)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(config, client_factory=lambda _config: object())

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](statement="select 1")

    assert exc.value.details["policy"] == "tool_group_allowlist"


def test_create_server_diagnostics_endpoints_require_token(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    config.security.diagnostics_enabled = True
    config.security.diagnostics_token = "diag-token"

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(config, client_factory=lambda _config: object())

    app = server.http_app()
    assert _get("/diagnostics/json", app).status_code == 403
    assert _get("/diagnostics", app).status_code == 403

    async def _authorized_get(path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, headers={"x-diagnostics-token": "diag-token"})

    response = asyncio.run(_authorized_get("/diagnostics/json"))
    assert response.status_code == 200
    assert response.json()["service"] == "faircom-mcp"
