from __future__ import annotations

import pytest

from faircom_mcp.errors import ValidationFailure
from tests.helpers.server_harness import (
    BasicFakeSQL,
    BasicFakeTables,
    load_server_module,
    patched_adapters,
)
from tests.helpers.server_harness import (
    create_test_config as _config,
)


def _make_server(monkeypatch: object) -> object:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())
    return server


class _CaptureConnectors:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def create_input(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("createInput", payload))
        return {"action": "createInput", "payload": payload}

    def delete_input(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("deleteInput", payload))
        return {"action": "deleteInput", "payload": payload}


class _DescribeInputsWithSettings:
    def list_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        return {"inputs": [], "payload": payload}

    def describe_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        _ = payload
        return {
            "inputs": [
                {
                    "inputName": "modbus_energy_input",
                    "serviceName": "modbus",
                    "settings": {
                        "enabled": False,
                        "description": "Boiler room telemetry",
                    },
                }
            ]
        }


class _CaptureCodePackageSql(BasicFakeSQL):
    def __init__(self) -> None:
        self.query_statements: list[str] = []
        self.execute_statements: list[str] = []

    def query(self, statement: str, params: list[object] | None = None) -> dict[str, object]:
        _ = params
        self.query_statements.append(statement)
        if "SELECT TOP 1 id FROM codepackage_name" in statement:
            return {"result": {"data": [{"id": 9}]}}
        if "SELECT TOP 1 codepackage_id, version FROM codepackage" in statement:
            return {"result": {"data": [{"codepackage_id": 9, "version": 1}]}}
        return {"result": {"data": []}}

    def execute(
        self,
        statement: str,
        params: list[object] | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        _ = params
        _ = dry_run
        self.execute_statements.append(statement)
        return {"ok": True}


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


def test_compatibility_matrix_validate_connector_payloads_rejects_unknown_action(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["validate_connector_payloads"](
            action="createTransformX",
            payload={
                "transformName": "inline_decode_asset01",
                "serviceName": "javascript",
                "transformActions": [
                    {
                        "transformService": "v8TransformService",
                        "inputFields": ["*"],
                        "transformStepMethod": "javascript",
                        "outputFields": ["*"],
                        "transformParams": {
                            "script": "function transform(row){ return row; }",
                        },
                    }
                ],
            },
        )

    assert exc.value.details["tool_name"] == "validate_connector_payloads"
    assert exc.value.details["reason_code"] == "invalid_arguments"
    assert "unsupported action" in exc.value.message.lower()


def test_compatibility_matrix_modbus_schema_validation_on_write(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
            },
            confirm_write=True,
        )

    assert exc.value.details["reason_code"] == "invalid_arguments"
    assert any(
        issue["path"] == "payload.propertyMapList"
        for issue in exc.value.details["received_args"]["validation_errors"]
    )


def test_compatibility_matrix_modbus_requires_table_name(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "propertyMapList": [
                    {
                        "propertyName": "temperature",
                        "modbusDataAccess": "holdingregister",
                        "modbusDataAddress": 1199,
                    }
                ],
            },
            confirm_write=True,
        )

    assert any(
        issue["path"] == "payload.tableName"
        for issue in exc.value.details["received_args"]["validation_errors"]
    )


def test_compatibility_matrix_modbus_dry_run_invalid_preview(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["create_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
        },
        dry_run=True,
    )

    assert result["status"] == "invalid"
    assert result["schema_outcome"] == "invalid"
    assert result["execution_status"] == "not_executed"
    issue = next(
        issue for issue in result["validation_errors"] if issue["path"] == "payload.propertyMapList"
    )
    assert issue["json_pointer"] == "/payload/propertyMapList"
    assert result["preview_details"]["forwarded_payload"]["settings"]["modbusServer"] == "127.0.0.1"


