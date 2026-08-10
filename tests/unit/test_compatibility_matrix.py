from __future__ import annotations

import pytest

from faircom_mcp.errors import UpstreamAPIError, ValidationFailure
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

    def alter_input(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("alterInput", payload))
        return {"action": "alterInput", "payload": payload}

    def describe_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append(("describeInputs", payload or {}))
        return {
            "inputs": [
                {
                    "inputName": "modbus_input",
                    "settings": {
                        "dataCollectionIntervalMilliseconds": 5000,
                    },
                }
            ]
        }


class _AlterInputEventuallyApplies:
    def __init__(self) -> None:
        self.describe_calls = 0

    def alter_input(self, payload: dict[str, object]) -> dict[str, object]:
        _ = payload
        return {"errorCode": 0, "errorMessage": "", "result": {"ok": True}}

    def describe_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        _ = payload
        self.describe_calls += 1
        interval = 5000 if self.describe_calls == 1 else 15000
        return {
            "inputs": [
                {
                    "inputName": "modbus_input",
                    "settings": {
                        "tableName": "modbus_energy_raw",
                        "dataCollectionIntervalMilliseconds": interval,
                    },
                }
            ]
        }


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


class _ReadinessCheckProbe:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        import time

        self.calls += 1
        time.sleep(2.5)
        return True


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


