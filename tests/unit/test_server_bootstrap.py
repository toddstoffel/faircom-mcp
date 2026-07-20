from __future__ import annotations

import asyncio
import importlib
import sys
import types
from collections.abc import Callable

import httpx
import pytest

from faircom_mcp.config import AppConfig, AuthConfig, TransportConfig
from faircom_mcp.errors import ValidationFailure


def _get(path: str, app: object) -> httpx.Response:
    async def _request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(_request())


def _install_fake_fastmcp(monkeypatch: object) -> type:
    fake_module = types.ModuleType("fastmcp")

    class FakeFastMCP:
        last_instance: object | None = None

        def __init__(self, name: str) -> None:
            self.name = name
            self.routes: list[tuple[str, Callable[..., object]]] = []
            self.tools: dict[str, Callable[..., object]] = {}
            self.http_app_calls: list[str] = []
            self.state = types.SimpleNamespace()
            FakeFastMCP.last_instance = self

        def custom_route(self, path: str, methods: list[str]):
            _ = methods

            def decorator(handler: Callable[..., object]) -> Callable[..., object]:
                self.routes.append((path, handler))
                return handler

            return decorator

        def http_app(self, transport: str = "http") -> object:
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Route

            self.http_app_calls.append(transport)
            routes = [
                Route(
                    "/mcp",
                    endpoint=lambda _request: JSONResponse({"transport": transport}),
                    methods=["GET"],
                )
            ]
            for path, handler in self.routes:
                routes.append(Route(path, endpoint=handler, methods=["GET"]))
            return Starlette(routes=routes)

        def tool(self, name: str | None = None, **_kwargs: object):
            def decorator(handler: Callable[..., object]) -> Callable[..., object]:
                tool_name = name or handler.__name__
                self.tools[tool_name] = handler
                return handler

            return decorator

        async def run_async(self, transport: str = "stdio") -> None:
            self.http_app_calls.append(f"run:{transport}")

    fake_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_module)
    return FakeFastMCP


def _load_server_module(monkeypatch: object) -> tuple[type, object]:
    fake_class = _install_fake_fastmcp(monkeypatch)
    sys.modules.pop("faircom_mcp.server", None)
    server_module = importlib.import_module("faircom_mcp.server")
    return fake_class, server_module


def _config() -> AppConfig:
    return AppConfig(
        faircom_api_base_url="https://example.test/api",
        auth=AuthConfig(token="abc123"),
        transport=TransportConfig(host="127.0.0.1", port=8000),
        tls_verify=True,
    )