def test_compatibility_matrix_preserves_modbus_mapping_fields_on_write(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _CaptureConnectors()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["create_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "unitId": 7,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                    "modbusDataType": "float32ABCD",
                    "modbusDataLen": 2,
                    "scale": 0.1,
                }
            ],
        },
        confirm_write=True,
    )

    assert result["action"] == "createInput"
    assert connector_adapter.calls
    _action, forwarded_payload = connector_adapter.calls[-1]
    forwarded_property_map = forwarded_payload["propertyMapList"][0]

    # In this test we patch a fake connector adapter directly, so we validate
    # server-side normalization (not adapter-side request reshaping).
    assert forwarded_payload["connectorName"] == "modbus_input"
    assert forwarded_payload["inputName"] == "modbus_input"
    assert forwarded_property_map["propertyName"] == "temperature"
    assert forwarded_property_map["propertyPath"] == "temperature"
    assert forwarded_property_map["modbusDataAddress"] == 1199
    assert forwarded_property_map["modbusUnitId"] == 7
    assert forwarded_property_map["modbusConvertToFloat"] == "divideByInteger"
    assert forwarded_property_map["modbusDivisor"] == 10


def test_compatibility_matrix_modbus_requires_address_and_property_target(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    assert any(
        issue["path"].endswith(".modbusDataAddress") and issue["reason"] == "required"
        for issue in validation_errors
    )
    assert any(
        issue["path"] == "payload.propertyMapList[0]" and issue["reason"] == "required"
        for issue in validation_errors
    )


def test_compatibility_matrix_enum_required_includes_allowed_values(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
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
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
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


def test_compatibility_matrix_modbus_delete_requires_only_identity(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _CaptureConnectors()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["delete_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
        },
        confirm_write=True,
    )

    assert result["action"] == "deleteInput"


def test_compatibility_matrix_modbus_unknown_key_is_passed_through(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)
    result = server.tools["validate_connector_payloads"](
        action="createInput",
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                }
            ],
            "modbusServerPoort": 502,
        },
    )

    assert result["summary"]["all_valid"] is True
    warning = result["results"][0]["warnings"][0]
    assert "modbusServerPoort" in warning
    assert "modbusServerPort" in warning


def test_compatibility_matrix_validation_errors_include_corrected_snippet(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "enabled": True,
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    property_map_issue = next(
        issue for issue in validation_errors if issue["path"] == "payload.propertyMapList"
    )

    assert property_map_issue["corrected_snippet"] == {
        "serviceName": "modbus",
        "tableName": "modbus_energy_raw",
        "modbusProtocol": "TCP",
        "modbusServer": "127.0.0.1",
        "modbusServerPort": 502,
        "propertyMapList": [
            {
                "propertyName": "temperature",
                "modbusDataAccess": "holdingregister",
                "modbusDataAddress": 1199,
            }
        ],
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
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                }
            ],
        },
    )

    assert result["mode"] == "preflight"
    assert result["summary"] == {"total": 1, "valid": 1, "invalid": 0, "all_valid": True}
    assert result["results"][0]["status"] == "valid"
    assert result["results"][0]["normalized_payload"]["inputName"] == "modbus_input"
    assert result["results"][0]["forwarded_payload"]["settings"]["modbusServer"] == "127.0.0.1"


