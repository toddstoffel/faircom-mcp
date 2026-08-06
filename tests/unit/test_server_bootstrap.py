from __future__ import annotations

import pytest

from faircom_mcp import __version__
from faircom_mcp.config import AppConfig
from faircom_mcp.errors import ValidationFailure
from tests.helpers.http import get as _get
from tests.helpers.server_harness import create_test_config as _config
from tests.helpers.server_harness import load_server_module as _load_server_module


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
    connector_calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTables:
        def list_tables(
            self,
            name_like: str | None = None,
            *,
            database: str | None = None,
        ) -> dict[str, object]:
            list_table_calls.append(name_like)
            return {"tables": [], "name_like": name_like, "database": database}

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
            continuation_token: str | None = None,
            order_by: str | None = None,
        ) -> dict[str, object]:
            sql_query_page_calls.append((statement, params, page, page_size))
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
            sql_execute_calls.append((statement, params))
            if dry_run:
                return {
                    "dry_run": True,
                    "statement": statement,
                    "params": params,
                    "preview": "Write statement would execute",
                    "preview_details": {
                        "target_table": "demo",
                        "operation": "DELETE",
                        "scoped_by_where": True,
                        "row_estimate": "unknown",
                    },
                    "sample_results": {"before": [{"id": 1}], "after": []},
                }
            return {"statement": statement, "params": params}

    class FakeConnectors:
        def list_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
            connector_calls.append(("listInputs", payload))
            return {"action": "listInputs", "payload": payload}

        def describe_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
            connector_calls.append(("describeInputs", payload))
            return {"action": "describeInputs", "payload": payload}

        def create_input(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("createInput", payload))
            return {"action": "createInput", "payload": payload}

        def alter_input(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("alterInput", payload))
            return {"action": "alterInput", "payload": payload}

        def delete_input(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("deleteInput", payload))
            return {"action": "deleteInput", "payload": payload}

        def list_outputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
            connector_calls.append(("listOutputs", payload))
            return {"action": "listOutputs", "payload": payload}

        def describe_outputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
            connector_calls.append(("describeOutputs", payload))
            return {"action": "describeOutputs", "payload": payload}

        def create_output(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("createOutput", payload))
            return {"action": "createOutput", "payload": payload}

        def alter_output(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("alterOutput", payload))
            return {"action": "alterOutput", "payload": payload}

        def delete_output(self, payload: dict[str, object]) -> dict[str, object]:
            connector_calls.append(("deleteOutput", payload))
            return {"action": "deleteOutput", "payload": payload}

    class FakeClient:
        pass

    def client_factory(received: AppConfig) -> object:
        captured.append(received)
        return FakeClient()

    original_table_adapter = server_module.TableAdapter
    original_connector_adapter = server_module.ConnectorAdapter
    original_sql_adapter = server_module.SQLAdapter
    server_module.TableAdapter = lambda _client: FakeTables()
    server_module.ConnectorAdapter = lambda _client: FakeConnectors()
    server_module.SQLAdapter = lambda _client, **_kwargs: FakeSQL()
    try:
        server = server_module.create_server(config, client_factory=client_factory)
    finally:
        server_module.TableAdapter = original_table_adapter
        server_module.ConnectorAdapter = original_connector_adapter
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
        "list_inputs",
        "listInputs",
        "describe_inputs",
        "describeInputs",
        "create_input",
        "createInput",
        "alter_input",
        "alterInput",
        "delete_input",
        "deleteInput",
        "list_outputs",
        "listOutputs",
        "describe_outputs",
        "describeOutputs",
        "create_output",
        "createOutput",
        "alter_output",
        "alterOutput",
        "delete_output",
        "deleteOutput",
        "sql_query",
        "sql_query_page",
        "get_usage_contract",
        "describe_connector_schema",
        "validate_connector_payloads",
        "runtime_status",
        "capabilities_summary",
        "observability_metrics",
        "observability_audit",
        "observability_health",
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
        "database": None,
    }
    assert server.tools["list_tables"](table_like="foo", database="faircom") == {
        "tables": [],
        "name_like": "foo",
        "database": "faircom",
        "compatibility": {
            "tool_name": "list_tables",
            "normalized_args": [{"from": "table_like", "to": "name_like"}],
            "metadata": {
                "database": {
                    "received": "faircom",
                    "applied": False,
                    "reason": "database_scoping_not_supported_by_backend_adapter",
                },
            },
        },
    }
    assert server.tools["describe_table"](table="demo") == {
        "table_name": "demo",
        "columns": [],
        "compatibility": {
            "tool_name": "describe_table",
            "normalized_args": [{"from": "table", "to": "table_name"}],
            "metadata": {},
        },
    }
    assert server.tools["list_table_columns"](table="demo") == {
        "table_name": "demo",
        "columns": [{"name": "id"}],
        "column_count": 1,
        "compatibility": {
            "tool_name": "list_table_columns",
            "normalized_args": [{"from": "table", "to": "table_name"}],
            "metadata": {},
        },
    }
    assert server.tools["list_table_indexes"](table="demo") == {
        "table_name": "demo",
        "indexes": [{"name": "pk_demo"}],
        "index_count": 1,
        "compatibility": {
            "tool_name": "list_table_indexes",
            "normalized_args": [{"from": "table", "to": "table_name"}],
            "metadata": {},
        },
    }

    assert server.tools["list_inputs"](payload={"connectorNameLike": "modbus%"}) == {
        "action": "listInputs",
        "payload": {"connectorNameLike": "modbus%"},
    }
    assert server.tools["describe_inputs"](payload={"connectorNames": ["modbus_1"]}) == {
        "action": "describeInputs",
        "payload": {"connectorNames": ["modbus_1"]},
    }
    assert server.tools["listInputs"](payload={"connectorNameLike": "modbus%"}) == {
        "action": "listInputs",
        "payload": {"connectorNameLike": "modbus%"},
    }
    assert server.tools["describeInputs"](payload={"connectorNames": ["modbus_1"]}) == {
        "action": "describeInputs",
        "payload": {"connectorNames": ["modbus_1"]},
    }
    assert server.tools["create_input"](
        payload={"connectorName": "modbus_1"},
        confirm_write=True,
    ) == {
        "action": "createInput",
        "payload": {"connectorName": "modbus_1", "inputName": "modbus_1"},
        "dry_run_applied": False,
        "confirm_write_required": True,
        "mutation_applied": True,
    }
    assert server.tools["createInput"](
        payload={"connectorName": "modbus_2"},
        confirm_write=True,
    ) == {
        "action": "createInput",
        "payload": {"connectorName": "modbus_2", "inputName": "modbus_2"},
        "dry_run_applied": False,
        "confirm_write_required": True,
        "mutation_applied": True,
    }
    assert server.tools["list_outputs"](payload={"connectorNameLike": "mqtt%"}) == {
        "action": "listOutputs",
        "payload": {"connectorNameLike": "mqtt%"},
    }
    assert server.tools["describe_outputs"](payload={"connectorNames": ["mqtt_1"]}) == {
        "action": "describeOutputs",
        "payload": {"connectorNames": ["mqtt_1"]},
    }
    assert server.tools["listOutputs"](payload={"connectorNameLike": "mqtt%"}) == {
        "action": "listOutputs",
        "payload": {"connectorNameLike": "mqtt%"},
    }
    assert server.tools["describeOutputs"](payload={"connectorNames": ["mqtt_1"]}) == {
        "action": "describeOutputs",
        "payload": {"connectorNames": ["mqtt_1"]},
    }
    assert server.tools["create_output"](
        payload={"connectorName": "mqtt_1"},
        dry_run=True,
    ) == {
        "mode": "dry_run",
        "status": "success",
        "tool_name": "create_output",
        "action": "createOutput",
        "payload": {"connectorName": "mqtt_1"},
        "would_succeed": "unvalidated",
        "preview": "Connector change preview only",
        "preview_details": {
            "action": "createOutput",
            "target": {"connectorName": "mqtt_1"},
            "row_estimate": "unknown",
            "upstream_validated": False,
            "schema_validated": False,
            "schema_status": "unvalidated",
            "schema_service": None,
        },
        "warnings": [
            "Dry run is a local preview only and does not call FairCom backend APIs.",
            (
                "Dry run did not run full schema validation because no local schema profile "
                "matched this payload."
            ),
        ],
        "hint": (
            "Review the preview above. Then run list_inputs/describe_inputs to verify upstream "
            "connectivity before calling with confirm_write=True."
        ),
    }
    assert server.tools["createOutput"](
        payload={"connectorName": "mqtt_2"},
        dry_run=True,
    ) == {
        "mode": "dry_run",
        "status": "success",
        "tool_name": "create_output",
        "action": "createOutput",
        "payload": {"connectorName": "mqtt_2"},
        "would_succeed": "unvalidated",
        "preview": "Connector change preview only",
        "preview_details": {
            "action": "createOutput",
            "target": {"connectorName": "mqtt_2"},
            "row_estimate": "unknown",
            "upstream_validated": False,
            "schema_validated": False,
            "schema_status": "unvalidated",
            "schema_service": None,
        },
        "warnings": [
            "Dry run is a local preview only and does not call FairCom backend APIs.",
            (
                "Dry run did not run full schema validation because no local schema profile "
                "matched this payload."
            ),
        ],
        "hint": (
            "Review the preview above. Then run list_inputs/describe_inputs to verify upstream "
            "connectivity before calling with confirm_write=True."
        ),
    }

    assert server.tools["sql_query"](
        sql="select * from demo",
        params=[1, "two"],
    ) == {
        "statement": "select * from demo",
        "params": [1, "two"],
        "compatibility": {
            "tool_name": "sql_query",
            "normalized_args": [{"from": "sql", "to": "statement"}],
            "metadata": {},
        },
    }
    assert server.tools["sql_query_page"](
        query="select * from demo order by id",
        params=["active"],
        page=3,
        page_size=50,
    ) == {
        "statement": "select * from demo order by id",
        "params": ["active"],
        "page": 3,
        "page_size": 50,
        "continuation_token": None,
        "order_by": None,
        "compatibility": {
            "tool_name": "sql_query_page",
            "normalized_args": [{"from": "query", "to": "statement"}],
            "metadata": {},
        },
    }
    usage_contract = server.tools["get_usage_contract"]()
    assert usage_contract["contract_version"] == "2026-08-05"
    assert usage_contract["supported_aliases"]["sql_query"]["sql"] == "statement"
    assert usage_contract["canonical_arg_keys"]["validate_connector_payloads"] == [
        "action",
        "payload",
        "payloads",
    ]
    create_input_example = usage_contract["minimal_payload_examples"]["create_input"]["arguments"][
        "payload"
    ]
    create_output_example = usage_contract["minimal_payload_examples"]["create_output"][
        "arguments"
    ]["payload"]
    assert create_input_example["inputName"] == "modbus_energy_input"
    assert create_output_example["serviceName"] == "mqtt"
    assert usage_contract["example_validity"]["create_input"] == "complete"
    assert usage_contract["example_validity"]["create_output"] == "complete"
    assert "modbus" in usage_contract["connector_payload_profiles"]
    assert "mqtt" in usage_contract["connector_payload_profiles"]
    assert "required_keys" in usage_contract["connector_payload_profiles"]["modbus"]
    assert "enum_values" in usage_contract["connector_payload_profiles"]["modbus"]
    assert "schema" in usage_contract["connector_payload_profiles"]["modbus"]
    schema_profile = server.tools["describe_connector_schema"](service_name="modbus")
    assert schema_profile["service_name"] == "modbus"
    assert schema_profile["schema_version"] == "2026-08-05"
    assert "propertyMapList" in schema_profile["schema"]["required"]
    preflight = server.tools["validate_connector_payloads"](
        action="createInput",
        payload={
            "connectorName": "modbus_energy_input",
            "serviceName": "modbus",
            "modbusServer": "tcp://127.0.0.1:502",
            "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
        },
    )
    assert preflight["mode"] == "preflight"
    assert preflight["summary"]["all_valid"] is True
    assert server.tools["runtime_status"]() == {
        "service": "faircom-mcp",
        "tool_group_allowlist": ["metadata", "query", "write", "connector", "admin", "diagnostics"],
        "metrics_enabled": True,
        "tracing_enabled": False,
    }
    capabilities = server.tools["capabilities_summary"]()
    assert capabilities["service"]["name"] == "faircom-mcp"
    assert capabilities["service"]["version"] == __version__
    assert capabilities["security"]["default_policy"] == "default"
    assert capabilities["security"]["read_write_enabled"] is True
    assert capabilities["security"]["diagnostics_enabled"] is False
    assert capabilities["transport_modes"][0]["name"] == "http"
    assert any(
        tool["name"] == "sql_execute" and tool["risk_level"] == "critical"
        for tool in capabilities["tools"]
    )
    assert any(
        tool["name"] == "sql_query_page" and tool["idempotent"] is True
        for tool in capabilities["tools"]
    )
    assert any(
        tool["name"] == "create_input" and tool.get("aliases") == ["createInput"]
        for tool in capabilities["tools"]
    )
    assert any(
        tool["name"] == "describe_connector_schema" and tool["group"] == "admin"
        for tool in capabilities["tools"]
    )
    assert any(
        tool["name"] == "validate_connector_payloads" and tool["group"] == "admin"
        for tool in capabilities["tools"]
    )
    metrics_payload = server.tools["observability_metrics"]()
    assert metrics_payload["service"] == "faircom-mcp"
    assert isinstance(metrics_payload["tool_calls"], dict)
    assert isinstance(metrics_payload["tool_seconds_total"], dict)
    assert isinstance(metrics_payload["compatibility_events"], dict)
    assert metrics_payload["compatibility_events"]["sql_query:alias_normalized"] >= 1
    audit_payload = server.tools["observability_audit"]()
    assert audit_payload["service"] == "faircom-mcp"
    assert [event["type"] for event in audit_payload["events"]] == [
        "connector_write_attempt",
        "connector_write_attempt",
        "connector_write_attempt",
        "connector_write_attempt",
    ]
    assert {event["details"]["tool"] for event in audit_payload["events"]} == {
        "create_input",
        "create_output",
    }
    assert server.tools["observability_health"]() == {
        "service": "faircom-mcp",
        "status": "ok",
        "details": {
            "metrics_enabled": True,
            "tracing_enabled": False,
            "diagnostics_enabled": False,
        },
    }
    try:
        server.tools["sql_execute"](statement="update demo set flag = 1")
    except ValidationFailure as exc:
        assert exc.details["tool_name"] == "sql_execute"
        assert exc.details["reason_code"] == "missing_write_confirmation"
    else:  # pragma: no cover - defensive
        raise AssertionError("sql_execute should require explicit confirmation")

    assert server.tools["sql_execute"](
        statement="update demo set flag = 1",
        params=["x"],
        confirm_write=True,
    ) == {
        "statement": "update demo set flag = 1",
        "params": ["x"],
        "dry_run_applied": False,
        "confirm_write_required": True,
        "mutation_applied": True,
    }
    assert server.tools["sql_execute"](
        statement="delete from demo where id = 1",
        params=["x"],
        dry_run=True,
    ) == {
        "dry_run": True,
        "statement": "delete from demo where id = 1",
        "params": ["x"],
        "preview": "Write statement would execute",
        "preview_details": {
            "target_table": "demo",
            "operation": "DELETE",
            "scoped_by_where": True,
            "row_estimate": "unknown",
        },
        "sample_results": {"before": [{"id": 1}], "after": []},
        "dry_run_applied": True,
        "confirm_write_required": True,
        "mutation_applied": False,
    }
    metrics_response = _get("/metrics", app)
    assert metrics_response.status_code == 200
    assert "faircom_mcp_tool_calls_total" in metrics_response.text
    assert "faircom_mcp_compatibility_events_total" in metrics_response.text

    assert list_table_calls == [None, "foo"]
    assert describe_table_calls == ["demo"]
    assert list_table_columns_calls == ["demo"]
    assert list_table_indexes_calls == ["demo"]
    assert connector_calls == [
        ("listInputs", {"connectorNameLike": "modbus%"}),
        ("describeInputs", {"connectorNames": ["modbus_1"]}),
        ("listInputs", {"connectorNameLike": "modbus%"}),
        ("describeInputs", {"connectorNames": ["modbus_1"]}),
        ("createInput", {"connectorName": "modbus_1", "inputName": "modbus_1"}),
        ("createInput", {"connectorName": "modbus_2", "inputName": "modbus_2"}),
        ("listOutputs", {"connectorNameLike": "mqtt%"}),
        ("describeOutputs", {"connectorNames": ["mqtt_1"]}),
        ("listOutputs", {"connectorNameLike": "mqtt%"}),
        ("describeOutputs", {"connectorNames": ["mqtt_1"]}),
    ]
    assert sql_query_calls == [("select * from demo", [1, "two"])]
    assert sql_query_page_calls == [("select * from demo order by id", ["active"], 3, 50)]
    assert sql_execute_calls == [
        ("update demo set flag = 1", ["x"]),
        ("delete from demo where id = 1", ["x"]),
    ]

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](statement="select * from demo limit 1")

    assert exc.value.details["tool_name"] == "sql_query"
    assert exc.value.details["reason_code"] == "unsupported_sql_feature"
    assert exc.value.details["unsupported_sql_feature"] == ["LIMIT"]
    assert "TOP" in exc.value.details["suggested_fix"]

    metrics_payload = server.tools["observability_metrics"]()
    assert metrics_payload["compatibility_events"]["sql_query:unsupported_sql_feature"] >= 1

    with pytest.raises(ValidationFailure) as write_exc:
        server.tools["sql_execute"](statement="update demo set flag = 1")

    assert write_exc.value.details["reason_code"] == "missing_write_confirmation"
    metrics_payload_after = server.tools["observability_metrics"]()
    assert metrics_payload_after["compatibility_events"]["sql_execute:invalid_arg_name"] >= 1