def test_create_server_registers_health_routes(monkeypatch: object) -> None:
    fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    captured: list[AppConfig] = []
    list_table_calls: list[str | None] = []
    describe_table_calls: list[str] = []
    list_table_columns_calls: list[str] = []
    list_table_indexes_calls: list[str] = []
    sql_query_calls: list[tuple[str, list[object] | None]] = []
    sql_query_page_calls: list[tuple[str, list[object] | None, int, int]] = []
    sql_execute_calls: list[tuple[str, list[object] | None]] = []

    class FakeTables:
        def list_tables(self, name_like: str | None = None) -> dict[str, object]:
            list_table_calls.append(name_like)
            return {"tables": [], "name_like": name_like}

        def describe_table(self, table_name: str) -> dict[str, object]:
            describe_table_calls.append(table_name)
            return {"table_name": table_name, "columns": []}

        def list_table_columns(self, table_name: str) -> dict[str, object]:
            list_table_columns_calls.append(table_name)
            return {"table_name": table_name, "columns": [{"name": "id"}], "column_count": 1}

        def list_table_indexes(self, table_name: str) -> dict[str, object]:
            list_table_indexes_calls.append(table_name)
            return {"table_name": table_name, "indexes": [{"name": "pk_demo"}], "index_count": 1}

    class FakeSQL:
        def query(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            sql_query_calls.append((statement, params))
            return {"statement": statement, "params": params}

        def query_page(
            self,
            statement: str,
            params: list[object] | None = None,
            *,
            page: int = 1,
            page_size: int = 100,
        ) -> dict[str, object]:
            sql_query_page_calls.append((statement, params, page, page_size))
            return {
                "statement": statement,
                "params": params,
                "page": page,
                "page_size": page_size,
            }

        def execute(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            sql_execute_calls.append((statement, params))
            return {"statement": statement, "params": params}

    class FakeClient:
        pass

    def client_factory(received: AppConfig) -> object:
        captured.append(received)
        return FakeClient()

    original_table_adapter = server_module.TableAdapter
    original_sql_adapter = server_module.SQLAdapter
    server_module.TableAdapter = lambda _client: FakeTables()
    server_module.SQLAdapter = lambda _client, **_kwargs: FakeSQL()
    try:
        server = server_module.create_server(config, client_factory=client_factory)
    finally:
        server_module.TableAdapter = original_table_adapter
        server_module.SQLAdapter = original_sql_adapter

    assert isinstance(server, fake_class)
    assert captured == [config]
    assert [path for path, _handler in server.routes] == [
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        "/metrics",
    ]
    assert list(server.tools) == [
        "list_tables",
        "describe_table",
        "list_table_columns",
        "list_table_indexes",
        "sql_query",
        "sql_query_page",
        "runtime_status",
        "sql_execute",
    ]

    app = server.http_app()

    assert _get("/health", app).json() == {"status": "ok"}
    assert _get("/healthz", app).json() == {"status": "ok"}
    assert _get("/ready", app).json() == {"status": "ready"}
    assert _get("/readyz", app).json() == {"status": "ready"}
    assert server.tools["list_tables"]() == {
        "tables": [],
        "name_like": None,
    }
    assert server.tools["list_tables"](name_like="foo") == {
        "tables": [],
        "name_like": "foo",
    }
    assert server.tools["describe_table"](table_name="demo") == {
        "table_name": "demo",
        "columns": [],
    }
    assert server.tools["list_table_columns"](table_name="demo") == {
        "table_name": "demo",
        "columns": [{"name": "id"}],
        "column_count": 1,
    }
    assert server.tools["list_table_indexes"](table_name="demo") == {
        "table_name": "demo",
        "indexes": [{"name": "pk_demo"}],
        "index_count": 1,
    }
    assert server.tools["sql_query"](
        statement="select * from demo",
        params=[1, "two"],
    ) == {
        "statement": "select * from demo",
        "params": [1, "two"],
    }
    assert server.tools["sql_query_page"](
        statement="select * from demo order by id",
        params=["active"],
        page=3,
        page_size=50,
    ) == {
        "statement": "select * from demo order by id",
        "params": ["active"],
        "page": 3,
        "page_size": 50,
    }
    assert server.tools["runtime_status"]() == {
        "service": "faircom-mcp",
        "tool_group_allowlist": ["metadata", "query", "write", "admin", "diagnostics"],
        "metrics_enabled": True,
        "tracing_enabled": False,
    }
    try:
        server.tools["sql_execute"](statement="update demo set flag = 1")
    except ValidationFailure as exc:
        assert exc.details == {"tool": "sql_execute"}
    else:  # pragma: no cover - defensive
        raise AssertionError("sql_execute should require explicit confirmation")

    assert server.tools["sql_execute"](
        statement="update demo set flag = 1",
        params=["x"],
        confirm_write=True,
    ) == {
        "statement": "update demo set flag = 1",
        "params": ["x"],
    }
    metrics_response = _get("/metrics", app)
    assert metrics_response.status_code == 200
    assert "faircom_mcp_tool_calls_total" in metrics_response.text

    assert list_table_calls == [None, "foo"]
    assert describe_table_calls == ["demo"]
    assert list_table_columns_calls == ["demo"]
    assert list_table_indexes_calls == ["demo"]
    assert sql_query_calls == [("select * from demo", [1, "two"])]
    assert sql_query_page_calls == [
        ("select * from demo order by id", ["active"], 3, 50)
    ]
    assert sql_execute_calls == [("update demo set flag = 1", ["x"])]


def test_create_http_app_honors_transport_and_readiness(monkeypatch: object) -> None:
    fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()

    app = server_module.create_http_app(
        config,
        readiness_check=lambda: False,
        transport="sse",
    )

    assert fake_class.last_instance is not None
    assert fake_class.last_instance.http_app_calls == ["sse"]

    assert _get("/readyz", app).status_code == 503
    assert _get("/readyz", app).json() == {"status": "not_ready"}
    assert _get("/mcp", app).json() == {"transport": "sse"}


def test_create_server_enforces_tool_group_policy(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    config.security.tool_group_allowlist = ("metadata",)

    class FakeTables:
        def list_tables(self, name_like: str | None = None) -> dict[str, object]:
            return {"tables": [], "name_like": name_like}

        def describe_table(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name}

        def list_table_columns(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name, "columns": []}

        def list_table_indexes(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name, "indexes": []}

    class FakeSQL:
        def query(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            return {"statement": statement, "params": params}

        def query_page(
            self,
            statement: str,
            params: list[object] | None = None,
            *,
            page: int = 1,
            page_size: int = 100,
        ) -> dict[str, object]:
            return {"statement": statement, "params": params, "page": page, "page_size": page_size}

        def execute(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            return {"statement": statement, "params": params}

    original_table_adapter = server_module.TableAdapter
    original_sql_adapter = server_module.SQLAdapter
    server_module.TableAdapter = lambda _client: FakeTables()
    server_module.SQLAdapter = lambda _client, **_kwargs: FakeSQL()
    try:
        server = server_module.create_server(config, client_factory=lambda _config: object())
    finally:
        server_module.TableAdapter = original_table_adapter
        server_module.SQLAdapter = original_sql_adapter

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](statement="select 1")

    assert exc.value.details["policy"] == "tool_group_allowlist"


def test_create_server_diagnostics_endpoints_require_token(monkeypatch: object) -> None:
    _fake_class, server_module = _load_server_module(monkeypatch)
    config = _config()
    config.security.diagnostics_enabled = True
    config.security.diagnostics_token = "diag-token"

    class FakeTables:
        def list_tables(self, name_like: str | None = None) -> dict[str, object]:
            return {"tables": [], "name_like": name_like}

        def describe_table(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name}

        def list_table_columns(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name, "columns": []}

        def list_table_indexes(self, table_name: str) -> dict[str, object]:
            return {"table_name": table_name, "indexes": []}

    class FakeSQL:
        def query(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            return {"statement": statement, "params": params}

        def query_page(
            self,
            statement: str,
            params: list[object] | None = None,
            *,
            page: int = 1,
            page_size: int = 100,
        ) -> dict[str, object]:
            return {"statement": statement, "params": params, "page": page, "page_size": page_size}

        def execute(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
            return {"statement": statement, "params": params}

    original_table_adapter = server_module.TableAdapter
    original_sql_adapter = server_module.SQLAdapter
    server_module.TableAdapter = lambda _client: FakeTables()
    server_module.SQLAdapter = lambda _client, **_kwargs: FakeSQL()
    try:
        server = server_module.create_server(config, client_factory=lambda _config: object())
    finally:
        server_module.TableAdapter = original_table_adapter
        server_module.SQLAdapter = original_sql_adapter

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