def test_compatibility_matrix_connector_preflight_batch_mixed(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createInput",
        payloads=[
            {
                "connectorName": "modbus_valid",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "propertyMapList": [
                    {
                        "propertyName": "temperature",
                        "modbusDataAccess": "holdingregister",
                        "modbusDataAddress": 1199,
                    }
                ],
            },
            {
                "connectorName": "modbus_invalid",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "propertyMapList": [
                    {
                        "propertyName": "temperature",
                        "modbusDataAccess": "holding_register",
                        "modbusDataAddress": 1199,
                    }
                ],
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


def test_compatibility_matrix_transform_methods_require_output_fields_and_mapping(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createTransform",
        payload={
            "transformName": "typed_asset01",
            "serviceName": "javascript",
            "transformActions": [
                {
                    "inputFields": ["source_payload"],
                    "transformStepMethod": "jsonToDifferentTableFields",
                    "transformParams": {},
                }
            ],
        },
    )

    assert result["summary"]["all_valid"] is False
    issues = result["results"][0]["errors"]
    assert any(issue["path"].endswith(".outputFields") for issue in issues)
    assert any(
        issue["path"].endswith(".transformParams.mapOfPropertiesToFields") for issue in issues
    )


def test_compatibility_matrix_transform_preflight_accepts_inline_script(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createTransform",
        payload={
            "transformName": "inline_decode_asset01",
            "serviceName": "javascript",
            "transformActions": [
                {
                    "transformService": "v8TransformService",
                    "inputFields": ["*"],
                    "transformStepMethod": "javascript",
                    "outputFields": ["*"],
                    "transformParams": {
                        "script": "function transform(row){ return row; }",
                    },
                }
            ],
        },
    )

    assert result["summary"]["all_valid"] is True
    normalized_params = result["results"][0]["normalized_payload"]["transformActions"][0][
        "transformParams"
    ]
    assert normalized_params["codeType"] == "integrationTableTransform"


def test_compatibility_matrix_transform_preflight_rejects_unknown_code_type(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createTransform",
        payload={
            "transformName": "bad_type_asset01",
            "serviceName": "javascript",
            "transformActions": [
                {
                    "transformService": "v8TransformService",
                    "inputFields": ["*"],
                    "transformStepMethod": "javascript",
                    "outputFields": ["*"],
                    "transformParams": {
                        "codeName": "decode_mixing_tank",
                        "codeType": "badType",
                    },
                }
            ],
        },
    )

    assert result["summary"]["all_valid"] is False
    issues = result["results"][0]["errors"]
    code_type_issue = next(
        issue for issue in issues if issue["path"].endswith(".transformParams.codeType")
    )
    assert code_type_issue["reason"] == "invalid_enum"
    assert "integrationTableTransform" in code_type_issue["allowed_values"]


def test_compatibility_matrix_modbus_divisor_sets_divide_by_integer(monkeypatch: object) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _CaptureConnectors()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["create_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                    "modbusDataType": "int16SignedAB",
                    "modbusDivisor": 10,
                }
            ],
        },
        confirm_write=True,
    )

    assert result["action"] == "createInput"
    _action, forwarded_payload = connector_adapter.calls[-1]
    forwarded_property_map = forwarded_payload["propertyMapList"][0]
    assert forwarded_property_map["modbusConvertToFloat"] == "divideByInteger"
    assert forwarded_property_map["modbusDivisor"] == 10
    assert forwarded_property_map["modbusRegisterType"] == "int16SignedAB"


def test_compatibility_matrix_transform_action_name_alias_normalized(monkeypatch: object) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CaptureTransformConnector:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def create_transform(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("createTransform", payload))
            return {"action": "createTransform", "payload": payload}

    connector_adapter = _CaptureTransformConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["create_transform"](
        payload={
            "transformName": "typed_asset01",
            "serviceName": "javascript",
            "transformActions": [
                {
                    "inputFields": ["source_payload"],
                    "transformActionName": "jsonToTableFields",
                    "outputFields": ["*"],
                    "transformParams": {
                        "mapOfPropertiesToFields": [
                            {
                                "recordPath": "source_payload.temperature",
                                "fieldName": "temperature",
                            }
                        ]
                    },
                }
            ],
        },
        confirm_write=True,
    )

    assert result["action"] == "createTransform"
    _action, forwarded_payload = connector_adapter.calls[-1]
    step_method = forwarded_payload["transformActions"][0]["transformStepMethod"]
    assert step_method == "jsonToTableFields"


