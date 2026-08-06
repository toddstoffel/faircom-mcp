from __future__ import annotations

import importlib
import sys
import types
from contextlib import contextmanager
from collections.abc import Callable

from faircom_mcp.config import AppConfig, AuthConfig, TransportConfig


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


def install_fake_fastmcp(monkeypatch: object) -> type:
    fake_module = types.ModuleType("fastmcp")
    fake_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_module)
    return FakeFastMCP


def load_server_module(monkeypatch: object) -> tuple[type, object]:
    fake_class = install_fake_fastmcp(monkeypatch)
    sys.modules.pop("faircom_mcp.server", None)
    server_module = importlib.import_module("faircom_mcp.server")
    return fake_class, server_module


def create_test_config() -> AppConfig:
    return AppConfig(
        faircom_api_base_url="https://example.test/api",
        auth=AuthConfig(token="abc123"),
        transport=TransportConfig(host="127.0.0.1", port=8000),
        tls_verify=True,
    )


class BasicFakeTables:
    def list_tables(
        self,
        name_like: str | None = None,
        *,
        database: str | None = None,
    ) -> dict[str, object]:
        return {"tables": [], "name_like": name_like, "database": database}

    def describe_table(self, table_name: str) -> dict[str, object]:
        return {"table_name": table_name, "columns": []}

    def list_table_columns(self, table_name: str) -> dict[str, object]:
        return {"table_name": table_name, "columns": []}

    def list_table_indexes(self, table_name: str) -> dict[str, object]:
        return {"table_name": table_name, "indexes": []}


class BasicFakeSQL:
    def query(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
        return {"statement": statement, "params": params}

    def query_page(
        self,
        statement: str,
        params: list[object] | None = None,
        *,
        page: int = 1,
        page_size: int = 100,
        continuation_token: str | None = None,
        order_by: str | None = None,
    ) -> dict[str, object]:
        return {
            "statement": statement,
            "params": params,
            "page": page,
            "page_size": page_size,
            "continuation_token": continuation_token,
            "order_by": order_by,
        }

    def execute(
        self,
        statement: str,
        params: list[object] | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        return {"statement": statement, "params": params, "dry_run": dry_run}


@contextmanager
def patched_adapters(
    server_module: object,
    *,
    table_adapter: object | None = None,
    sql_adapter: object | None = None,
    connector_adapter: object | None = None,
):
    original_table_adapter = getattr(server_module, "TableAdapter")
    original_sql_adapter = getattr(server_module, "SQLAdapter")
    original_connector_adapter = getattr(server_module, "ConnectorAdapter")

    if table_adapter is not None:
        setattr(server_module, "TableAdapter", lambda _client: table_adapter)
    if sql_adapter is not None:
        setattr(server_module, "SQLAdapter", lambda _client, **_kwargs: sql_adapter)
    if connector_adapter is not None:
        setattr(server_module, "ConnectorAdapter", lambda _client: connector_adapter)

    try:
        yield
    finally:
        setattr(server_module, "TableAdapter", original_table_adapter)
        setattr(server_module, "SQLAdapter", original_sql_adapter)
        setattr(server_module, "ConnectorAdapter", original_connector_adapter)
