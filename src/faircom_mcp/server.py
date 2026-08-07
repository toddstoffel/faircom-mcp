from __future__ import annotations

import difflib
import html
import json
import logging
import re
import time
import types
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal, Required, TypedDict, cast

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from faircom_mcp import __version__
from faircom_mcp.api.client import FaircomAPIClient, create_client
from faircom_mcp.api.connectors import ConnectorAdapter, transform_connector_request
from faircom_mcp.api.dialect import detect_unsupported_features, normalize_select_first_to_top
from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.config import AppConfig, load_config
from faircom_mcp.errors import FaircomError, ValidationFailure
from faircom_mcp.observability import AuditLog, RuntimeMetrics, build_tracer, maybe_span

_MODBUS_DATA_ACCESS_ENUM = [
    "holdingregister",
    "inputregister",
    "coil",
    "discreteinput",
]

_MODBUS_DATA_TYPE_ENUM = [
    "int16SignedAB",
    "int16UnsignedAB",
    "int32SignedABCD",
    "int32UnsignedABCD",
    "float32ABCD",
    "bitBoolean",
]

_MODBUS_ALLOWED_PAYLOAD_KEYS = {
    "connectorName",
    "inputName",
    "serviceName",
    "modbusServer",
    "modbusProtocol",
    "modbusServerPort",
    "modbusDataAddressType",
    "thingName",
    "tableName",
    "enabled",
    "description",
    "unitId",
    "transformName",
    "disableTransformSteps",
    "dataCollectionIntervalMilliseconds",
    "propertyMapList",
    "settings",
}

_MODBUS_ALLOWED_PROPERTY_MAP_KEYS = {
    "propertyName",
    "propertyPath",
    "tagName",
    "tagId",
    "modbusDataAccess",
    "modbusDataAddress",
    "modbusDataType",
    "modbusRegisterType",
    "modbusDataLen",
    "modbusUnitId",
    "modbusConvertToFloat",
    "modbusDivisor",
    "modbusDecimalDigits",
    "bitStartPosition",
    "scale",
}


class ModbusPropertyMapItem(TypedDict, total=False):
    modbusDataAccess: Required[Literal["holdingregister", "inputregister", "coil", "discreteinput"]]
    propertyName: str
    propertyPath: str
    modbusDataAddress: int
    modbusDataType: str
    modbusRegisterType: str
    modbusDataLen: int | float
    modbusUnitId: int
    modbusConvertToFloat: str
    modbusDivisor: int
    modbusDecimalDigits: int
    bitStartPosition: int
    scale: int | float


class ModbusConnectorPayload(TypedDict, total=False):
    connectorName: Required[str]
    serviceName: Required[Literal["modbus"]]
    modbusServer: Required[str]
    modbusProtocol: str
    modbusServerPort: int
    modbusDataAddressType: str
    thingName: str
    tableName: str
    enabled: bool
    description: str
    unitId: int
    transformName: str
    disableTransformSteps: bool
    dataCollectionIntervalMilliseconds: int
    propertyMapList: Required[list[ModbusPropertyMapItem]]
    inputName: str


class MqttConnectorPayload(TypedDict, total=False):
    connectorName: Required[str]
    serviceName: Required[Literal["mqtt"]]
    inputName: str
    topic: str


class GenericConnectorPayload(TypedDict, total=False):
    connectorName: Required[str]
    inputName: str
    serviceName: str


ConnectorPayload = ModbusConnectorPayload | MqttConnectorPayload | GenericConnectorPayload
ConnectorPayloadBatch = list[ConnectorPayload]

_CONNECTOR_SCHEMA_REGISTRY: dict[str, dict[str, object]] = {
    "modbus": {
        "service_name": "modbus",
        "required": [
            "connectorName",
            "serviceName",
            "modbusProtocol",
            "modbusServer",
            "modbusServerPort",
            "propertyMapList",
        ],
        "description": "Modbus connector payload schema (local validation profile).",
        "properties": {
            "connectorName": {"type": "string", "minLength": 1},
            "inputName": {"type": "string", "minLength": 1},
            "serviceName": {"type": "string", "const": "modbus"},
            "modbusServer": {"type": "string", "minLength": 1},
            "modbusProtocol": {"type": "string"},
            "modbusServerPort": {"type": ["integer", "number"]},
            "modbusDataAddressType": {"type": "string"},
            "thingName": {"type": "string", "minLength": 1},
            "tableName": {"type": "string", "minLength": 1},
            "enabled": {"type": "boolean"},
            "description": {"type": "string"},
            "unitId": {"type": ["integer", "number"]},
            "transformName": {"type": "string", "minLength": 1},
            "disableTransformSteps": {"type": "boolean"},
            "dataCollectionIntervalMilliseconds": {"type": ["integer", "number"]},
            "propertyMapList": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["modbusDataAccess"],
                    "properties": {
                        "propertyName": {"type": "string", "minLength": 1},
                        "propertyPath": {"type": "string", "minLength": 1},
                        "modbusDataAccess": {
                            "type": "string",
                            "enum": _MODBUS_DATA_ACCESS_ENUM,
                        },
                        "modbusDataAddress": {"type": ["integer", "number"]},
                        "modbusDataType": {
                            "type": "string",
                            "enum": _MODBUS_DATA_TYPE_ENUM,
                        },
                        "modbusRegisterType": {
                            "type": "string",
                            "enum": _MODBUS_DATA_TYPE_ENUM,
                        },
                        "modbusDataLen": {"type": ["integer", "number"]},
                        "modbusUnitId": {"type": ["integer", "number"]},
                        "modbusConvertToFloat": {"type": "string"},
                        "modbusDivisor": {"type": ["integer", "number"]},
                        "modbusDecimalDigits": {"type": ["integer", "number"]},
                        "bitStartPosition": {"type": ["integer", "number"]},
                        "scale": {"type": ["integer", "number"]},
                    },
                },
            },
        },
        "example": {
            "connectorName": "modbus_energy_input",
            "inputName": "modbus_energy_input",
            "serviceName": "modbus",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "unitId": 1,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAddress": 1199,
                    "modbusDataAccess": "holdingregister",
                    "modbusDataType": "int16SignedAB",
                    "modbusDataLen": 2,
                }
            ],
        },
    },
    "mqtt": {
        "service_name": "mqtt",
        "required": ["connectorName", "serviceName"],
        "description": "MQTT connector payload baseline schema (local validation profile).",
        "properties": {
            "connectorName": {"type": "string", "minLength": 1},
            "serviceName": {"type": "string", "const": "mqtt"},
        },
        "example": {
            "connectorName": "mqtt_telemetry_input",
            "serviceName": "mqtt",
            "topic": "factory/line-1/telemetry",
        },
    },
    "javascript": {
        "service_name": "javascript",
        "required": ["transformName", "serviceName", "transformActions"],
        "description": "JavaScript transform payload profile for Edge transform actions.",
        "properties": {
            "transformName": {"type": "string", "minLength": 1},
            "serviceName": {"type": "string", "const": "javascript"},
            "description": {"type": "string"},
            "transformService": {"type": "string", "const": "v8TransformService"},
            "transformActions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "inputFields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "transformStepMethod": {"type": "string", "minLength": 1},
                        # Deprecated spelling accepted by Edge for compatibility.
                        "transformActionName": {"type": "string", "minLength": 1},
                        "outputFields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "transformParams": {"type": "object"},
                    },
                },
            },
        },
        "example": {
            "transformName": "add_asset_id",
            "serviceName": "javascript",
            "transformService": "v8TransformService",
            "transformActions": [
                {
                    "inputFields": ["*"],
                    "transformStepMethod": "javascript",
                    "outputFields": ["*"],
                    "transformParams": {
                        "script": "payload.asset_id = 'asset-001'; return payload;",
                    },
                }
            ],
        },
    },
}