def test_compatibility_matrix_connector_validation_rejection_is_logged(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_CaptureConnectors(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    with pytest.raises(ValidationFailure):
        server.tools["create_input"](
            payload={"serviceName": "modbus"},
            confirm_write=True,
        )

    audit = server.tools["observability_audit"]()["events"]
    assert any(
        event["type"] == "connector_write_attempt" and event["details"]["tool"] == "create_input"
        for event in audit
    )
    assert any(
        event["type"] == "connector_write_result"
        and event["details"]["tool"] == "create_input"
        and event["details"]["outcome"] == "rejected"
        for event in audit
    )


def test_compatibility_matrix_connector_write_nonzero_error_code_raises(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CreateReturnsErrorPayload:
        def create_input(self, payload: dict[str, object]) -> dict[str, object]:
            _ = payload
            return {
                "errorCode": 12048,
                "errorMessage": "Service is inactive",
            }

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_CreateReturnsErrorPayload(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    with pytest.raises(UpstreamAPIError) as exc:
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

    assert exc.value.details["errorCode"] == 12048


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
        },
        confirm_write=True,
    )

    assert result["action"] == "deleteInput"


def test_compatibility_matrix_delete_integration_tables_preflight_valid(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="deleteIntegrationTables",
        payload={"tableNames": ["normalize_energy_data"]},
    )

    assert result["summary"]["all_valid"] is True
    assert result["results"][0]["status"] == "valid"


def test_compatibility_matrix_describe_inputs_includes_runtime_service_state(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _ClientWithServiceState:
        def admin_action(
            self,
            action: str,
            payload: dict[str, object] | None = None,
        ) -> dict[str, object]:
            _ = payload
            if action != "listServices":
                return {"action": action}
            return {
                "services": [
                    {
                        "serviceName": "modbus",
                        "active": False,
                        "status": "stopped",
                    }
                ]
            }

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_DescribeInputsWithSettings(),
    ):
        server = server_module.create_server(
            _config(),
            client_factory=lambda _config: _ClientWithServiceState(),
        )

    result = server.tools["describe_inputs"]()

    assert result["inputs"][0]["serviceName"] == "modbus"
    assert result["inputs"][0]["enabled"] is False
    assert result["inputs"][0]["description"] == "Boiler room telemetry"
    assert result["inputs"][0]["runtime_service_state"]["active"] is False
    assert result["inputs"][0]["runtime_service_state"]["service_name"] == "modbus"


def test_compatibility_matrix_alter_input_adds_recovery_for_inactive_service(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _AlterFailsForInactiveService:
        def alter_input(self, payload: dict[str, object]) -> dict[str, object]:
            raise UpstreamAPIError(
                "Connector service inactive",
                details={
                    "errorCode": 12048,
                    "errorMessage": "The requested service is not active",
                    "payload": payload,
                },
            )

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_AlterFailsForInactiveService(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    with pytest.raises(UpstreamAPIError) as exc:
        server.tools["alter_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "mqtt",
            },
            confirm_write=True,
        )

    recovery = exc.value.details.get("recovery")
    assert isinstance(recovery, dict)
    assert recovery["reason_code"] == "service_inactive"


def test_compatibility_matrix_alter_input_detects_non_applied_mutation(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _AlterReturnsSuccessButDoesNotApply:
        def alter_input(self, payload: dict[str, object]) -> dict[str, object]:
            _ = payload
            return {"errorCode": 0, "errorMessage": "", "result": {"ok": True}}

        def describe_inputs(self, payload: dict[str, object] | None = None) -> dict[str, object]:
            _ = payload
            return {
                "inputs": [
                    {
                        "inputName": "modbus_input",
                        "settings": {
                            "dataCollectionIntervalMilliseconds": 5000,
                        },
                    }
                ]
            }

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_AlterReturnsSuccessButDoesNotApply(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    with pytest.raises(UpstreamAPIError) as exc:
        server.tools["alter_input"](
            payload={
                "connectorName": "modbus_input",
                "serviceName": "modbus",
                "tableName": "modbus_energy_raw",
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "dataCollectionIntervalMilliseconds": 15000,
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

    assert exc.value.details["reason_code"] == "mutation_not_applied"
    assert any(
        item["field"] == "dataCollectionIntervalMilliseconds"
        for item in exc.value.details["mismatches"]
    )


def test_compatibility_matrix_manage_service_requires_control_field(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["manage_service"](
            payload={
                "serviceName": "modbus",
                "totallyBogusField": 12345,
            },
            dry_run=True,
        )

    assert exc.value.details["tool_name"] == "manage_service"
    assert "contains unsupported fields" in exc.value.message


def test_compatibility_matrix_validate_connector_payloads_supports_manage_service_alias(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="manage_service",
        payload={"serviceName": "modbus", "command": "pause"},
    )

    assert result["summary"]["all_valid"] is True
    assert result["results"][0]["action"] == "manageService"
    assert result["results"][0]["forwarded_payload"]["command"] == "pause"


def test_compatibility_matrix_validate_connector_payloads_supports_manage_service(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="manageService",
        payload={"serviceName": "modbus", "command": "pause"},
    )

    assert result["summary"]["all_valid"] is True
    assert result["results"][0]["status"] == "valid"
    assert result["results"][0]["forwarded_payload"]["command"] == "pause"


def test_compatibility_matrix_alter_input_polls_until_commit_visible(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _AlterInputEventuallyApplies()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["alter_input"](
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "dataCollectionIntervalMilliseconds": 15000,
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

    assert result["mutation_applied"] is True
    assert result["mutation_verification"]["status"] == "verified"
    assert result["mutation_verification"]["attempts"] >= 2


def test_compatibility_matrix_observability_health_times_out_on_slow_readiness(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    def _slow_readiness_check() -> bool:
        import time

        time.sleep(2.5)
        return True

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
    ):
        server = server_module.create_server(
            _config(),
            client_factory=lambda _config: object(),
            readiness_check=_slow_readiness_check,
        )

    result = server.tools["observability_health"]()

    assert result["status"] == "degraded"
    assert result["details"]["readiness"]["status"] == "timeout"


def test_compatibility_matrix_validate_connector_payloads_rejects_invalid_manage_service(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="manageService",
        payload={"serviceName": "modbus", "totallyBogusField": 12345},
    )

    assert result["summary"]["all_valid"] is False
    assert result["results"][0]["status"] == "invalid"


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
    assert any(
        event["type"] == "code_package_write_result"
        and event["details"]["tool"] == "register_code_package"
        and event["details"]["code_name"] == "syntax_probe"
        and event["details"]["outcome"] == "rejected"
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
    assert "enabled" not in first_input["settings"]
    assert "description" not in first_input["settings"]


def test_compatibility_matrix_register_code_package_alters_existing_package(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CaptureCodePackageConnector:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def describe_code_packages(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("describeCodePackages", payload))
            return {"result": {"data": [{"codeName": "decode_mixing_tank"}]}}

        def create_code_package(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("createCodePackage", payload))
            return {"result": {}, "errorCode": 0, "errorMessage": ""}

        def alter_code_package(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("alterCodePackage", payload))
            return {"result": {}, "errorCode": 0, "errorMessage": ""}

    connector_adapter = _CaptureCodePackageConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    server.tools["register_code_package"](
        code_name="decode_mixing_tank",
        code="function transform(row){ return row; }",
        confirm_write=True,
    )

    action_names = [action for action, _payload in connector_adapter.calls]
    assert action_names == ["describeCodePackages", "alterCodePackage"]
    _action, alter_payload = connector_adapter.calls[-1]
    assert alter_payload["codeName"] == "decode_mixing_tank"
    assert alter_payload["codeType"] == "integrationTableTransform"


class _ServiceRegistryClient:
    def __init__(self, services: list[dict[str, object]]) -> None:
        self._services = services

    def admin_action(
        self,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = payload
        if action != "listServices":
            return {"action": action}
        return {"services": self._services}


def test_compatibility_matrix_create_input_rejects_unregistered_service(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_CaptureConnectors(),
    ):
        server = server_module.create_server(
            _config(),
            client_factory=lambda _config: _ServiceRegistryClient(
                [{"serviceName": "modbus", "active": True}]
            ),
        )

    with pytest.raises(ValidationFailure) as exc:
        server.tools["create_input"](
            payload={
                "connectorName": "opcua_input",
                "serviceName": "opcua",
                "tableName": "opcua_raw",
            },
            confirm_write=True,
        )

    validation_errors = exc.value.details["received_args"]["validation_errors"]
    unregistered_issue = next(
        issue for issue in validation_errors if issue["reason"] == "unregistered_service"
    )
    assert unregistered_issue["registered_services"] == ["modbus"]


def test_compatibility_matrix_validate_connector_payloads_warns_disabled_service(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_CaptureConnectors(),
    ):
        server = server_module.create_server(
            _config(),
            client_factory=lambda _config: _ServiceRegistryClient(
                [{"serviceName": "modbus", "active": False}]
            ),
        )

    result = server.tools["validate_connector_payloads"](
        action="createInput",
        payload={
            "connectorName": "modbus_input",
            "serviceName": "modbus",
            "tableName": "modbus_raw",
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

    assert result["results"][0]["status"] == "valid"
    assert any("disabled" in warning for warning in result["results"][0]["warnings"])


def test_compatibility_matrix_describe_connector_schema_removes_mqtt(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["describe_connector_schema"](service_name="mqtt")

    assert exc.value.details["received_args"]["service_name"] == "mqtt"
    assert "mqtt" not in exc.value.details["expected_args"]["service_name"]


def test_compatibility_matrix_describe_connector_schema_output_direction(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    schema_profile = server.tools["describe_connector_schema"](
        service_name="modbus",
        direction="output",
    )

    assert schema_profile["direction"] == "output"
    assert "outputName" in schema_profile["schema"]["required"]
    assert "sourceFields" in schema_profile["schema"]["required"]


def test_compatibility_matrix_create_output_preflight_nests_settings(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    preview = server.tools["create_output"](
        payload={
            "outputName": "writeTemperatureToModbus",
            "serviceName": "modbus",
            "tableName": "modbusTableTCP",
            "sourceFields": ["source_payload"],
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
        },
        dry_run=True,
    )

    forwarded = preview["forwarded_payload"]
    assert "settings" in forwarded
    assert forwarded["settings"]["modbusProtocol"] == "TCP"
    assert "modbusProtocol" not in forwarded


def test_compatibility_matrix_validate_connector_payloads_flags_missing_output_fields(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    result = server.tools["validate_connector_payloads"](
        action="createOutput",
        payload={"outputName": "writeTemperatureToModbus", "serviceName": "modbus"},
    )

    assert result["results"][0]["status"] == "invalid"
    error_paths = {issue["path"] for issue in result["results"][0]["errors"]}
    assert "payload.tableName" in error_paths
    assert "payload.sourceFields" in error_paths


class _TopicConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self._configured: dict[str, object] | None = None

    def configure_topic(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("configureTopic", payload))
        self._configured = dict(payload)
        return {"errorCode": 0, "errorMessage": "", "result": {"ok": True}}

    def describe_topics(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append(("describeTopics", payload))
        if self._configured is None:
            return {"topics": []}
        return {"topics": [dict(self._configured)]}

    def delete_topic(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("deleteTopic", payload))
        return {"errorCode": 0, "errorMessage": ""}

    def list_topics(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append(("listTopics", payload))
        return {"topics": ["factory/line-1/mixing_tank/temperature"]}


def test_compatibility_matrix_configure_topic_requires_confirm_write(
    monkeypatch: object,
) -> None:
    server = _make_server(monkeypatch)

    with pytest.raises(ValidationFailure) as exc:
        server.tools["configure_topic"](
            payload={
                "topic": "factory/line-1/mixing_tank/temperature",
                "databaseName": "faircom",
                "tableName": "modbus_mixing_tank_temp",
            },
        )

    assert exc.value.details["reason_code"] == "missing_write_confirmation"


def test_compatibility_matrix_configure_topic_dry_run_preview(monkeypatch: object) -> None:
    server = _make_server(monkeypatch)

    preview = server.tools["configure_topic"](
        payload={
            "topic": "factory/line-1/mixing_tank/temperature",
            "databaseName": "faircom",
            "tableName": "modbus_mixing_tank_temp",
        },
        dry_run=True,
    )

    assert preview["execution_status"] == "not_executed"
    assert "upsert" in preview["preview"].lower()


def test_compatibility_matrix_configure_topic_verifies_mutation(monkeypatch: object) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _TopicConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["configure_topic"](
        payload={
            "topic": "factory/line-1/mixing_tank/temperature",
            "databaseName": "faircom",
            "tableName": "modbus_mixing_tank_temp",
        },
        confirm_write=True,
    )

    assert result["mutation_applied"] is True
    assert result["mutation_verification"]["status"] == "verified"
    action_names = [action for action, _payload in connector_adapter.calls]
    assert action_names == ["configureTopic", "describeTopics"]


def test_compatibility_matrix_list_and_describe_topics(monkeypatch: object) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)
    connector_adapter = _TopicConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    topics = server.tools["list_topics"]()
    assert topics["topics"] == ["factory/line-1/mixing_tank/temperature"]

    described = server.tools["describe_topics"](
        payload={"topics": ["factory/line-1/mixing_tank/temperature"]}
    )
    assert described == {"topics": []}


def test_compatibility_matrix_register_code_package_warns_missing_wizard_metadata(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CaptureCodePackageConnector:
        def describe_code_packages(self, payload: dict[str, object]) -> dict[str, object]:
            _ = payload
            return {"result": {"data": []}}

        def create_code_package(self, payload: dict[str, object]) -> dict[str, object]:
            _ = payload
            return {"result": {}, "errorCode": 0, "errorMessage": ""}

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=_CaptureCodePackageConnector(),
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["register_code_package"](
        code_name="decode_mixing_tank",
        code="record.value = record.source_payload.value;",
        confirm_write=True,
    )

    assert any("inputFields" in warning for warning in result["warnings"])
    assert any("outputFieldDefinitions" in warning for warning in result["warnings"])


def test_compatibility_matrix_register_code_package_sets_wizard_metadata(
    monkeypatch: object,
) -> None:
    _fake_class, server_module = load_server_module(monkeypatch)

    class _CaptureCodePackageConnector:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def describe_code_packages(self, payload: dict[str, object]) -> dict[str, object]:
            _ = payload
            return {"result": {"data": []}}

        def create_code_package(self, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append(("createCodePackage", payload))
            return {"result": {}, "errorCode": 0, "errorMessage": ""}

    connector_adapter = _CaptureCodePackageConnector()

    with patched_adapters(
        server_module,
        table_adapter=BasicFakeTables(),
        sql_adapter=BasicFakeSQL(),
        connector_adapter=connector_adapter,
    ):
        server = server_module.create_server(_config(), client_factory=lambda _config: object())

    result = server.tools["register_code_package"](
        code_name="decode_mixing_tank",
        code="record.temperature_c = record.source_payload.value;",
        input_fields=["source_payload.value"],
        output_field_definitions=[{"name": "temperature_c", "type": "double"}],
        confirm_write=True,
    )

    assert result["warnings"] == []
    _action, payload = connector_adapter.calls[-1]
    assert payload["metadata"]["inputFields"] == ["source_payload.value"]
    assert payload["metadata"]["outputFieldDefinitions"] == [
        {"name": "temperature_c", "type": "double"}
    ]
