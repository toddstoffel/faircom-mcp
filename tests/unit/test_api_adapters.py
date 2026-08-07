from __future__ import annotations

from typing import Any, cast

from faircom_mcp.api.connectors import ConnectorAdapter
from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.errors import ValidationFailure
from faircom_mcp.security import SqlStatementPolicy


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def post_action(
        self,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((action, payload))
        return {"action": action, "payload": payload}

    def admin_action(
        self,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((f"admin:{action}", payload))
        return {"action": action, "payload": payload}

    def hub_action(
        self,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((f"hub:{action}", payload))
        return {"action": action, "payload": payload}


def test_table_adapter_calls_expected_actions() -> None:
    client = StubClient()
    adapter = TableAdapter(cast(Any, client))

    tables_result = adapter.list_tables("cust%")
    describe_result = adapter.describe_table("customers")

    assert tables_result["action"] == "listTables"
    assert tables_result["payload"] is None
    assert describe_result["action"] == "describeTables"
    assert describe_result["payload"] == {"tableNames": ["customers"]}
    assert client.calls == [
        ("listTables", None),
        ("describeTables", {"tableNames": ["customers"]}),
    ]
    assert tables_result["filter"] == {
        "name_like": "cust%",
        "applied": False,
        "source_count": 0,
        "matched_count": 0,
        "unknown_name_count": 0,
        "reason": "unsupported_list_tables_response_shape",
    }


def test_table_adapter_applies_local_name_like_filter() -> None:
    client = StubClient()
    adapter = TableAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "tables": [
            {"tableName": "customers"},
            {"tableName": "orders"},
            {"tableName": "cust_archive"},
        ],
    }

    filtered = adapter.list_tables("cust%")

    assert [table["tableName"] for table in filtered["tables"]] == [
        "customers",
        "cust_archive",
    ]
    assert filtered["filter"] == {
        "name_like": "cust%",
        "applied": True,
        "source_count": 3,
        "matched_count": 2,
        "unknown_name_count": 0,
        "reason": "local_like_filter",
    }


def test_connector_adapter_calls_expected_hub_actions() -> None:
    client = StubClient()
    adapter = ConnectorAdapter(cast(Any, client))

    inputs = adapter.list_inputs({"connectorNameLike": "modbus%"})
    input_detail = adapter.describe_inputs({"connectorNames": ["modbus_1"]})
    create_input = adapter.create_input({"connectorName": "modbus_1"})
    alter_input = adapter.alter_input({"connectorName": "modbus_1"})
    delete_input = adapter.delete_input({"connectorName": "modbus_1"})
    outputs = adapter.list_outputs({"connectorNameLike": "mqtt%"})
    output_detail = adapter.describe_outputs({"connectorNames": ["mqtt_1"]})
    create_output = adapter.create_output({"connectorName": "mqtt_1"})
    alter_output = adapter.alter_output({"connectorName": "mqtt_1"})
    delete_output = adapter.delete_output({"connectorName": "mqtt_1"})
    transforms = adapter.list_transforms({"connectorNameLike": "xform%"})
    transform_detail = adapter.describe_transforms({"connectorNames": ["xform_1"]})
    create_transform = adapter.create_transform({"connectorName": "xform_1"})
    alter_transform = adapter.alter_transform({"connectorName": "xform_1"})
    delete_transform = adapter.delete_transform({"connectorName": "xform_1"})

    assert inputs["action"] == "listInputs"
    assert input_detail["action"] == "describeInputs"
    assert create_input["action"] == "createInput"
    assert alter_input["action"] == "alterInput"
    assert delete_input["action"] == "deleteInput"
    assert outputs["action"] == "listOutputs"
    assert output_detail["action"] == "describeOutputs"
    assert create_output["action"] == "createOutput"
    assert alter_output["action"] == "alterOutput"
    assert delete_output["action"] == "deleteOutput"
    assert transforms["action"] == "listTransforms"
    assert transform_detail["action"] == "describeTransforms"
    assert create_transform["action"] == "createTransform"
    assert alter_transform["action"] == "alterTransform"
    assert delete_transform["action"] == "deleteTransform"
    assert client.calls == [
        ("hub:listInputs", {"inputNameLike": "modbus%"}),
        ("hub:describeInputs", {"inputNames": ["modbus_1"]}),
        ("hub:createInput", {"inputName": "modbus_1"}),
        ("hub:alterInput", {"inputName": "modbus_1"}),
        ("hub:deleteInput", {"inputName": "modbus_1"}),
        ("hub:listOutputs", {"connectorNameLike": "mqtt%"}),
        ("hub:describeOutputs", {"connectorNames": ["mqtt_1"]}),
        ("hub:createOutput", {"connectorName": "mqtt_1"}),
        ("hub:alterOutput", {"connectorName": "mqtt_1"}),
        ("hub:deleteOutput", {"connectorName": "mqtt_1"}),
        ("hub:listTransforms", {"connectorNameLike": "xform%"}),
        ("hub:describeTransforms", {"connectorNames": ["xform_1"]}),
        ("hub:createTransform", {"connectorName": "xform_1"}),
        ("hub:alterTransform", {"connectorName": "xform_1"}),
        ("hub:deleteTransform", {"connectorName": "xform_1"}),
    ]


def test_connector_adapter_translates_modbus_input_payload_to_settings_shape() -> None:
    client = StubClient()
    adapter = ConnectorAdapter(cast(Any, client))

    create_input = adapter.create_input(
        {
            "connectorName": "modbus_1",
            "serviceName": "modbus",
            "thingName": "PLC 74",
            "tableName": "modbusTableTCP",
            "transformName": "normalize_energy_data",
            "dataCollectionIntervalMilliseconds": 500,
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "propertyMapList": [
                {
                    "propertyPath": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                    "modbusUnitId": 5,
                    "modbusDataLen": 1,
                }
            ],
        }
    )

    assert create_input["action"] == "createInput"
    assert client.calls == [
        (
            "hub:createInput",
            {
                "inputName": "modbus_1",
                "serviceName": "modbus",
                "thingName": "PLC 74",
                "tableName": "modbusTableTCP",
                "transformName": "normalize_energy_data",
                "dataCollectionIntervalMilliseconds": 500,
                "settings": {
                    "transformName": "normalize_energy_data",
                    "dataCollectionIntervalMilliseconds": 500,
                    "modbusProtocol": "TCP",
                    "modbusServer": "127.0.0.1",
                    "modbusServerPort": 502,
                    "propertyMapList": [
                        {
                            "propertyPath": "temperature",
                            "modbusDataAccess": "holdingregister",
                            "modbusDataAddress": 1199,
                            "modbusUnitId": 5,
                            "modbusDataLen": 1,
                        }
                    ],
                },
            },
        )
    ]


def test_connector_adapter_prefers_explicit_top_level_modbus_over_existing_settings() -> None:
    client = StubClient()
    adapter = ConnectorAdapter(cast(Any, client))

    adapter.alter_input(
        {
            "inputName": "modbus_1",
            "serviceName": "modbus",
            "transformName": "new_transform",
            "dataCollectionIntervalMilliseconds": 1500,
            "settings": {
                "transformName": "old_transform",
                "dataCollectionIntervalMilliseconds": 500,
            },
        }
    )

    assert client.calls == [
        (
            "hub:alterInput",
            {
                "inputName": "modbus_1",
                "serviceName": "modbus",
                "transformName": "new_transform",
                "dataCollectionIntervalMilliseconds": 1500,
                "settings": {
                    "transformName": "new_transform",
                    "dataCollectionIntervalMilliseconds": 1500,
                },
            },
        )
    ]


def test_table_adapter_extracts_columns_and_indexes() -> None:
    client = StubClient()
    adapter = TableAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "columns": [{"name": "id"}, {"name": "name"}],
        "indexes": [{"name": "pk_customers"}],
    }

    columns = adapter.list_table_columns("customers")
    indexes = adapter.list_table_indexes("customers")

    assert columns == {
        "table_name": "customers",
        "columns": [{"name": "id"}, {"name": "name"}],
        "column_count": 2,
    }
    assert indexes == {
        "table_name": "customers",
        "indexes": [{"name": "pk_customers"}],
        "index_count": 1,
    }


def test_sql_adapter_calls_expected_actions() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    query_result = adapter.query("SELECT * FROM customers WHERE id = ?", [123])
    exec_result = adapter.execute("DELETE FROM customers WHERE id = ?", [123])

    assert query_result["action"] == "getRecordsUsingSQL"
    assert query_result["payload"] == {
        "sql": "SELECT * FROM customers WHERE id = ?",
        "sqlParams": [{"name": "p1", "value": 123}],
    }
    assert exec_result["action"] == "runSQLStatements"
    assert exec_result["payload"] == {
        "sqlStatements": ["DELETE FROM customers WHERE id = ?"],
        "inParams": [{"name": "p1", "value": 123}],
    }


def test_sql_adapter_returns_dry_run_preview_without_executing() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    result = adapter.execute(
        "UPDATE customers SET status='active' WHERE id = 1",
        dry_run=True,
    )

    assert result["mode"] == "dry_run"
    assert result["status"] == "success"
    assert result["statement_type"] == "UPDATE"
    assert result["would_succeed"] is True
    assert result["changes"]["updated_rows"] == 1
    assert result["preview"] == "Write statement would execute"
    assert result["preview_details"]["target_table"] == "customers"
    assert result["preview_details"]["operation"] == "UPDATE"
    assert result["sample_results"]["before"]
    assert result["sample_results"]["after"]
    assert client.calls == []


def test_sql_adapter_paginated_query_calls_expected_action() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    # Simulate a page with exactly page_size rows so metadata should indicate another page.
    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "result": {"data": [{"id": 1}, {"id": 2}], "moreRecords": False},
    }

    page_result = adapter.query_page(
        "SELECT * FROM customers ORDER BY id",
        ["active"],
        page=2,
        page_size=250,
    )

    assert page_result["action"] == "getRecordsUsingSQL"
    assert page_result["payload"] == {
        "sql": "SELECT * FROM customers ORDER BY id",
        "sqlParams": [{"name": "p1", "value": "active"}],
        "skipRecords": 250,
        "maxRecords": 250,
    }
    assert page_result["has_more"] is False
    assert page_result["next_page"] is None
    assert page_result["next_cursor"] is None


