from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Callable

import pytest

from faircom_mcp.config import AppConfig, AuthConfig, TransportConfig
from faircom_mcp.errors import ValidationFailure


class _FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name
        self.routes: list[tuple[str, Callable[..., object]]] = []
        self.tools: dict[str, Callable[..., object]] = {}

    def custom_route(self, path: str, methods: list[str]):
        _ = methods

        def decorator(handler: Callable[..., object]) -> Callable[..., object]:
            self.routes.append((path, handler))
            return handler

        return decorator

    def tool(self, name: str | None = None, **_kwargs: object):
        def decorator(handler: Callable[..., object]) -> Callable[..., object]:
            self.tools[name or handler.__name__] = handler
            return handler

        return decorator


def _load_server_module(monkeypatch: object) -> object:
    fake_module = types.ModuleType("fastmcp")
    fake_module.FastMCP = _FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_module)
    sys.modules.pop("faircom_mcp.server", None)
    return importlib.import_module("faircom_mcp.server")


def _config() -> AppConfig:
    return AppConfig(
        faircom_api_base_url="https://example.test/api",
        auth=AuthConfig(token="abc123"),
        transport=TransportConfig(host="127.0.0.1", port=8000),
        tls_verify=True,
    )


def _make_server(monkeypatch: object) -> object:
    server_module = _load_server_module(monkeypatch)

    class FakeTables:
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

    original_table_adapter = server_module.TableAdapter
    original_sql_adapter = server_module.SQLAdapter
    server_module.TableAdapter = lambda _client: FakeTables()
    server_module.SQLAdapter = lambda _client, **_kwargs: FakeSQL()
    try:
        server = server_module.create_server(_config(), client_factory=lambda _config: object())
    finally:
        server_module.TableAdapter = original_table_adapter
        server_module.SQLAdapter = original_sql_adapter
    return server


def test_compatibility_matrix_strict_canonical_sql_query(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["sql_query"](statement="select * from demo_assets")

    assert result == {"statement": "select * from demo_assets", "params": None}


def test_compatibility_matrix_alias_sql_query(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["sql_query"](sql="select * from demo_assets")

    assert result["statement"] == "select * from demo_assets"
    assert result["compatibility"]["normalized_args"] == [{"from": "sql", "to": "statement"}]


def test_compatibility_matrix_alias_table_metadata(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["list_tables"](table_like="demo_%", database="faircom")

    assert result["name_like"] == "demo_%"
    assert result["compatibility"]["normalized_args"] == [{"from": "table_like", "to": "name_like"}]
    assert result["compatibility"]["metadata"]["database"]["applied"] is False


def test_compatibility_matrix_dialect_first_to_top(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["sql_query"](statement="select first 3 id from demo_assets")

    assert result["statement"] == "select TOP 3 id from demo_assets"
    assert result["compatibility"]["metadata"]["sql_rewrites"] == [{"from": "FIRST", "to": "TOP"}]


def test_compatibility_matrix_dialect_unsupported_limit(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](statement="select * from demo_assets limit 1")

    assert exc.value.details["reason_code"] == "unsupported_sql_feature"
    assert exc.value.details["unsupported_sql_feature"] == ["LIMIT"]


def test_compatibility_matrix_sql_execute_confirmation_required(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_execute"](statement="update demo_assets set status='active'")

    assert exc.value.details["reason_code"] == "missing_write_confirmation"


def test_compatibility_matrix_connector_confirmation_required(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](payload={"connectorName": "demo_input"})

    assert exc.value.details["reason_code"] == "missing_write_confirmation"


def test_compatibility_matrix_connector_payload_required(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_output"](payload={})

    assert exc.value.details["reason_code"] == "invalid_arguments"
    assert exc.value.details["received_args"]["payload"] == {}


def test_compatibility_matrix_conflicting_alias_values(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](
            statement="select * from demo_assets",
            sql="select * from demo_other",
        )

    assert exc.value.details["reason_code"] == "invalid_arguments"