def create_server(
    config: AppConfig | None = None,
    *,
    client_factory: Callable[[AppConfig], FaircomAPIClient] = create_client,
    readiness_check: Callable[[], bool] | None = None,
) -> FastMCP:
    resolved_config = config or load_config()
    client = client_factory(resolved_config)
    table_adapter = TableAdapter(client)
    connector_adapter = ConnectorAdapter(client)
    sql_adapter = SQLAdapter(client, policy=resolved_config.security.to_sql_policy())
    tool_group_policy = resolved_config.security.to_tool_group_policy()
    metrics = RuntimeMetrics()
    audit_log = AuditLog()
    tracer = build_tracer(enabled=resolved_config.observability.enable_tracing)
    logger = logging.getLogger("faircom_mcp")

    server = FastMCP("faircom-mcp")
    # Export runtime metrics for transport-layer compatibility wrappers.
    if not hasattr(server, "state"):
        server.state = types.SimpleNamespace()  # type: ignore[attr-defined]
    server.state.runtime_metrics = metrics  # type: ignore[attr-defined]

    def _run_tool(tool_name: str, group: str, action: Callable[[], object]) -> object:
        try:
            tool_group_policy.validate(group)
        except Exception:
            audit_log.record(
                event_type="policy_denial",
                details={"tool": tool_name, "group": group},
            )
            raise
        started = time.perf_counter()
        try:
            with maybe_span(
                tracer,
                f"tool.{tool_name}",
                {
                    "tool.name": tool_name,
                    "tool.group": group,
                },
            ):
                result = action()
        except Exception as exc:
            metrics.record_tool_call(
                tool=tool_name,
                status="error",
                duration_seconds=time.perf_counter() - started,
            )
            if isinstance(exc, FaircomError):
                logger.error(
                    "Tool failed with normalized FaircomError",
                    extra={
                        "tool": tool_name,
                        "group": group,
                        "error_code": str(exc.code),
                        "error_message": exc.message,
                        "retryable": exc.retryable,
                        "error_details": exc.details,
                    },
                )
            else:
                logger.exception(
                    "Tool failed with unexpected exception",
                    extra={"tool": tool_name, "group": group},
                )
            raise

        metrics.record_tool_call(
            tool=tool_name,
            status="success",
            duration_seconds=time.perf_counter() - started,
        )
        return result

    def _validation_failure(
        *,
        tool_name: str,
        message: str,
        expected_args: dict[str, object],
        received_args: dict[str, object],
        suggested_fix: str,
        example_payload: dict[str, object],
        reason_code: str = "invalid_arguments",
    ) -> ValidationFailure:
        if reason_code == "unsupported_sql_feature":
            metrics.record_compat_event(tool=tool_name, event="unsupported_sql_feature")
        elif reason_code in {"invalid_arguments", "missing_write_confirmation"}:
            metrics.record_compat_event(tool=tool_name, event="invalid_arg_name")

        return ValidationFailure(
            message,
            details={
                "error_code": "validation_error",
                "reason_code": reason_code,
                "tool_name": tool_name,
                "expected_args": expected_args,
                "received_args": received_args,
                "suggested_fix": suggested_fix,
                "example_payload": example_payload,
            },
        )

    def _with_compatibility(
        result: object,
        *,
        tool_name: str,
        normalized_args: list[dict[str, str]] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> object:
        if not normalized_args and not metadata:
            return result
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        for _entry in normalized_args or []:
            metrics.record_compat_event(tool=tool_name, event="alias_normalized")
        payload["compatibility"] = {
            "tool_name": tool_name,
            "normalized_args": normalized_args or [],
            "metadata": metadata or {},
        }
        return payload

    def _connector_preview(
        *,
        tool_name: str,
        action: str,
        payload: ConnectorPayload | None,
    ) -> dict[str, object]:
        validation = _validate_connector_schema(
            tool_name=tool_name,
            action=action,
            payload=payload,
        )
        validation_warnings = cast(list[str], validation.get("warnings", []))
        if validation["status"] == "invalid":
            return {
                "mode": "dry_run",
                "status": "invalid",
                "tool_name": tool_name,
                "action": action,
                "payload": payload,
                "schema_outcome": "invalid",
                "execution_status": "not_executed",
                "preview": "Connector payload failed local schema validation",
                "preview_details": {
                    "action": action,
                    "target": payload or {},
                    "forwarded_payload": transform_connector_request(action, payload),
                    "row_estimate": "unknown",
                    "upstream_validated": False,
                    "schema_validated": True,
                    "schema_status": "invalid",
                },
                "validation_errors": validation["errors"],
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                    *validation_warnings,
                ],
                "hint": "Fix validation_errors and run dry_run again before confirm_write=True.",
            }

        schema_validated = validation["status"] == "validated"
        schema_outcome = "schema_valid" if schema_validated else "unvalidated"
        forwarded_payload = transform_connector_request(action, payload)
        warnings = [
            "Dry run is a local preview only and does not call FairCom backend APIs.",
            (
                "Dry run did not run full schema validation because no local schema profile "
                "matched this payload."
                if not schema_validated
                else ("Dry run passed local schema validation only; upstream checks were not run.")
            ),
        ]
        warnings.extend(validation_warnings)
        if action == "alterInput":
            warnings.append(
                "alterInput may replace existing mappings; include a complete propertyMapList in "
                "the payload."
            )
        return {
            "mode": "dry_run",
            "status": "success",
            "tool_name": tool_name,
            "action": action,
            "payload": payload,
            "forwarded_payload": forwarded_payload,
            "schema_outcome": schema_outcome,
            "execution_status": "not_executed",
            "preview": (
                "Connector change would execute"
                if schema_validated
                else "Connector change preview only"
            ),
            "preview_details": {
                "action": action,
                "target": payload or {},
                "forwarded_payload": forwarded_payload,
                "row_estimate": "unknown",
                "upstream_validated": False,
                "schema_validated": schema_validated,
                "schema_status": validation["status"],
                "schema_service": validation["service_name"],
            },
            "warnings": warnings,
            "hint": (
                "Review the preview above. Then run list_inputs/describe_inputs to verify upstream "
                "connectivity before calling with confirm_write=True."
            ),
        }

    def _validate_connector_schema(
        *,
        tool_name: str,
        action: str,
        payload: ConnectorPayload | None,
    ) -> dict[str, object]:
        _ = tool_name
        if not payload:
            return {
                "status": "unvalidated",
                "service_name": None,
                "errors": [],
                "warnings": [],
            }

        payload_data = dict(payload)

        service_name_raw = payload_data.get("serviceName")
        if not isinstance(service_name_raw, str) or not service_name_raw.strip():
            return {
                "status": "unvalidated",
                "service_name": None,
                "errors": [],
                "warnings": [],
            }

        service_name = service_name_raw.strip().lower()
        schema = _CONNECTOR_SCHEMA_REGISTRY.get(service_name)
        if schema is None:
            return {
                "status": "unvalidated",
                "service_name": service_name,
                "errors": [],
                "warnings": [],
            }

        enforce_required_fields = action in {
            "createInput",
            "alterInput",
            "createOutput",
            "alterOutput",
        }
        modbus_create_or_alter = service_name == "modbus" and action in {
            "createInput",
            "alterInput",
        }

        errors: list[dict[str, object]] = []
        warnings: list[str] = []

        def _to_json_pointer(path: str) -> str:
            # Convert dot/bracket path form (payload.a[0].b) into JSON Pointer.
            pointer_tokens: list[str] = []
            for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                if token.startswith("[") and token.endswith("]"):
                    pointer_tokens.append(token[1:-1])
                else:
                    pointer_tokens.append(token)
            escaped = [part.replace("~", "~0").replace("/", "~1") for part in pointer_tokens]
            return "/" + "/".join(escaped)

        def _validation_error(
            *,
            path: str,
            reason: str,
            message: str,
            details: dict[str, object] | None = None,
        ) -> dict[str, object]:
            error: dict[str, object] = {
                "path": path,
                "json_pointer": _to_json_pointer(path),
                "reason": reason,
                "message": message,
            }
            if details:
                error.update(details)
            return error

        def _modbus_minimal_snippet() -> dict[str, object]:
            return {
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
            }

        def _enum_error(
            *,
            path: str,
            message: str,
            allowed_values: list[str],
            reason: str,
            received: object | None = None,
        ) -> dict[str, object]:
            error = _validation_error(
                path=path,
                reason=reason,
                message=message,
                details={"allowed_values": allowed_values},
            )
            if received is not None:
                error["received"] = received
                if isinstance(received, str):
                    nearest = difflib.get_close_matches(
                        received.strip().lower(),
                        allowed_values,
                        n=1,
                        cutoff=0.6,
                    )
                    if nearest:
                        error["nearest_match"] = nearest[0]
            if path.endswith(".modbusDataAccess"):
                selected_access = error.get("nearest_match") or allowed_values[0]
                error["corrected_snippet"] = {
                    "propertyMapList": [{"modbusDataAccess": selected_access}]
                }
            return error

        required_keys = schema.get("required", [])
        if enforce_required_fields and isinstance(required_keys, list):
            for key in required_keys:
                value = payload.get(str(key))
                if isinstance(value, str):
                    if not value.strip():
                        errors.append(
                            _validation_error(
                                path=f"payload.{key}",
                                reason="required",
                                message=f"{key} is required and must be non-empty",
                            )
                        )
                elif value is None:
                    details: dict[str, object] = {}
                    if service_name == "modbus":
                        details["corrected_snippet"] = _modbus_minimal_snippet()
                    errors.append(
                        _validation_error(
                            path=f"payload.{key}",
                            reason="required",
                            message=f"{key} is required",
                            details=details,
                        )
                    )

        if service_name == "javascript" and action in {"createTransform", "alterTransform"}:
            transform_actions = payload.get("transformActions")
            if isinstance(transform_actions, list):
                for index, action_entry in enumerate(transform_actions):
                    if not isinstance(action_entry, dict):
                        continue

                    step_method_raw = action_entry.get("transformStepMethod")
                    if not isinstance(step_method_raw, str) or not step_method_raw.strip():
                        step_method_raw = action_entry.get("transformActionName")
                    step_method = (
                        step_method_raw.strip()
                        if isinstance(step_method_raw, str) and step_method_raw.strip()
                        else None
                    )
                    if step_method not in {"jsonToDifferentTableFields", "jsonToTableFields"}:
                        continue

                    output_fields = action_entry.get("outputFields")
                    if not isinstance(output_fields, list) or not output_fields:
                        errors.append(
                            _validation_error(
                                path=f"payload.transformActions[{index}].outputFields",
                                reason="required",
                                message=(
                                    "outputFields is required for transformStepMethod "
                                    f"{step_method}"
                                ),
                                details={
                                    "corrected_snippet": {
                                        "outputFields": ["*"],
                                    }
                                },
                            )
                        )

                    transform_params = action_entry.get("transformParams")
                    map_of_properties = (
                        transform_params.get("mapOfPropertiesToFields")
                        if isinstance(transform_params, dict)
                        else None
                    )
                    if not isinstance(map_of_properties, list) or not map_of_properties:
                        errors.append(
                            _validation_error(
                                path=(
                                    "payload.transformActions"
                                    f"[{index}].transformParams.mapOfPropertiesToFields"
                                ),
                                reason="required",
                                message=(
                                    "transformParams.mapOfPropertiesToFields is required for "
                                    f"transformStepMethod {step_method}"
                                ),
                                details={
                                    "corrected_snippet": {
                                        "transformParams": {
                                            "mapOfPropertiesToFields": [
                                                {
                                                    "recordPath": "source_payload.temperature",
                                                    "fieldName": "temperature",
                                                }
                                            ]
                                        }
                                    }
                                },
                            )
                        )

        if modbus_create_or_alter:
            for key in payload:
                if key in _MODBUS_ALLOWED_PAYLOAD_KEYS:
                    continue
                warning = (
                    f"Unknown modbus payload field '{key}' will be passed through unchanged "
                    "for upstream validation."
                )
                nearest = difflib.get_close_matches(
                    key,
                    sorted(_MODBUS_ALLOWED_PAYLOAD_KEYS),
                    n=1,
                    cutoff=0.6,
                )
                if nearest:
                    warning = f"{warning} Nearest known key: {nearest[0]}."
                warnings.append(warning)

            modbus_protocol = payload.get("modbusProtocol")
            if not isinstance(modbus_protocol, str) or not modbus_protocol.strip():
                errors.append(
                    _validation_error(
                        path="payload.modbusProtocol",
                        reason="required",
                        message="modbusProtocol is required",
                    )
                )

            modbus_server = payload.get("modbusServer")
            if isinstance(modbus_server, str) and modbus_server.strip():
                modbus_server_normalized = modbus_server.strip()
                if "://" in modbus_server_normalized:
                    errors.append(
                        _validation_error(
                            path="payload.modbusServer",
                            reason="invalid_format",
                            message=(
                                "modbusServer must be a bare host or IP. "
                                "Provide port separately via modbusServerPort."
                            ),
                            details={
                                "received": modbus_server,
                                "corrected_snippet": {
                                    "modbusServer": "127.0.0.1",
                                    "modbusServerPort": 502,
                                },
                            },
                        )
                    )
                elif re.fullmatch(r"[^/:]+:\d+", modbus_server_normalized):
                    host_part, _, port_part = modbus_server_normalized.partition(":")
                    errors.append(
                        _validation_error(
                            path="payload.modbusServer",
                            reason="invalid_format",
                            message=(
                                "modbusServer must not include a :port suffix. "
                                "Use modbusServerPort."
                            ),
                            details={
                                "received": modbus_server,
                                "corrected_snippet": {
                                    "modbusServer": host_part,
                                    "modbusServerPort": int(port_part),
                                },
                            },
                        )
                    )

            modbus_server_port = payload.get("modbusServerPort")
            if isinstance(modbus_server_port, float) and modbus_server_port.is_integer():
                modbus_server_port = int(modbus_server_port)
            if not isinstance(modbus_server_port, int) or not (1 <= modbus_server_port <= 65535):
                errors.append(
                    _validation_error(
                        path="payload.modbusServerPort",
                        reason="required",
                        message=(
                            "modbusServerPort is required and must be an integer in range 1..65535"
                        ),
                    )
                )

            property_map_list = payload.get("propertyMapList")
            if not isinstance(property_map_list, list) or not property_map_list:
                errors.append(
                    _validation_error(
                        path="payload.propertyMapList",
                        reason="invalid_type",
                        message="propertyMapList must be a non-empty array",
                        details={
                            "expected": "array",
                            "received": type(property_map_list).__name__,
                            "corrected_snippet": {
                                "propertyMapList": [
                                    {
                                        "propertyName": "temperature",
                                        "modbusDataAccess": "holdingregister",
                                        "modbusDataAddress": 1199,
                                    }
                                ]
                            },
                        },
                    )
                )
            else:
                for index, entry in enumerate(property_map_list):
                    if not isinstance(entry, dict):
                        errors.append(
                            _validation_error(
                                path=f"payload.propertyMapList[{index}]",
                                reason="invalid_type",
                                message="Each property map item must be an object",
                                details={
                                    "expected": "object",
                                    "received": type(entry).__name__,
                                },
                            )
                        )
                        continue

                    for key in entry:
                        if key in _MODBUS_ALLOWED_PROPERTY_MAP_KEYS:
                            continue
                        unknown_entry_error = _validation_error(
                            path=f"payload.propertyMapList[{index}].{key}",
                            reason="unknown_field",
                            message=(
                                "Unknown field in propertyMapList item "
                                "for modbus connector payload."
                            ),
                            details={
                                "field": key,
                                "allowed_fields": sorted(_MODBUS_ALLOWED_PROPERTY_MAP_KEYS),
                            },
                        )
                        nearest = difflib.get_close_matches(
                            key,
                            sorted(_MODBUS_ALLOWED_PROPERTY_MAP_KEYS),
                            n=1,
                            cutoff=0.6,
                        )
                        if nearest:
                            unknown_entry_error["nearest_match"] = nearest[0]
                        errors.append(unknown_entry_error)

                    access = entry.get("modbusDataAccess")
                    if not isinstance(access, str) or not access.strip():
                        errors.append(
                            _enum_error(
                                path=f"payload.propertyMapList[{index}].modbusDataAccess",
                                reason="required",
                                message="modbusDataAccess is required",
                                allowed_values=_MODBUS_DATA_ACCESS_ENUM,
                            )
                        )
                    elif access.strip().lower() not in _MODBUS_DATA_ACCESS_ENUM:
                        errors.append(
                            _enum_error(
                                path=f"payload.propertyMapList[{index}].modbusDataAccess",
                                reason="invalid_enum",
                                message="modbusDataAccess must be one of the allowed values",
                                allowed_values=_MODBUS_DATA_ACCESS_ENUM,
                                received=access,
                            )
                        )

                    address = entry.get("modbusDataAddress")
                    if not isinstance(address, (int, float)):
                        errors.append(
                            _validation_error(
                                path=f"payload.propertyMapList[{index}].modbusDataAddress",
                                reason="required",
                                message="modbusDataAddress is required",
                            )
                        )

                    data_type = entry.get("modbusDataType")
                    register_type = entry.get("modbusRegisterType")
                    normalized_type = None
                    if isinstance(data_type, str) and data_type.strip():
                        normalized_type = data_type.strip()
                    if isinstance(register_type, str) and register_type.strip():
                        normalized_register_type = register_type.strip()
                        if normalized_type is None:
                            normalized_type = normalized_register_type
                        elif normalized_register_type != normalized_type:
                            errors.append(
                                _validation_error(
                                    path=f"payload.propertyMapList[{index}]",
                                    reason="invalid_arguments",
                                    message=(
                                        "modbusDataType and modbusRegisterType conflict; "
                                        "provide only one or make them match"
                                    ),
                                    details={
                                        "received": {
                                            "modbusDataType": data_type,
                                            "modbusRegisterType": register_type,
                                        }
                                    },
                                )
                            )

                    data_type = normalized_type
                    if isinstance(data_type, str) and data_type.strip():
                        data_type_normalized = data_type.strip()
                        if data_type_normalized not in _MODBUS_DATA_TYPE_ENUM:
                            enum_error = _validation_error(
                                path=f"payload.propertyMapList[{index}].modbusDataType",
                                reason="invalid_enum",
                                message=(
                                    "modbusDataType must be one of the allowed values. "
                                    "Use explicit byte-order-qualified values."
                                ),
                                details={
                                    "received": data_type,
                                    "allowed_values": _MODBUS_DATA_TYPE_ENUM,
                                },
                            )
                            nearest = difflib.get_close_matches(
                                data_type_normalized,
                                _MODBUS_DATA_TYPE_ENUM,
                                n=1,
                                cutoff=0.6,
                            )
                            if nearest:
                                enum_error["nearest_match"] = nearest[0]
                            errors.append(enum_error)

                    access_normalized = access.strip().lower() if isinstance(access, str) else None
                    if (
                        access_normalized in {"coil", "discreteinput"}
                        and isinstance(data_type, str)
                        and data_type.strip() == "bitBoolean"
                        and not isinstance(entry.get("bitStartPosition"), (int, float))
                    ):
                        errors.append(
                            _validation_error(
                                path=f"payload.propertyMapList[{index}].bitStartPosition",
                                reason="required",
                                message=(
                                    "bitStartPosition is required when "
                                    "modbusDataType is bitBoolean for "
                                    "coil/discreteinput mappings"
                                ),
                            )
                        )

                    has_property_target = any(
                        isinstance(entry.get(field), str) and str(entry.get(field)).strip()
                        for field in ("propertyName", "propertyPath", "tagName")
                    ) or isinstance(entry.get("tagId"), int)
                    if not has_property_target:
                        selected_access = (
                            access
                            if isinstance(access, str) and access.strip()
                            else "holdingregister"
                        )
                        errors.append(
                            _validation_error(
                                path=f"payload.propertyMapList[{index}]",
                                reason="required",
                                message=(
                                    "Each property map item must include "
                                    "propertyName, propertyPath, "
                                    "tagName, or tagId"
                                ),
                                details={
                                    "corrected_snippet": {
                                        "propertyName": "temperature",
                                        "modbusDataAccess": selected_access,
                                        "modbusDataAddress": 1199,
                                    }
                                },
                            )
                        )

                    divisor = entry.get("modbusDivisor")
                    convert_to_float = entry.get("modbusConvertToFloat")
                    if isinstance(divisor, float) and divisor.is_integer():
                        divisor = int(divisor)
                    if (
                        isinstance(divisor, int)
                        and divisor > 1
                        and (not isinstance(convert_to_float, str) or not convert_to_float.strip())
                    ):
                        warnings.append(
                            "payload.propertyMapList"
                            f"[{index}].modbusDivisor was provided without "
                            "modbusConvertToFloat; MCP will normalize this to "
                            "divideByInteger on write requests."
                        )

        status = "validated" if not errors else "invalid"
        return {
            "status": status,
            "service_name": service_name,
            "errors": errors,
            "warnings": warnings,
        }

    def _require_connector_payload(
        *,
        tool_name: str,
        payload: ConnectorPayload | None,
        action: str,
    ) -> ConnectorPayload:
        if payload is None or not payload:
            raise _validation_failure(
                tool_name=tool_name,
                message="connector payload is required",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload, "action": action},
                suggested_fix="Provide a non-empty connector payload object or set dry_run=true.",
                example_payload={
                    "name": tool_name,
                    "arguments": {"payload": {"connectorName": "demo", "type": "input"}},
                },
            )

        normalized_payload = dict(payload)
        is_input_action = action in {"createInput", "alterInput", "deleteInput"}
        is_transform_action = action in {"createTransform", "alterTransform", "deleteTransform"}
        connector_name = normalized_payload.get("connectorName")
        if (not isinstance(connector_name, str) or not connector_name.strip()) and is_input_action:
            input_name = normalized_payload.get("inputName")
            if isinstance(input_name, str) and input_name.strip():
                connector_name = input_name.strip()
                normalized_payload["connectorName"] = connector_name
        if (
            not isinstance(connector_name, str) or not connector_name.strip()
        ) and is_transform_action:
            transform_name = normalized_payload.get("transformName")
            if isinstance(transform_name, str) and transform_name.strip():
                connector_name = transform_name.strip()
                normalized_payload["connectorName"] = connector_name

        if not isinstance(connector_name, str) or not connector_name.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="connectorName is required in connector payload",
                expected_args={
                    "payload": "object (required)",
                    "payload.connectorName": "string (required)",
                    "payload.inputName": "string (accepted alias for input actions)",
                    "payload.transformName": "string (accepted alias for transform actions)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload, "action": action},
                suggested_fix=(
                    "Provide payload.connectorName with a non-empty connector name. "
                    "For input actions, payload.inputName is also accepted. "
                    "For transform actions, payload.transformName is also accepted."
                ),
                example_payload={
                    "name": tool_name,
                    "arguments": {
                        "payload": {"connectorName": "modbus_sim_assets", "type": "input"}
                    },
                },
            )

        connector_name = connector_name.strip()
        normalized_payload["connectorName"] = connector_name
        if is_input_action:
            input_name = normalized_payload.get("inputName")
            if not isinstance(input_name, str) or not input_name.strip():
                normalized_payload["inputName"] = connector_name
        if is_transform_action:
            transform_name = normalized_payload.get("transformName")
            if not isinstance(transform_name, str) or not transform_name.strip():
                normalized_payload["transformName"] = connector_name

        service_name = normalized_payload.get("serviceName")
        if isinstance(service_name, str) and service_name.strip().lower() == "modbus":
            default_unit_id = normalized_payload.get("unitId")
            if isinstance(default_unit_id, float) and default_unit_id.is_integer():
                default_unit_id = int(default_unit_id)
            if not isinstance(default_unit_id, int):
                default_unit_id = None

            property_map_list = normalized_payload.get("propertyMapList")
            if isinstance(property_map_list, list):
                normalized_entries: list[dict[str, object]] = []
                for entry in property_map_list:
                    if not isinstance(entry, dict):
                        normalized_entries.append(entry)
                        continue

                    normalized_entry = dict(entry)

                    # Accept either field name used in documentation and normalize both.
                    data_type = normalized_entry.get("modbusDataType")
                    register_type = normalized_entry.get("modbusRegisterType")
                    if isinstance(data_type, str) and data_type.strip():
                        normalized_entry["modbusDataType"] = data_type.strip()
                        normalized_entry.setdefault("modbusRegisterType", data_type.strip())
                    elif isinstance(register_type, str) and register_type.strip():
                        normalized_entry["modbusRegisterType"] = register_type.strip()
                        normalized_entry["modbusDataType"] = register_type.strip()

                    property_name = normalized_entry.get("propertyName")
                    if (
                        isinstance(property_name, str)
                        and property_name.strip()
                        and "propertyPath" not in normalized_entry
                    ):
                        normalized_entry["propertyPath"] = property_name.strip()

                    if default_unit_id is not None and "modbusUnitId" not in normalized_entry:
                        normalized_entry["modbusUnitId"] = default_unit_id

                    scale = normalized_entry.get("scale")
                    if (
                        isinstance(scale, (int, float))
                        and scale not in {0, 1}
                        and "modbusConvertToFloat" not in normalized_entry
                        and "modbusDivisor" not in normalized_entry
                    ):
                        inverse_scale = 1 / float(scale)
                        if inverse_scale.is_integer() and inverse_scale > 0:
                            normalized_entry["modbusConvertToFloat"] = "divideByInteger"
                            normalized_entry["modbusDivisor"] = int(inverse_scale)

                    divisor = normalized_entry.get("modbusDivisor")
                    if isinstance(divisor, float) and divisor.is_integer():
                        divisor = int(divisor)
                        normalized_entry["modbusDivisor"] = divisor
                    if (
                        isinstance(divisor, int)
                        and divisor > 1
                        and "modbusConvertToFloat" not in normalized_entry
                    ):
                        normalized_entry["modbusConvertToFloat"] = "divideByInteger"

                    normalized_entries.append(normalized_entry)

                normalized_payload["propertyMapList"] = normalized_entries

        if isinstance(service_name, str) and service_name.strip().lower() == "javascript":
            transform_actions = normalized_payload.get("transformActions")
            if isinstance(transform_actions, list):
                normalized_actions: list[dict[str, object]] = []
                for action_entry in transform_actions:
                    if not isinstance(action_entry, dict):
                        normalized_actions.append(action_entry)
                        continue
                    normalized_action = dict(action_entry)
                    step_method = normalized_action.get("transformStepMethod")
                    action_name = normalized_action.get("transformActionName")
                    if (
                        (not isinstance(step_method, str) or not step_method.strip())
                        and isinstance(action_name, str)
                        and action_name.strip()
                    ):
                        normalized_action["transformStepMethod"] = action_name.strip()
                    elif isinstance(step_method, str) and step_method.strip():
                        normalized_action["transformStepMethod"] = step_method.strip()
                    normalized_actions.append(normalized_action)
                normalized_payload["transformActions"] = normalized_actions

        typed_payload = cast(ConnectorPayload, normalized_payload)

        validation = _validate_connector_schema(
            tool_name=tool_name,
            action=action,
            payload=typed_payload,
        )
        if validation["status"] == "invalid":
            raise _validation_failure(
                tool_name=tool_name,
                message="connector payload failed local schema validation",
                expected_args={
                    "payload": "object (required)",
                    "payload.serviceName": "string (recommended for schema validation)",
                },
                received_args={
                    "payload": normalized_payload,
                    "action": action,
                    "schema_service": validation["service_name"],
                    "validation_errors": validation["errors"],
                },
                suggested_fix=(
                    "Correct the fields listed in validation_errors and retry. "
                    "Use describe_connector_schema(service_name=...) for a known-good payload "
                    "shape."
                ),
                example_payload={
                    "name": "describe_connector_schema",
                    "arguments": {"service_name": str(validation["service_name"] or "modbus")},
                },
            )

        return typed_payload

    def _validate_sql_shape(tool_name: str, statement: str) -> None:
        unsupported = detect_unsupported_features(statement)
        if not unsupported:
            return

        validation_error = _validation_failure(
            tool_name=tool_name,
            message="Unsupported SQL syntax for this backend",
            expected_args={
                "statement": "Use FairCom grammar (TOP / SKIP).",
                "params": "array (optional)",
            },
            received_args={"statement": statement},
            suggested_fix=(
                "Replace LIMIT/OFFSET/FETCH with TOP and optional SKIP, e.g. "
                "SELECT TOP 25 ... or SELECT SKIP 10 TOP 25 ..."
            ),
            example_payload={
                "name": tool_name,
                "arguments": {
                    "statement": (
                        "SELECT TOP 1 id, metric FROM demo_sensor_readings ORDER BY metric DESC"
                    )
                },
            },
            reason_code="unsupported_sql_feature",
        )
        validation_error.details["unsupported_sql_feature"] = unsupported
        raise validation_error

    def _normalize_sql_dialect(
        *,
        tool_name: str,
        statement: str,
    ) -> tuple[str, dict[str, object] | None]:
        normalized_statement, rewrites = normalize_select_first_to_top(statement)
        if not rewrites:
            return statement, None

        metrics.record_compat_event(tool=tool_name, event="sql_rewritten_first_to_top")
        return normalized_statement, {"sql_rewrites": rewrites}

    def _resolve_single_string_arg(
        *,
        tool_name: str,
        canonical_name: str,
        canonical_value: str | None,
        aliases: dict[str, str | None],
        expected_args: dict[str, object],
        example_payload: dict[str, object],
    ) -> tuple[str, list[dict[str, str]]]:
        chosen = canonical_value
        normalized_args: list[dict[str, str]] = []

        for alias_name, alias_value in aliases.items():
            if alias_value is None:
                continue
            if chosen is None:
                chosen = alias_value
                normalized_args.append({"from": alias_name, "to": canonical_name})
                continue
            if alias_value != chosen:
                received_args: dict[str, object] = {
                    canonical_name: chosen,
                    alias_name: alias_value,
                }
                raise _validation_failure(
                    tool_name=tool_name,
                    message=f"Conflicting values for {canonical_name} and {alias_name}",
                    expected_args=expected_args,
                    received_args=received_args,
                    suggested_fix=(
                        f"Provide only '{canonical_name}' or provide matching alias values."
                    ),
                    example_payload=example_payload,
                )

        if chosen is None:
            missing_received_args: dict[str, object] = {}
            if canonical_value is not None:
                missing_received_args[canonical_name] = canonical_value
            for key, value in aliases.items():
                if value is not None:
                    missing_received_args[key] = value
            raise _validation_failure(
                tool_name=tool_name,
                message=f"Missing required argument: {canonical_name}",
                expected_args=expected_args,
                received_args=missing_received_args,
                suggested_fix=f"Provide '{canonical_name}' as a non-empty string.",
                example_payload=example_payload,
            )

        return chosen, normalized_args

    def _is_diagnostics_authorized(request: Request) -> bool:
        token = resolved_config.security.diagnostics_token
        if not token:
            return False

        provided = request.headers.get("x-diagnostics-token")
        if provided is None:
            provided = request.query_params.get("token")
        return provided == token

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/ready", methods=["GET"])
    async def ready(_request: Request) -> JSONResponse:
        is_ready = bool(readiness_check() if readiness_check is not None else True)
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status}, status_code=status_code)

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        is_ready = bool(readiness_check() if readiness_check is not None else True)
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status}, status_code=status_code)

    if resolved_config.observability.enable_metrics:

        @server.custom_route("/metrics", methods=["GET"])
        async def metrics_route(_request: Request) -> PlainTextResponse:
            return PlainTextResponse(
                metrics.to_prometheus(),
                media_type="text/plain; version=0.0.4",
            )

    if (
        resolved_config.security.diagnostics_enabled
        and "diagnostics" in resolved_config.security.tool_group_allowlist
    ):

        @server.custom_route("/diagnostics/json", methods=["GET"])
        async def diagnostics_json(request: Request) -> JSONResponse:
            if not _is_diagnostics_authorized(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)

            return JSONResponse(
                {
                    "service": "faircom-mcp",
                    "tool_group_allowlist": list(resolved_config.security.tool_group_allowlist),
                    "metrics": metrics.snapshot(),
                }
            )

        @server.custom_route("/diagnostics", methods=["GET"])
        async def diagnostics_html(request: Request) -> HTMLResponse:
            if not _is_diagnostics_authorized(request):
                return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)

            payload = html.escape(
                json.dumps(
                    {
                        "service": "faircom-mcp",
                        "tool_group_allowlist": list(resolved_config.security.tool_group_allowlist),
                        "metrics": metrics.snapshot(),
                    },
                    indent=2,
                )
            )
            return HTMLResponse(
                """
<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>FairCom MCP Diagnostics</title></head>
<body>
<pre>"""
                + payload
                + """</pre>
</body>
</html>
"""
            )

    @server.tool(name="list_tables")
    def list_tables(
        name_like: str | None = None,
        table_like: str | None = None,
        database: str | None = None,
    ) -> object:
        resolved_name_like = name_like
        normalized_args: list[dict[str, str]] = []
        if table_like is not None:
            if resolved_name_like is None:
                resolved_name_like = table_like
                normalized_args.append({"from": "table_like", "to": "name_like"})
            elif resolved_name_like != table_like:
                raise _validation_failure(
                    tool_name="list_tables",
                    message="Conflicting values for name_like and table_like",
                    expected_args={
                        "name_like": "string (optional)",
                        "database": "string (optional)",
                    },
                    received_args={
                        "name_like": resolved_name_like,
                        "table_like": table_like,
                        "database": database,
                    },
                    suggested_fix="Provide only name_like or provide matching values.",
                    example_payload={
                        "name": "list_tables",
                        "arguments": {"name_like": "demo_%"},
                    },
                    reason_code="invalid_arguments",
                )

        result = _run_tool(
            "list_tables",
            "metadata",
            lambda: table_adapter.list_tables(name_like=resolved_name_like, database=database),
        )
        metadata: dict[str, object] | None = None
        if database is not None:
            metadata = {
                "database": {
                    "received": database,
                    "applied": False,
                    "reason": "database_scoping_not_supported_by_backend_adapter",
                }
            }

        return _with_compatibility(
            result,
            tool_name="list_tables",
            normalized_args=normalized_args,
            metadata=metadata,
        )

    @server.tool(name="describe_table")
    def describe_table(table_name: str | None = None, table: str | None = None) -> object:
        resolved_table_name, normalized_args = _resolve_single_string_arg(
            tool_name="describe_table",
            canonical_name="table_name",
            canonical_value=table_name,
            aliases={"table": table},
            expected_args={"table_name": "string (required)"},
            example_payload={
                "name": "describe_table",
                "arguments": {"table_name": "demo_assets"},
            },
        )
        return _run_tool(
            "describe_table",
            "metadata",
            lambda: _with_compatibility(
                table_adapter.describe_table(resolved_table_name),
                tool_name="describe_table",
                normalized_args=normalized_args,
            ),
        )

    @server.tool(name="list_table_columns")
    def list_table_columns(table_name: str | None = None, table: str | None = None) -> object:
        resolved_table_name, normalized_args = _resolve_single_string_arg(
            tool_name="list_table_columns",
            canonical_name="table_name",
            canonical_value=table_name,
            aliases={"table": table},
            expected_args={"table_name": "string (required)"},
            example_payload={
                "name": "list_table_columns",
                "arguments": {"table_name": "demo_assets"},
            },
        )
        return _run_tool(
            "list_table_columns",
            "metadata",
            lambda: _with_compatibility(
                table_adapter.list_table_columns(resolved_table_name),
                tool_name="list_table_columns",
                normalized_args=normalized_args,
            ),
        )

    @server.tool(name="list_table_indexes")
    def list_table_indexes(table_name: str | None = None, table: str | None = None) -> object:
        resolved_table_name, normalized_args = _resolve_single_string_arg(
            tool_name="list_table_indexes",
            canonical_name="table_name",
            canonical_value=table_name,
            aliases={"table": table},
            expected_args={"table_name": "string (required)"},
            example_payload={
                "name": "list_table_indexes",
                "arguments": {"table_name": "demo_assets"},
            },
        )
        return _run_tool(
            "list_table_indexes",
            "metadata",
            lambda: _with_compatibility(
                table_adapter.list_table_indexes(resolved_table_name),
                tool_name="list_table_indexes",
                normalized_args=normalized_args,
            ),
        )

    @server.tool(name="list_inputs")
    def list_inputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool("list_inputs", "metadata", lambda: connector_adapter.list_inputs(payload))

    @server.tool(name="listInputs")
    def list_inputs_alias(payload: dict[str, object] | None = None) -> object:
        return list_inputs(payload=payload)

    @server.tool(name="describe_inputs")
    def describe_inputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_inputs",
            "metadata",
            lambda: connector_adapter.describe_inputs(payload),
        )

    @server.tool(name="describeInputs")
    def describe_inputs_alias(payload: dict[str, object] | None = None) -> object:
        return describe_inputs(payload=payload)

    @server.tool(name="create_input")
    def create_input(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "create_input", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "create_input",
                "connector",
                lambda: _connector_preview(
                    tool_name="create_input",
                    action="createInput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="create_input",
            payload=payload,
            action="createInput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="create_input",
                message="create_input requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "create_input",
                    "arguments": {"payload": {"connectorName": "demo", "type": "input"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "create_input",
            "connector",
            lambda: connector_adapter.create_input(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="createInput")
    def create_input_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return create_input(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="alter_input")
    def alter_input(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "alter_input", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "alter_input",
                "connector",
                lambda: _connector_preview(
                    tool_name="alter_input",
                    action="alterInput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="alter_input",
            payload=payload,
            action="alterInput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="alter_input",
                message="alter_input requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "alter_input",
                    "arguments": {"payload": {"connectorName": "demo", "type": "input"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "alter_input",
            "connector",
            lambda: connector_adapter.alter_input(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="alterInput")
    def alter_input_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return alter_input(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="delete_input")
    def delete_input(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "delete_input", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "delete_input",
                "connector",
                lambda: _connector_preview(
                    tool_name="delete_input",
                    action="deleteInput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="delete_input",
            payload=payload,
            action="deleteInput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="delete_input",
                message="delete_input requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "delete_input",
                    "arguments": {"payload": {"connectorName": "demo", "type": "input"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "delete_input",
            "connector",
            lambda: connector_adapter.delete_input(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="deleteInput")
    def delete_input_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return delete_input(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="list_outputs")
    def list_outputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_outputs",
            "metadata",
            lambda: connector_adapter.list_outputs(payload),
        )

    @server.tool(name="listOutputs")
    def list_outputs_alias(payload: dict[str, object] | None = None) -> object:
        return list_outputs(payload=payload)

    @server.tool(name="describe_outputs")
    def describe_outputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_outputs",
            "metadata",
            lambda: connector_adapter.describe_outputs(payload),
        )

    @server.tool(name="describeOutputs")
    def describe_outputs_alias(payload: dict[str, object] | None = None) -> object:
        return describe_outputs(payload=payload)

    @server.tool(name="create_output")
    def create_output(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "create_output", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "create_output",
                "connector",
                lambda: _connector_preview(
                    tool_name="create_output",
                    action="createOutput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="create_output",
            payload=payload,
            action="createOutput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="create_output",
                message="create_output requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "create_output",
                    "arguments": {"payload": {"connectorName": "demo", "type": "output"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "create_output",
            "connector",
            lambda: connector_adapter.create_output(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="createOutput")
    def create_output_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return create_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="alter_output")
    def alter_output(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "alter_output", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "alter_output",
                "connector",
                lambda: _connector_preview(
                    tool_name="alter_output",
                    action="alterOutput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="alter_output",
            payload=payload,
            action="alterOutput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="alter_output",
                message="alter_output requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "alter_output",
                    "arguments": {"payload": {"connectorName": "demo", "type": "output"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "alter_output",
            "connector",
            lambda: connector_adapter.alter_output(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="alterOutput")
    def alter_output_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return alter_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="delete_output")
    def delete_output(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={"tool": "delete_output", "dry_run": dry_run, "confirm_write": confirm_write},
        )
        if dry_run:
            return _run_tool(
                "delete_output",
                "connector",
                lambda: _connector_preview(
                    tool_name="delete_output",
                    action="deleteOutput",
                    payload=payload,
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="delete_output",
            payload=payload,
            action="deleteOutput",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="delete_output",
                message="delete_output requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "delete_output",
                    "arguments": {"payload": {"connectorName": "demo", "type": "output"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "delete_output",
            "connector",
            lambda: connector_adapter.delete_output(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="deleteOutput")
    def delete_output_alias(
        payload: ConnectorPayload | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return delete_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="list_transforms")
    def list_transforms(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_transforms",
            "metadata",
            lambda: connector_adapter.list_transforms(payload),
        )

    @server.tool(name="listTransforms")
    def list_transforms_alias(payload: dict[str, object] | None = None) -> object:
        return list_transforms(payload=payload)

    @server.tool(name="describe_transforms")
    def describe_transforms(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_transforms",
            "metadata",
            lambda: connector_adapter.describe_transforms(payload),
        )

    @server.tool(name="describeTransforms")
    def describe_transforms_alias(payload: dict[str, object] | None = None) -> object:
        return describe_transforms(payload=payload)

    @server.tool(name="create_transform")
    def create_transform(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "create_transform",
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "create_transform",
                "connector",
                lambda: _connector_preview(
                    tool_name="create_transform",
                    action="createTransform",
                    payload=cast(ConnectorPayload | None, payload),
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="create_transform",
            payload=cast(ConnectorPayload | None, payload),
            action="createTransform",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="create_transform",
                message="create_transform requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "create_transform",
                    "arguments": {"payload": {"connectorName": "demo", "type": "transform"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "create_transform",
            "connector",
            lambda: connector_adapter.create_transform(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="createTransform")
    def create_transform_alias(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return create_transform(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="alter_transform")
    def alter_transform(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "alter_transform",
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "alter_transform",
                "connector",
                lambda: _connector_preview(
                    tool_name="alter_transform",
                    action="alterTransform",
                    payload=cast(ConnectorPayload | None, payload),
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="alter_transform",
            payload=cast(ConnectorPayload | None, payload),
            action="alterTransform",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="alter_transform",
                message="alter_transform requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "alter_transform",
                    "arguments": {"payload": {"connectorName": "demo", "type": "transform"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "alter_transform",
            "connector",
            lambda: connector_adapter.alter_transform(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="alterTransform")
    def alter_transform_alias(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return alter_transform(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="delete_transform")
    def delete_transform(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "delete_transform",
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "delete_transform",
                "connector",
                lambda: _connector_preview(
                    tool_name="delete_transform",
                    action="deleteTransform",
                    payload=cast(ConnectorPayload | None, payload),
                ),
            )
        resolved_payload = _require_connector_payload(
            tool_name="delete_transform",
            payload=cast(ConnectorPayload | None, payload),
            action="deleteTransform",
        )
        if not confirm_write:
            raise _validation_failure(
                tool_name="delete_transform",
                message="delete_transform requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": resolved_payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply the change or dry_run=true to preview it."
                ),
                example_payload={
                    "name": "delete_transform",
                    "arguments": {"payload": {"connectorName": "demo", "type": "transform"}},
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "delete_transform",
            "connector",
            lambda: connector_adapter.delete_transform(resolved_payload),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    @server.tool(name="deleteTransform")
    def delete_transform_alias(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return delete_transform(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="sql_query")
    def sql_query(
        statement: str | None = None,
        params: list[object] | None = None,
        sql: str | None = None,
        query: str | None = None,
    ) -> object:
        resolved_statement, normalized_args = _resolve_single_string_arg(
            tool_name="sql_query",
            canonical_name="statement",
            canonical_value=statement,
            aliases={"sql": sql, "query": query},
            expected_args={
                "statement": "string (required)",
                "params": "array (optional)",
            },
            example_payload={
                "name": "sql_query",
                "arguments": {"statement": "SELECT COUNT(*) AS row_count FROM demo_assets"},
            },
        )
        resolved_statement, dialect_metadata = _normalize_sql_dialect(
            tool_name="sql_query",
            statement=resolved_statement,
        )
        _validate_sql_shape("sql_query", resolved_statement)
        return _run_tool(
            "sql_query",
            "query",
            lambda: _with_compatibility(
                sql_adapter.query(resolved_statement, params=params),
                tool_name="sql_query",
                normalized_args=normalized_args,
                metadata=dialect_metadata,
            ),
        )

    @server.tool(name="sql_query_page")
    def sql_query_page(
        statement: str | None = None,
        params: list[object] | None = None,
        page: int = 1,
        page_size: int = 100,
        continuation_token: str | None = None,
        order_by: str | None = None,
        sql: str | None = None,
        query: str | None = None,
    ) -> object:
        resolved_statement, normalized_args = _resolve_single_string_arg(
            tool_name="sql_query_page",
            canonical_name="statement",
            canonical_value=statement,
            aliases={"sql": sql, "query": query},
            expected_args={
                "statement": "string (required)",
                "params": "array (optional)",
                "page": "integer (default=1)",
                "page_size": "integer (default=100)",
                "continuation_token": "string (optional)",
                "order_by": "string (optional)",
            },
            example_payload={
                "name": "sql_query_page",
                "arguments": {
                    "statement": "SELECT id, status FROM demo_assets ORDER BY id",
                    "page": 1,
                    "page_size": 100,
                },
            },
        )
        resolved_statement, dialect_metadata = _normalize_sql_dialect(
            tool_name="sql_query_page",
            statement=resolved_statement,
        )
        _validate_sql_shape("sql_query_page", resolved_statement)
        return _run_tool(
            "sql_query_page",
            "query",
            lambda: _with_compatibility(
                sql_adapter.query_page(
                    resolved_statement,
                    params=params,
                    page=page,
                    page_size=page_size,
                    continuation_token=continuation_token,
                    order_by=order_by,
                ),
                tool_name="sql_query_page",
                normalized_args=normalized_args,
                metadata=dialect_metadata,
            ),
        )

    @server.tool(name="get_usage_contract")
    def get_usage_contract() -> object:
        def _extract_enums_from_schema(
            schema_node: dict[str, object],
            *,
            path: str = "payload",
        ) -> dict[str, list[str]]:
            enum_map: dict[str, list[str]] = {}
            properties = schema_node.get("properties")
            if isinstance(properties, dict):
                for key, child in properties.items():
                    if not isinstance(child, dict):
                        continue
                    child_path = f"{path}.{key}"
                    enum_values = child.get("enum")
                    if isinstance(enum_values, list) and all(
                        isinstance(v, str) for v in enum_values
                    ):
                        enum_map[child_path] = [str(v) for v in enum_values]

                    child_type = child.get("type")
                    if child_type == "object":
                        enum_map.update(_extract_enums_from_schema(child, path=child_path))
                    elif child_type == "array":
                        items = child.get("items")
                        if isinstance(items, dict):
                            enum_map.update(
                                _extract_enums_from_schema(
                                    items,
                                    path=f"{child_path}[]",
                                )
                            )
            return enum_map

        connector_contract_profiles: dict[str, dict[str, object]] = {}
        for service_name, schema in _CONNECTOR_SCHEMA_REGISTRY.items():
            required = schema.get("required")
            required_keys = [str(v) for v in required] if isinstance(required, list) else []
            enums = _extract_enums_from_schema(schema)
            connector_contract_profiles[service_name] = {
                "service_name": service_name,
                "required_keys": required_keys,
                "enum_values": enums,
                "schema": schema,
                "known_good_example": schema.get("example"),
            }

        return _run_tool(
            "get_usage_contract",
            "admin",
            lambda: {
                "contract_version": "2026-08-06",
                "updated_at": "2026-08-06",
                "required_call_order": ["initialize", "tools/list", "tools/call"],
                "session_requirements": {
                    "required": True,
                    "header": "Mcp-Session-Id",
                    "missing_or_stale_behavior": (
                        "Re-run initialize and retry the failed call with the new session id."
                    ),
                },
                "transport_notes": {
                    "http": "Use JSON transport for plain JSON payloads.",
                    "sse": "Use an SSE parser for text/event-stream framing.",
                    "stdio": "Use MCP stdio framing for local process transport.",
                },
                "canonical_arg_keys": {
                    "sql_query": ["statement", "params"],
                    "sql_query_page": [
                        "statement",
                        "params",
                        "page",
                        "page_size",
                        "continuation_token",
                        "order_by",
                    ],
                    "list_tables": ["name_like", "database"],
                    "describe_table": ["table_name"],
                    "list_table_columns": ["table_name"],
                    "list_table_indexes": ["table_name"],
                    "sql_execute": ["statement", "params", "confirm_write", "dry_run"],
                    "list_inputs": ["payload"],
                    "describe_inputs": ["payload"],
                    "create_input": ["payload", "confirm_write", "dry_run"],
                    "alter_input": ["payload", "confirm_write", "dry_run"],
                    "delete_input": ["payload", "confirm_write", "dry_run"],
                    "list_outputs": ["payload"],
                    "describe_outputs": ["payload"],
                    "create_output": ["payload", "confirm_write", "dry_run"],
                    "alter_output": ["payload", "confirm_write", "dry_run"],
                    "delete_output": ["payload", "confirm_write", "dry_run"],
                    "list_transforms": ["payload"],
                    "describe_transforms": ["payload"],
                    "create_transform": ["payload", "confirm_write", "dry_run"],
                    "alter_transform": ["payload", "confirm_write", "dry_run"],
                    "delete_transform": ["payload", "confirm_write", "dry_run"],
                    "describe_connector_schema": ["service_name"],
                    "validate_connector_payloads": ["action", "payload", "payloads"],
                },
                "supported_aliases": {
                    "sql_query": {"sql": "statement", "query": "statement"},
                    "sql_query_page": {"sql": "statement", "query": "statement"},
                    "list_tables": {"table_like": "name_like"},
                    "describe_table": {"table": "table_name"},
                    "list_table_columns": {"table": "table_name"},
                    "list_table_indexes": {"table": "table_name"},
                    "sql_execute": {"sql": "statement", "query": "statement"},
                    "list_inputs": {},
                    "describe_inputs": {},
                    "create_input": {},
                    "alter_input": {},
                    "delete_input": {},
                    "list_outputs": {},
                    "describe_outputs": {},
                    "create_output": {},
                    "alter_output": {},
                    "delete_output": {},
                    "list_transforms": {},
                    "describe_transforms": {},
                    "create_transform": {},
                    "alter_transform": {},
                    "delete_transform": {},
                    "describe_connector_schema": {},
                    "validate_connector_payloads": {},
                },
                "dialect_notes": {
                    "row_limit": "Prefer TOP N and optional SKIP N.",
                    "auto_normalization": "SELECT FIRST N is normalized to SELECT TOP N.",
                    "unsupported_tokens": ["LIMIT", "OFFSET", "FETCH"],
                },
                "minimal_payload_examples": {
                    "sql_query": {
                        "name": "sql_query",
                        "arguments": {"statement": "SELECT COUNT(*) AS row_count FROM demo_assets"},
                    },
                    "list_tables": {
                        "name": "list_tables",
                        "arguments": {"name_like": "demo_%"},
                    },
                    "create_input": {
                        "name": "create_input",
                        "arguments": {
                            "payload": {
                                "connectorName": "modbus_energy_input",
                                "inputName": "modbus_energy_input",
                                "serviceName": "modbus",
                                "modbusProtocol": "TCP",
                                "modbusServer": "127.0.0.1",
                                "modbusServerPort": 502,
                                "propertyMapList": [
                                    {
                                        "modbusDataAccess": "holdingregister",
                                        "modbusDataType": "int16SignedAB",
                                        "modbusDataLen": 2,
                                    }
                                ],
                            },
                            "confirm_write": True,
                        },
                    },
                    "create_output": {
                        "name": "create_output",
                        "arguments": {
                            "payload": {
                                "connectorName": "mqtt_telemetry_output",
                                "serviceName": "mqtt",
                            },
                            "confirm_write": True,
                        },
                    },
                    "create_transform": {
                        "name": "create_transform",
                        "arguments": {
                            "payload": {
                                "transformName": "add_asset_id",
                                "serviceName": "javascript",
                                "transformService": "v8TransformService",
                                "transformActions": [
                                    {
                                        "inputFields": ["*"],
                                        "transformStepMethod": "javascript",
                                        "outputFields": ["*"],
                                        "transformParams": {
                                            "script": (
                                                "payload.asset_id = 'asset-001'; return payload;"
                                            )
                                        },
                                    }
                                ],
                            },
                            "confirm_write": True,
                        },
                    },
                    "describe_connector_schema": {
                        "name": "describe_connector_schema",
                        "arguments": {"service_name": "modbus"},
                    },
                    "validate_connector_payloads": {
                        "name": "validate_connector_payloads",
                        "arguments": {
                            "action": "createInput",
                            "payloads": [
                                {
                                    "connectorName": "modbus_energy_input",
                                    "serviceName": "modbus",
                                    "modbusProtocol": "TCP",
                                    "modbusServer": "127.0.0.1",
                                    "modbusServerPort": 502,
                                    "propertyMapList": [
                                        {
                                            "modbusDataAccess": "holdingregister",
                                            "modbusDataType": "int16SignedAB",
                                            "modbusDataLen": 2,
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                },
                "example_validity": {
                    "sql_query": "complete",
                    "list_tables": "complete",
                    "create_input": "complete",
                    "create_output": "complete",
                    "create_transform": "complete",
                    "describe_connector_schema": "complete",
                    "validate_connector_payloads": "complete",
                },
                "connector_payload_profiles": connector_contract_profiles,
                "connector_schema_profiles": sorted(_CONNECTOR_SCHEMA_REGISTRY.keys()),
            },
        )

    @server.tool(name="describe_connector_schema")
    def describe_connector_schema(service_name: str) -> object:
        normalized = service_name.strip().lower()
        schema = _CONNECTOR_SCHEMA_REGISTRY.get(normalized)
        if schema is None:
            raise _validation_failure(
                tool_name="describe_connector_schema",
                message="Unsupported connector service_name",
                expected_args={
                    "service_name": f"one of {sorted(_CONNECTOR_SCHEMA_REGISTRY.keys())}",
                },
                received_args={"service_name": service_name},
                suggested_fix="Call with a supported connector profile name.",
                example_payload={
                    "name": "describe_connector_schema",
                    "arguments": {"service_name": "modbus"},
                },
            )

        return _run_tool(
            "describe_connector_schema",
            "admin",
            lambda: {
                "service_name": normalized,
                "schema_version": "2026-08-06",
                "schema": schema,
                "known_good_example": schema.get("example"),
            },
        )

    @server.tool(name="validate_connector_payloads")
    def validate_connector_payloads(
        action: Literal[
            "createInput",
            "alterInput",
            "deleteInput",
            "createOutput",
            "alterOutput",
            "deleteOutput",
        ] = "createInput",
        payload: ConnectorPayload | None = None,
        payloads: ConnectorPayloadBatch | None = None,
    ) -> object:
        if payload is not None and payloads is not None:
            raise _validation_failure(
                tool_name="validate_connector_payloads",
                message="Provide either payload or payloads, but not both",
                expected_args={
                    "action": (
                        "one of createInput/alterInput/deleteInput/"
                        "createOutput/alterOutput/deleteOutput"
                    ),
                    "payload": "object (optional, single preflight)",
                    "payloads": "array<object> (optional, batch preflight)",
                },
                received_args={"action": action, "payload": payload, "payloads": payloads},
                suggested_fix="Send exactly one of payload or payloads.",
                example_payload={
                    "name": "validate_connector_payloads",
                    "arguments": {
                        "action": "createInput",
                        "payload": {
                            "connectorName": "modbus_energy_input",
                            "serviceName": "modbus",
                            "modbusProtocol": "TCP",
                            "modbusServer": "127.0.0.1",
                            "modbusServerPort": 502,
                            "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
                        },
                    },
                },
            )

        if payload is None and payloads is None:
            raise _validation_failure(
                tool_name="validate_connector_payloads",
                message="payload or payloads is required",
                expected_args={
                    "action": (
                        "one of createInput/alterInput/deleteInput/"
                        "createOutput/alterOutput/deleteOutput"
                    ),
                    "payload": "object (optional, single preflight)",
                    "payloads": "array<object> (optional, batch preflight)",
                },
                received_args={"action": action, "payload": payload, "payloads": payloads},
                suggested_fix="Provide one payload object or a list of payload objects.",
                example_payload={
                    "name": "validate_connector_payloads",
                    "arguments": {
                        "action": "createInput",
                        "payloads": [
                            {
                                "connectorName": "modbus_energy_input",
                                "serviceName": "modbus",
                                "modbusProtocol": "TCP",
                                "modbusServer": "127.0.0.1",
                                "modbusServerPort": 502,
                                "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
                            }
                        ],
                    },
                },
            )

        if payloads is not None and not isinstance(payloads, list):
            raise _validation_failure(
                tool_name="validate_connector_payloads",
                message="payloads must be an array when provided",
                expected_args={"payloads": "array<object>"},
                received_args={"payloads": payloads},
                suggested_fix="Wrap payload objects in a JSON array for batch validation.",
                example_payload={
                    "name": "validate_connector_payloads",
                    "arguments": {
                        "payloads": [
                            {
                                "connectorName": "modbus_energy_input",
                                "serviceName": "modbus",
                                "modbusProtocol": "TCP",
                                "modbusServer": "127.0.0.1",
                                "modbusServerPort": 502,
                                "propertyMapList": [{"modbusDataAccess": "holdingregister"}],
                            }
                        ]
                    },
                },
            )

        items: list[ConnectorPayload] = payloads if payloads is not None else [payload]  # type: ignore[list-item]

        def _run_preflight() -> dict[str, object]:
            results: list[dict[str, object]] = []
            valid_count = 0
            invalid_count = 0
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    invalid_count += 1
                    results.append(
                        {
                            "index": index,
                            "status": "invalid",
                            "action": action,
                            "errors": [
                                {
                                    "path": "payload",
                                    "json_pointer": "/payload",
                                    "reason": "invalid_type",
                                    "message": "Payload must be an object",
                                    "expected": "object",
                                    "received": type(item).__name__,
                                }
                            ],
                        }
                    )
                    continue

                try:
                    normalized_payload = _require_connector_payload(
                        tool_name="validate_connector_payloads",
                        payload=item,
                        action=action,
                    )
                except ValidationFailure as exc:
                    invalid_count += 1
                    received_args = exc.details.get("received_args", {})
                    validation_errors = received_args.get("validation_errors")
                    if not isinstance(validation_errors, list) or not validation_errors:
                        validation_errors = [
                            {
                                "path": "payload",
                                "json_pointer": "/payload",
                                "reason": "invalid_arguments",
                                "message": str(exc.message),
                            }
                        ]

                    results.append(
                        {
                            "index": index,
                            "status": "invalid",
                            "action": action,
                            "errors": validation_errors,
                        }
                    )
                    continue

                valid_count += 1
                validation = _validate_connector_schema(
                    tool_name="validate_connector_payloads",
                    action=action,
                    payload=normalized_payload,
                )
                results.append(
                    {
                        "index": index,
                        "status": "valid",
                        "action": action,
                        "schema_service": validation["service_name"],
                        "warnings": validation.get("warnings", []),
                        "normalized_payload": normalized_payload,
                        "forwarded_payload": transform_connector_request(
                            action,
                            normalized_payload,
                        ),
                        "errors": [],
                    }
                )

            return {
                "mode": "preflight",
                "action": action,
                "summary": {
                    "total": len(items),
                    "valid": valid_count,
                    "invalid": invalid_count,
                    "all_valid": invalid_count == 0,
                },
                "results": results,
            }

        return _run_tool("validate_connector_payloads", "admin", _run_preflight)

    @server.tool(name="runtime_status")
    def runtime_status() -> object:
        return _run_tool(
            "runtime_status",
            "admin",
            lambda: {
                "service": "faircom-mcp",
                "tool_group_allowlist": list(resolved_config.security.tool_group_allowlist),
                "metrics_enabled": resolved_config.observability.enable_metrics,
                "tracing_enabled": resolved_config.observability.enable_tracing,
            },
        )

    @server.tool(name="capabilities_summary")
    def capabilities_summary() -> object:
        return _run_tool(
            "capabilities_summary",
            "admin",
            lambda: {
                "service": {
                    "name": "faircom-mcp",
                    "version": __version__,
                    "compatibility": {
                        "faircom": ["Edge", "DB", "RTG", "ISAM", "MQ"],
                        "transport": ["http", "sse", "stdio"],
                    },
                },
                "transport_modes": [
                    {"name": "http", "status": "available"},
                    {"name": "sse", "status": "available"},
                    {"name": "stdio", "status": "available"},
                ],
                "security": {
                    "tool_groups": list(resolved_config.security.tool_group_allowlist),
                    "default_policy": resolved_config.security.policy_preset or "default",
                    "read_write_enabled": "write" in resolved_config.security.tool_group_allowlist,
                    "connector_write_enabled": "connector"
                    in resolved_config.security.tool_group_allowlist,
                    "diagnostics_enabled": resolved_config.security.diagnostics_enabled,
                    "features": ["dry_run", "audit_logging", "policy_enforcement"],
                },
                "tools": [
                    {
                        "name": "list_tables",
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "List tables visible to the configured access context.",
                    },
                    {
                        "name": "describe_table",
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Describe a table schema and basic metadata.",
                    },
                    {
                        "name": "list_table_columns",
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Return column metadata for a table.",
                    },
                    {
                        "name": "list_table_indexes",
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Return indexes for a table.",
                    },
                    {
                        "name": "sql_query",
                        "group": "query",
                        "risk_level": "medium",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "Run a read-only SQL statement and return results. "
                            "Use argument key 'statement' (aliases: sql, query). "
                            "Use TOP/SKIP for row limiting; avoid LIMIT/OFFSET/FETCH."
                        ),
                    },
                    {
                        "name": "sql_query_page",
                        "group": "query",
                        "risk_level": "medium",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "Run paged SQL queries with page-size and continuation control. "
                            "Use argument key 'statement' (aliases: sql, query)."
                        ),
                    },
                    {
                        "name": "get_usage_contract",
                        "group": "admin",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "Return canonical argument keys, aliases, transport notes, and minimal "
                            "payload examples for AI client bootstrap and self-repair."
                        ),
                    },
                    {
                        "name": "describe_connector_schema",
                        "group": "admin",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "Return local connector payload schema profiles and known-good "
                            "examples for supported connector services."
                        ),
                    },
                    {
                        "name": "validate_connector_payloads",
                        "group": "admin",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "Preflight validate one or many connector payloads and return "
                            "deterministic per-item diagnostics without mutating backend state."
                        ),
                    },
                    {
                        "name": "sql_execute",
                        "group": "write",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Execute a write statement with confirmation guardrails "
                            "and optional dry-run preview."
                        ),
                    },
                    {
                        "name": "list_inputs",
                        "aliases": ["listInputs"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "List input connectors visible to the configured access context."
                        ),
                    },
                    {
                        "name": "describe_inputs",
                        "aliases": ["describeInputs"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Describe configured input connectors.",
                    },
                    {
                        "name": "list_outputs",
                        "aliases": ["listOutputs"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "List output connectors visible to the configured access context."
                        ),
                    },
                    {
                        "name": "describe_outputs",
                        "aliases": ["describeOutputs"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Describe configured output connectors.",
                    },
                    {
                        "name": "create_input",
                        "aliases": ["createInput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Create an input connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "alter_input",
                        "aliases": ["alterInput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Alter an input connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "delete_input",
                        "aliases": ["deleteInput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Delete an input connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "create_output",
                        "aliases": ["createOutput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Create an output connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "alter_output",
                        "aliases": ["alterOutput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Alter an output connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "delete_output",
                        "aliases": ["deleteOutput"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Delete an output connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "list_transforms",
                        "aliases": ["listTransforms"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": (
                            "List transform connectors visible to the configured access context."
                        ),
                    },
                    {
                        "name": "describe_transforms",
                        "aliases": ["describeTransforms"],
                        "group": "metadata",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Describe configured transform connectors.",
                    },
                    {
                        "name": "create_transform",
                        "aliases": ["createTransform"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Create a transform connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "alter_transform",
                        "aliases": ["alterTransform"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Alter a transform connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "delete_transform",
                        "aliases": ["deleteTransform"],
                        "group": "connector",
                        "risk_level": "critical",
                        "idempotent": False,
                        "stability": "stable",
                        "description": (
                            "Delete a transform connector with confirmation "
                            "guardrails and dry-run preview."
                        ),
                    },
                    {
                        "name": "runtime_status",
                        "group": "admin",
                        "risk_level": "low",
                        "idempotent": True,
                        "stability": "stable",
                        "description": "Return runtime configuration flags and policy state.",
                    },
                ],
                "metadata": {
                    "documentation": "https://github.com/faircom/faircom-mcp",
                    "audit_logging": True,
                    "observability": resolved_config.observability.enable_metrics,
                },
            },
        )

    @server.tool(name="observability_metrics")
    def observability_metrics() -> object:
        return _run_tool(
            "observability_metrics",
            "admin",
            lambda: {
                "service": "faircom-mcp",
                **metrics.snapshot(),
            },
        )

    @server.tool(name="observability_audit")
    def observability_audit() -> object:
        return _run_tool(
            "observability_audit",
            "admin",
            lambda: {
                "service": "faircom-mcp",
                "events": audit_log.snapshot(),
            },
        )

    @server.tool(name="observability_health")
    def observability_health() -> object:
        return _run_tool(
            "observability_health",
            "admin",
            lambda: {
                "service": "faircom-mcp",
                "status": "ok",
                "details": {
                    "metrics_enabled": resolved_config.observability.enable_metrics,
                    "tracing_enabled": resolved_config.observability.enable_tracing,
                    "diagnostics_enabled": resolved_config.security.diagnostics_enabled,
                },
            },
        )

    @server.tool(name="sql_execute")
    def sql_execute(
        statement: str | None = None,
        params: list[object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
        sql: str | None = None,
        query: str | None = None,
    ) -> object:
        resolved_statement, normalized_args = _resolve_single_string_arg(
            tool_name="sql_execute",
            canonical_name="statement",
            canonical_value=statement,
            aliases={"sql": sql, "query": query},
            expected_args={
                "statement": "string (required)",
                "params": "array (optional)",
                "confirm_write": "boolean (required for writes unless dry_run=true)",
                "dry_run": "boolean (optional)",
            },
            example_payload={
                "name": "sql_execute",
                "arguments": {
                    "statement": "UPDATE demo_assets SET status='active' WHERE id = 1",
                    "confirm_write": True,
                },
            },
        )
        _validate_sql_shape("sql_execute", resolved_statement)
        logger.info(
            "sql_execute requested",
            extra={
                "statement": resolved_statement,
                "params": params,
                "confirm_write": confirm_write,
                "dry_run": dry_run,
            },
        )
        audit_log.record(
            event_type="write_attempt",
            details={
                "statement": resolved_statement,
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            result = _run_tool(
                "sql_execute",
                "write",
                lambda: _with_compatibility(
                    sql_adapter.execute(resolved_statement, params=params, dry_run=True),
                    tool_name="sql_execute",
                    normalized_args=normalized_args,
                ),
            )
            if isinstance(result, dict):
                enriched = dict(result)
                enriched.update(
                    {
                        "dry_run_applied": True,
                        "confirm_write_required": True,
                        "mutation_applied": False,
                    }
                )
                return enriched
            return result
        if not confirm_write:
            raise _validation_failure(
                tool_name="sql_execute",
                message="sql_execute requires confirm_write=True",
                expected_args={
                    "statement": "string (required)",
                    "confirm_write": "true for non-dry-run writes",
                    "dry_run": "true to preview write",
                },
                received_args={
                    "statement": resolved_statement,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix=(
                    "Set confirm_write=true to apply mutation or set dry_run=true for preview."
                ),
                example_payload={
                    "name": "sql_execute",
                    "arguments": {
                        "statement": "UPDATE demo_assets SET status='active' WHERE id = 1",
                        "confirm_write": True,
                    },
                },
                reason_code="missing_write_confirmation",
            )
        result = _run_tool(
            "sql_execute",
            "write",
            lambda: _with_compatibility(
                sql_adapter.execute(resolved_statement, params=params),
                tool_name="sql_execute",
                normalized_args=normalized_args,
            ),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                }
            )
            return enriched
        return result

    _ = resolved_config
    return server


def create_http_app(
    config: AppConfig | None = None,
    *,
    readiness_check: Callable[[], bool] | None = None,
    transport: Literal["http", "sse", "auto"] = "http",
) -> Starlette:
    server = create_server(config, readiness_check=readiness_check)
    # Keep raw SSE behavior unchanged, but route HTTP through the compatibility
    # wrapper so JSON-only clients can still negotiate with streamable HTTP.
    if transport == "sse":
        return server.http_app(transport="sse")

    http_app = server.http_app(transport="http")
    sse_app = server.http_app(transport="sse")
    runtime_metrics = getattr(getattr(server, "state", None), "runtime_metrics", None)

    def _session_recovery_payload(reason_code: str) -> dict[str, Any]:
        return {
            "reinitialize_required": True,
            "reason_code": reason_code,
            "initialize_example": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "client", "version": "1.0"},
                },
            },
        }

    async def _proxy_to_target(
        request: Request,
        target_app: Starlette,
        *,
        path_override: str | None = None,
        accept_override: str | None = None,
    ) -> Response:
        target_url = str(request.url)
        if path_override is not None:
            target_url = str(request.url.replace(path=path_override))
        body = await request.body()
        filtered_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "accept"}
        }
        if accept_override is not None:
            filtered_headers["Accept"] = accept_override
        transport_client = httpx.ASGITransport(app=target_app)
        async with httpx.AsyncClient(
            transport=transport_client, base_url="http://mcp.local"
        ) as client:
            proxied = await client.request(
                request.method,
                target_url,
                headers=filtered_headers,
                content=body,
            )

        response_headers = {
            key: value
            for key, value in proxied.headers.items()
            if key.lower() not in {"content-length", "transfer-encoding", "connection"}
        }
        content_type = proxied.headers.get("content-type", "application/json")
        response = Response(
            content=proxied.content,
            status_code=proxied.status_code,
            headers=response_headers,
            media_type=content_type,
        )

        # Enrich likely session errors with deterministic recovery details.
        if "application/json" in content_type.lower():
            try:
                payload = proxied.json()
            except ValueError:
                return response
            if isinstance(payload, dict):
                serialized = json.dumps(payload).lower()
                reason_code = None
                if "session" in serialized and (
                    "missing" in serialized or "not found" in serialized
                ):
                    reason_code = "missing_session"
                elif "session" in serialized and ("stale" in serialized or "expired" in serialized):
                    reason_code = "stale_session"
                if reason_code is not None:
                    if runtime_metrics is not None:
                        runtime_metrics.record_compat_event(tool="transport", event=reason_code)
                    payload.setdefault("recovery", _session_recovery_payload(reason_code))
                    return JSONResponse(payload, status_code=proxied.status_code)

        return response

    def _extract_json_from_sse(body: bytes) -> dict[str, Any] | None:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            return None

        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            candidate = line[len("data:") :].strip()
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _has_route_path(app: Starlette, path: str) -> bool:
        for route in app.routes:
            route_path = getattr(route, "path", None)
            if route_path == path:
                return True
        return False

    async def mcp_auto(request: Request) -> Response:
        accept = request.headers.get("accept", "").lower()
        wants_sse = "text/event-stream" in accept
        wants_json = "application/json" in accept

        if runtime_metrics is not None and not wants_json and not wants_sse:
            runtime_metrics.record_compat_event(tool="transport", event="parse_mode_mismatch")

        # FastMCP streamable-http transport requires accepting event-stream even
        # for JSON-RPC calls. Normalize for compatibility and post-process for
        # strict JSON callers.
        normalized_accept = accept
        if wants_json or wants_sse:
            normalized_accept = "application/json, text/event-stream"

        target_app = http_app
        path_override = None

        if wants_sse and not wants_json and request.method.upper() == "GET":
            target_app = sse_app
            if _has_route_path(sse_app, "/sse") and not _has_route_path(sse_app, "/mcp"):
                path_override = "/sse"

        response = await _proxy_to_target(
            request,
            target_app,
            path_override=path_override,
            accept_override=normalized_accept,
        )

        if wants_json and not wants_sse:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type.lower():
                parsed = _extract_json_from_sse(bytes(response.body))
                if parsed is not None:
                    serialized = json.dumps(parsed).lower()
                    reason_code = None
                    if "session" in serialized and (
                        "missing" in serialized or "not found" in serialized
                    ):
                        reason_code = "missing_session"
                    elif "session" in serialized and (
                        "stale" in serialized or "expired" in serialized
                    ):
                        reason_code = "stale_session"
                    if reason_code is not None:
                        if runtime_metrics is not None:
                            runtime_metrics.record_compat_event(tool="transport", event=reason_code)
                        parsed.setdefault("recovery", _session_recovery_payload(reason_code))
                    return JSONResponse(parsed, status_code=response.status_code)

        return response

    @asynccontextmanager
    async def _auto_lifespan(_app: Starlette) -> AsyncIterator[None]:
        # FastMCP HTTP/SSE apps require lifespan startup to initialize their
        # internal session manager task groups.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(http_app.router.lifespan_context(http_app))
            await stack.enter_async_context(sse_app.router.lifespan_context(sse_app))
            yield

    return Starlette(
        lifespan=_auto_lifespan,
        routes=[
            Route("/mcp", endpoint=mcp_auto, methods=["POST", "GET"]),
            Mount("/", app=http_app),
        ],
    )


def create_stdio_server(
    config: AppConfig | None = None,
    *,
    readiness_check: Callable[[], bool] | None = None,
) -> FastMCP:
    return create_server(config, readiness_check=readiness_check)