def test_sql_adapter_paginated_query_adds_cursor_metadata() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "result": {"data": [{"id": 1}, {"id": 2}, {"id": 3}], "moreRecords": True},
    }

    page_result = adapter.query_page(
        "SELECT * FROM customers ORDER BY id",
        page=1,
        page_size=3,
    )

    assert page_result["has_more"] is True
    assert page_result["next_page"] == 2
    assert page_result["next_cursor"] == 2


def test_sql_adapter_generates_and_accepts_continuation_tokens() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "result": {"data": [{"id": 1}], "moreRecords": True},
    }

    first_page = adapter.query_page("SELECT * FROM customers", page=1, page_size=1)
    assert first_page["page"]["offset"] == 0
    assert first_page["page"]["limit"] == 1
    assert first_page["page"]["has_more"] is True
    assert first_page["continuation"]["token"] is not None

    second_page = adapter.query_page(
        "SELECT * FROM customers",
        continuation_token=first_page["continuation"]["token"],
        page_size=1,
    )

    assert second_page["page"]["offset"] == 1
    assert second_page["payload"]["skipRecords"] == 1


def test_sql_adapter_injects_order_by_when_missing() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    adapter.query_page("SELECT * FROM customers", page=1, page_size=1)

    assert client.calls[-1][1]["sql"] == "SELECT * FROM customers ORDER BY 1"


def test_sql_adapter_paginated_query_validates_paging_inputs() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    try:
        adapter.query_page("SELECT 1", page=0)
    except ValidationFailure as exc:
        assert exc.details == {"page": 0}
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected page validation failure")

    try:
        adapter.query_page("SELECT 1", page_size=0)
    except ValidationFailure as exc:
        assert exc.details == {"page_size": 0}
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected page_size validation failure")


def test_sql_adapter_enforces_policy_before_request() -> None:
    client = StubClient()
    adapter = SQLAdapter(
        cast(Any, client),
        policy=SqlStatementPolicy(allowlist=("SELECT",), denylist=("DROP",)),
    )

    result = adapter.query("SELECT * FROM customers")

    assert result["action"] == "getRecordsUsingSQL"
    assert client.calls == [("getRecordsUsingSQL", {"sql": "SELECT * FROM customers"})]
