from __future__ import annotations

import pytest

from faircom_mcp.errors import ValidationFailure
from tests.helpers.server_harness import BasicFakeSQL
from tests.helpers.server_harness import BasicFakeTables
from tests.helpers.server_harness import create_test_config as _config
from tests.helpers.server_harness import load_server_module
from tests.helpers.server_harness import patched_adapters


def _make_server(monkeypatch: object) -> object:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())
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


def test_compatibility_matrix_modbus_schema_validation_on_write(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
            },
            confirm_write=True,
        )

    assert exc.value.details["reason_code"] == "invalid_arguments"
    assert any(
        issue["path"] == "payload.propertyMapList"
        for issue in exc.value.details["received_args"]["validation_errors"]
    )


def test_compatibility_matrix_modbus_dry_run_invalid_preview(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["create_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "modbusServer": "tcp://127.0.0.1:502",
        },
        dry_run=True,
    )

    assert result["status"] == "invalid"
    assert result["would_succeed"] is False
    issue = next(
        issue for issue in result["validation_errors"] if issue["path"] == "payload.propertyMapList"
    )
    assert issue["json_pointer"] == "/payload/propertyMapList"


def test_compatibility_matrix_rejects_unknown_top_level_connector_keys(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
                "enabled": True,
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    unknown_issue = next(issue for issue in validation_errors if issue["reason"] == "unknown_keys")
    assert unknown_issue["path"] == "payload"
    assert "enabled" in unknown_issue["unknown_keys"]
    assert "connectorName" in unknown_issue["suggested_known_keys"]


def test_compatibility_matrix_rejects_unknown_modbus_property_map_keys(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [
                    {
                        "modbusDataAccess": "holdingregister",
                        "offset": 10,
                    }
                ],
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    unknown_issue = next(issue for issue in validation_errors if issue["reason"] == "unknown_keys")
    assert unknown_issue["path"] == "payload.propertyMapList[0]"
    assert unknown_issue["json_pointer"] == "/payload/propertyMapList/0"
    assert "offset" in unknown_issue["unknown_keys"]
    assert "modbusDataAccess" in unknown_issue["suggested_known_keys"]


def test_compatibility_matrix_enum_required_includes_allowed_values(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [{}],
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    enum_issue = next(
        issue
        for issue in validation_errors
        if issue["path"].endswith(".modbusDataAccess") and issue["reason"] == "required"
    )
    assert enum_issue["allowed_values"] == [
        "holdingregister",
        "inputregister",
        "coil",
        "discreteinput",
    ]


def test_compatibility_matrix_enum_invalid_includes_allowed_values_and_nearest_match(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [{"modbusDataAccess": "holding_register"}],
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    enum_issue = next(
        issue
        for issue in validation_errors
        if issue["path"].endswith(".modbusDataAccess") and issue["reason"] == "invalid_enum"
    )
    assert enum_issue["json_pointer"] == "/payload/propertyMapList/0/modbusDataAccess"
    assert enum_issue["allowed_values"] == [
        "holdingregister",
        "inputregister",
        "coil",
        "discreteinput",
    ]
    assert enum_issue["received"] == "holding_register"
    assert enum_issue["nearest_match"] == "holdingregister"
    assert enum_issue["corrected_snippet"] == {
        "propertyMapList": [{"modbusDataAccess": "holdingregister"}]
    }


def test_compatibility_matrix_validation_errors_include_corrected_snippet(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "enabled": True,
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    unknown_issue = next(issue for issue in validation_errors if issue["reason"] == "unknown_keys")
    property_map_issue = next(
        issue for issue in validation_errors if issue["path"] == "payload.propertyMapList"
    )

    assert unknown_issue["corrected_snippet"] == {
        "connectorName": "modbus_input",
        "inputName": "modbus_input",
        "serviceName": "modbus",
        "modbusServer": "tcp://127.0.0.1:502",
    }
    assert property_map_issue["corrected_snippet"] == {
        "serviceName": "modbus",
        "modbusServer": "tcp://127.0.0.1:502",
        "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
    }


def test_compatibility_matrix_conflicting_alias_values(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["sql_query"](
            statement="select * from demo_assets",
            sql="select * from demo_other",
        )

    assert exc.value.details["reason_code"] == "invalid_arguments"


def test_compatibility_matrix_connector_preflight_single_valid(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createInput",
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "modbusServer": "tcp://127.0.0.1:502",
            "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
        },
    )

    assert result["mode"] == "preflight"
    assert result["summary"] == {"total": 1, "valid": 1, "invalid": 0, "all_valid": True}
    assert result["results"][0]["status"] == "valid"
    assert result["results"][0]["normalized_payload"]["inputName"] == "modbus_input"


def test_compatibility_matrix_connector_preflight_batch_mixed(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createInput",
        payloads=[
            {
                "connectorName": "modbus_valid",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
            },
            {
                "connectorName": "modbus_invalid",
                "serviceName": "modbus",
                "modbusServer": "tcp://127.0.0.1:502",
                "propertyMapList": [{"modbusDataAccess": "holding_register"}],
            },
        ],
    )

    assert result["summary"] == {"total": 2, "valid": 1, "invalid": 1, "all_valid": False}
    assert result["results"][0]["status"] == "valid"
    assert result["results"][1]["status"] == "invalid"
    enum_issue = next(
        issue
        for issue in result["results"][1]["errors"]
        if issue["path"].endswith(".modbusDataAccess")
    )
    assert enum_issue["reason"] == "invalid_enum"
    assert enum_issue["allowed_values"] == [
        "holdingregister",
        "inputregister",
        "coil",
        "discreteinput",
    ]