def test_compatibility_matrix_transform_service_moved_to_action_scope(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CaptureTransformConnector:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def create_transform(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("createTransform", payload))
            return {"action": "createTransform", "payload": payload}

    connector_adapter = _CaptureTransformConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    server.tools["create_transform"](
        payload={
            "transformName": "decode_asset01",
            "serviceName": "javascript",
            "transformService": "v8TransformService",
            "transformActions": [
                {
                    "inputFields": ["*"],
                    "transformStepMethod": "javascript",
                    "outputFields": ["*"],
                    "transformParams": {"codeName": "decode_mixing_tank"},
                }
            ],
        },
        confirm_write=True,
    )

    _action, forwarded_payload = connector_adapter.calls[-1]
    assert "transformService" not in forwarded_payload
    assert forwarded_payload["transformActions"][0]["transformService"] == "v8TransformService"


def test_compatibility_matrix_tool_results_strip_auth_token(monkeypatch: object) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _TokenEchoConnector:
        def list_inputs(self, _payload: dict[str, object] | None = None) -> dict[str, object]:
            return {
                "authToken": "session-token",
                "inputs": [{"name": "modbus_1", "authToken": "nested-token"}],
            }

    connector_adapter = _TokenEchoConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["list_inputs"]()
    assert "authToken" not in result
    assert "authToken" not in result["inputs"][0]


def test_compatibility_matrix_audit_records_outcome_target_and_timestamp(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _CaptureConnectors()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    server.tools["create_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAccess": "holdingregister",
                    "modbusDataAddress": 1199,
                }
            ],
        },
        confirm_write=True,
    )

    audit = server.tools["observability_audit"]()["events"]
    attempt = next(event for event in audit if event["type"] == "connector_write_attempt")
    result = next(event for event in audit if event["type"] == "connector_write_result")

    assert "timestamp" in attempt
    assert attempt["details"]["action"] == "createInput"
    assert attempt["details"]["target"] == "modbus_input"
    assert result["details"]["outcome"] == "success"
    assert result["details"]["target"] == "modbus_input"


def test_compatibility_matrix_observability_records_code_package_write_attempt(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["register_code_package"](
        code_name="decode_mixing_tank",
        code="function transform(row){ return row; }",
        dry_run=True,
    )

    assert result["status"] == "success"
    audit = server.tools["observability_audit"]()["events"]
    assert any(
        event["type"] == "code_package_write_attempt"
        and event["details"]["tool"] == "register_code_package"
        and event["details"]["code_name"] == "decode_mixing_tank"
        for event in audit
    )


def test_compatibility_matrix_observability_records_rejected_code_package_attempt(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["register_code_package"](
            code_name="syntax_probe",
            code="function transform(row) { return row ",
            confirm_write=True,
        )

    assert "JavaScript syntax validation failed" in exc.value.message
    received_args = exc.value.details["received_args"]
    assert received_args["code_name"] == "syntax_probe"
    assert "parser_message" in received_args
    assert isinstance(received_args.get("line"), int)

    audit = server.tools["observability_audit"]()["events"]
    assert any(
        event["type"] == "code_package_write_attempt"
        and event["details"]["tool"] == "register_code_package"
        and event["details"]["code_name"] == "syntax_probe"
        and event["details"]["confirm_write"] is True
        for event in audit
    )


def test_compatibility_matrix_describe_inputs_lifts_enabled_and_description(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_DescribeInputsWithSettings(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["describe_inputs"](payload={"connectorNames": ["modbus_energy_input"]})
    first_input = result["inputs"][0]
    assert first_input["enabled"] is False
    assert first_input["description"] == "Boiler room telemetry"


def test_compatibility_matrix_codepackage_history_status_matches_inactive_rows(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    sql_adapter = _CaptureCodePackageSql()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=sql_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["register_code_package"](
        code_name="decode_mixing_tank",
        code="function transform(row){ return row; }",
        confirm_write=True,
    )

    assert result["result"]["version"] == 2
    assert any(
        "UPDATE codepackage_history SET active = 0, status = 'inactive' WHERE codepackage_id = 9"
        in statement
        for statement in sql_adapter.execute_statements
    )
