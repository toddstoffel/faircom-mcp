from __future__ import annotations

import difflib
import html
import json
import logging
import re
import threading
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
from faircom_mcp.errors import (
    FaircomError,
    UpstreamAPIError,
    ValidationFailure,
    normalize_exception,
)
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

# Full codeType enum accepted by listCodePackages' codeTypeFilter, per
# https://documentation.faircom.com/en_US/code-packages-api-actions/listcodepackages
_CODE_PACKAGE_TYPE_ENUM = [
    "integrationTableTransform",
    "expression",
    "getRecordsTransform",
    "globalFunction",
    "module",
    "event",
    "beforeTrigger",
    "afterTrigger",
    "job",
]

# createCodePackage/alterCodePackage only accept this narrower subset, per
# https://documentation.faircom.com/en_US/code-packages-api-actions/createcodepackage
_CODE_PACKAGE_CREATE_TYPE_ENUM = [
    "integrationTableTransform",
    "getRecordsTransform",
]

# transformSteps.transformStepMethod enum, per
# https://documentation.faircom.com/en_US/integration-tables-api-actions/createintegrationtable
_TRANSFORM_STEP_METHOD_ENUM = [
    "javascript",
    "jsonToDifferentTableFields",
    "jsonToTableFields",
    "tableFieldsToJson",
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

# FairCom rejects invalid testTransformScope values without listing the valid enum in its
# error text, so we validate client-side using the enum from FairCom's own docs.
_VALID_TEST_TRANSFORM_SCOPES = {
    "allRecords",
    "stop",
    "firstRecord",
    "lastRecord",
    "specificRecords",
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


class GenericConnectorPayload(TypedDict, total=False):
    connectorName: Required[str]
    inputName: str
    serviceName: str


ConnectorPayload = ModbusConnectorPayload | GenericConnectorPayload
ConnectorPayloadBatch = list[ConnectorPayload]

_CONNECTOR_SCHEMA_REGISTRY: dict[str, dict[str, object]] = {
    "modbus": {
        "service_name": "modbus",
        "required": [
            "connectorName",
            "serviceName",
            "tableName",
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
                        "modbusDataLen": {
                            "type": ["integer", "number"],
                            "description": (
                                "Number of 2-byte registers for the value: 1 for 16-bit "
                                "types, 2 for 32-bit types (e.g. float32ABCD)."
                            ),
                        },
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
            "tableName": "modbus_energy_raw",
            "modbusProtocol": "TCP",
            "modbusServer": "127.0.0.1",
            "modbusServerPort": 502,
            "modbusDataAddressType": "zeroBased",
            "unitId": 1,
            "propertyMapList": [
                {
                    "propertyName": "temperature",
                    "modbusDataAddress": 1199,
                    "modbusDataAccess": "holdingregister",
                    "modbusDataType": "int16SignedAB",
                    "modbusDataLen": 1,
                },
                {
                    "propertyName": "vibration",
                    "modbusDataAddress": 1200,
                    "modbusDataAccess": "holdingregister",
                    "modbusDataType": "float32ABCD",
                    "modbusDataLen": 2,
                },
            ],
        },
    },
}

# Separate from _CONNECTOR_SCHEMA_REGISTRY (createInput/alterInput) because output payloads
# use a different shape: outputName/sourceFields instead of inputName/propertyMapList, and
# connector-specific settings nested under "settings" rather than the input's flat properties.
_CONNECTOR_OUTPUT_SCHEMA_REGISTRY: dict[str, dict[str, object]] = {
    "modbus": {
        "service_name": "modbus",
        "required": ["outputName", "serviceName", "tableName", "sourceFields", "settings"],
        "description": "Modbus output connector payload schema (local validation profile).",
        "properties": {
            "outputName": {"type": "string", "minLength": 1},
            "serviceName": {"type": "string", "const": "modbus"},
            "databaseName": {"type": "string", "minLength": 1},
            "ownerName": {"type": "string", "minLength": 1},
            "tableName": {"type": "string", "minLength": 1},
            "sourceFields": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "settings": {
                "type": "object",
                "properties": {
                    "modbusProtocol": {"type": "string"},
                    "modbusServer": {"type": "string", "minLength": 1},
                    "modbusServerPort": {"type": ["integer", "number"]},
                    "propertyMapList": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "propertyPath": {"type": "string", "minLength": 1},
                                "modbusDataAddress": {"type": ["integer", "number"]},
                                "modbusDataAccess": {
                                    "type": "string",
                                    "enum": _MODBUS_DATA_ACCESS_ENUM,
                                },
                                "modbusUnitId": {"type": ["integer", "number"]},
                                "modbusDataLen": {"type": ["integer", "number"]},
                            },
                        },
                    },
                },
            },
        },
        "example": {
            "outputName": "writeTemperatureToModbus",
            "serviceName": "modbus",
            "databaseName": "faircom",
            "ownerName": "admin",
            "tableName": "modbusTableTCP",
            "sourceFields": ["source_payload"],
            "settings": {
                "modbusProtocol": "TCP",
                "modbusServer": "127.0.0.1",
                "modbusServerPort": 502,
                "propertyMapList": [
                    {
                        "propertyPath": "source_payload.temperature",
                        "modbusDataAddress": 1399,
                        "modbusDataAccess": "holdingregister",
                        "modbusUnitId": 5,
                        "modbusDataLen": 1,
                    }
                ],
            },
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
        return _redact_sensitive_fields(result)

    def _redact_sensitive_fields(value: object) -> object:
        if isinstance(value, dict):
            sanitized: dict[object, object] = {}
            for key, nested_value in value.items():
                if isinstance(key, str) and key.lower() == "authtoken":
                    continue
                sanitized[key] = _redact_sensitive_fields(nested_value)
            return sanitized
        if isinstance(value, list):
            return [_redact_sensitive_fields(item) for item in value]
        return value

    def _connector_target_name(payload: object) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("connectorName", "outputName", "inputName", "tableName", "codeName", "topic"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_service_name(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        for key in ("serviceName", "name", "service", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _collect_service_records(value: object) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    records.append(item)
                    records.extend(_collect_service_records(item))
                elif isinstance(item, list):
                    records.extend(_collect_service_records(item))
            return records
        if isinstance(value, dict):
            direct_name = _extract_service_name(value)
            if direct_name is not None:
                records.append(value)
            for child_key in (
                "services",
                "serviceList",
                "results",
                "data",
                "items",
                "serviceInfo",
            ):
                child = value.get(child_key)
                records.extend(_collect_service_records(child))
            return records
        return []

    def _coerce_runtime_status(entry: dict[str, object]) -> tuple[bool | None, str | None]:
        bool_keys = (
            "active",
            "enabled",
            "running",
            "started",
            "isActive",
            "isEnabled",
            "isRunning",
        )
        for key in bool_keys:
            raw = entry.get(key)
            if isinstance(raw, bool):
                return raw, key
            if isinstance(raw, (int, float)):
                return raw != 0, key
            if isinstance(raw, str):
                normalized = raw.strip().lower()
                if normalized in {"true", "on", "yes", "running", "active", "started", "up"}:
                    return True, key
                if normalized in {
                    "false",
                    "off",
                    "no",
                    "stopped",
                    "inactive",
                    "down",
                    "disabled",
                }:
                    return False, key

        state_keys = ("status", "state", "runtimeStatus", "serviceStatus")
        for key in state_keys:
            raw = entry.get(key)
            if not isinstance(raw, str):
                continue
            normalized = raw.strip().lower()
            if normalized in {"running", "active", "started", "ready", "up", "enabled"}:
                return True, key
            if normalized in {
                "stopped",
                "inactive",
                "down",
                "disabled",
                "paused",
                "error",
            }:
                return False, key
        return None, None

    def _list_service_runtime_state(
        service_names: set[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        admin_action = getattr(client, "admin_action", None)
        if not callable(admin_action):
            return {}

        candidate_payloads: list[dict[str, object] | None] = []
        if service_names:
            sorted_names = sorted(name.strip() for name in service_names if name.strip())
            if sorted_names:
                candidate_payloads.extend(
                    [
                        {"serviceNames": sorted_names},
                        {"names": sorted_names},
                        {"services": sorted_names},
                    ]
                )
        candidate_payloads.append(None)

        last_error: Exception | None = None
        for service_payload in candidate_payloads:
            try:
                response = admin_action("listServices", service_payload)
            except Exception as exc:  # pragma: no cover - defensive fallback
                last_error = exc
                continue

            runtime_state: dict[str, dict[str, object]] = {}
            for record in _collect_service_records(response):
                service_name = _extract_service_name(record)
                if service_name is None:
                    continue
                active, active_source = _coerce_runtime_status(record)
                runtime_state[service_name.lower()] = {
                    "service_name": service_name,
                    "active": active,
                    "active_source": active_source,
                    "raw": record,
                }
            if runtime_state:
                return runtime_state

        if last_error is not None:
            logger.debug("listServices runtime lookup failed", exc_info=last_error)
        return {}

    def _execute_connector_write(
        *,
        tool_name: str,
        action: str,
        target: str | None,
        writer: Callable[[], object],
        post_commit_verifier: Callable[[object], None] | None = None,
    ) -> object:
        def _coerce_error_code(value: object) -> int | None:
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                raw = value.strip()
                if not raw:
                    return None
                try:
                    return int(raw)
                except ValueError:
                    return None
            return None

        def _record_connector_write_result(
            outcome: str,
            *,
            extra: dict[str, object] | None = None,
        ) -> None:
            details: dict[str, object] = {
                "tool": tool_name,
                "action": action,
                "target": target,
                "outcome": outcome,
            }
            if extra:
                details.update(extra)
            audit_log.record(
                event_type="connector_write_result",
                details=details,
            )

        try:
            result = writer()
        except FaircomError as exc:
            _record_connector_write_result(
                "failed",
                extra={
                    "error_code": str(exc.code),
                    "error_message": exc.message,
                },
            )
            raise
        except Exception as exc:
            _record_connector_write_result(
                "failed",
                extra={
                    "error_code": "unexpected_exception",
                    "error_message": str(exc),
                },
            )
            raise

        try:
            if isinstance(result, dict):
                error_code = _coerce_error_code(result.get("errorCode"))
                if error_code is not None and error_code != 0:
                    raise UpstreamAPIError(
                        "FairCom connector action returned an application error",
                        details={
                            "errorCode": error_code,
                            "errorMessage": result.get("errorMessage"),
                            "request_action": action,
                            "tool": tool_name,
                            "target": target,
                            "response": result,
                        },
                        retryable=False,
                    )

            if post_commit_verifier is not None:
                post_commit_verifier(result)
        except FaircomError as exc:
            _record_connector_write_result(
                "failed",
                extra={
                    "error_code": str(exc.code),
                    "error_message": exc.message,
                },
            )
            raise
        except Exception as exc:
            _record_connector_write_result(
                "failed",
                extra={
                    "error_code": "unexpected_exception",
                    "error_message": str(exc),
                },
            )
            raise

        _record_connector_write_result("success")
        return result

    def _record_connector_validation_rejection(
        *,
        tool_name: str,
        action: str,
        payload: dict[str, object] | None,
        exc: ValidationFailure,
    ) -> None:
        audit_log.record(
            event_type="connector_write_result",
            details={
                "tool": tool_name,
                "action": action,
                "target": _connector_target_name(payload),
                "outcome": "rejected",
                "reason_code": exc.details.get("reason_code", "validation_error"),
                "error_message": exc.message,
            },
        )

    def _execute_code_package_write(
        *,
        tool_name: str,
        code_name: str,
        operation: str,
        writer: Callable[[], object],
    ) -> object:
        try:
            result = writer()
        except FaircomError as exc:
            audit_log.record(
                event_type="code_package_write_result",
                details={
                    "tool": tool_name,
                    "code_name": code_name,
                    "operation": operation,
                    "outcome": "failed",
                    "error_code": str(exc.code),
                    "error_message": exc.message,
                },
            )
            raise
        except Exception as exc:
            audit_log.record(
                event_type="code_package_write_result",
                details={
                    "tool": tool_name,
                    "code_name": code_name,
                    "operation": operation,
                    "outcome": "failed",
                    "error_code": "unexpected_exception",
                    "error_message": str(exc),
                },
            )
            raise

        audit_log.record(
            event_type="code_package_write_result",
            details={
                "tool": tool_name,
                "code_name": code_name,
                "operation": operation,
                "outcome": "success",
            },
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
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        normalized_payload = payload
        try:
            normalized_payload = _require_connector_payload(
                tool_name=tool_name,
                payload=payload,
                action=action,
            )
        except ValidationFailure as exc:
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
            validation = _validate_connector_schema(
                tool_name=tool_name,
                action=action,
                payload=payload,
            )
            validation_warnings = cast(list[str], validation.get("warnings", []))
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
                "validation_errors": validation_errors,
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                    *validation_warnings,
                ],
                "hint": "Fix validation_errors and run dry_run again before confirm_write=True.",
            }

        validation = _validate_connector_schema(
            tool_name=tool_name,
            action=action,
            payload=normalized_payload,
        )
        validation_warnings = cast(list[str], validation.get("warnings", []))
        schema_validated = validation["status"] == "validated"
        schema_outcome = "schema_valid" if schema_validated else "unvalidated"
        forwarded_payload = transform_connector_request(action, normalized_payload)
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
            "payload": normalized_payload,
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
                "target": normalized_payload or {},
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

    def _check_service_registration(
        *,
        action: str,
        service_name: str,
    ) -> tuple[list[dict[str, object]], list[str]]:
        if action not in {"createInput", "alterInput", "createOutput", "alterOutput"}:
            return [], []

        registered = _list_service_runtime_state()
        if not registered:
            # Could not determine the registered service list (or the call failed); skip
            # rather than block writes on a lookup we have no data for.
            return [], []

        entry = registered.get(service_name)
        if entry is None:
            registered_names = sorted(
                {
                    str(info["service_name"])
                    for info in registered.values()
                    if info.get("service_name")
                }
            )
            return (
                [
                    {
                        "path": "payload.serviceName",
                        "json_pointer": "/payload/serviceName",
                        "reason": "unregistered_service",
                        "message": (
                            f"serviceName '{service_name}' is not a registered integration "
                            "service on this FairCom Edge instance."
                        ),
                        "registered_services": registered_names,
                    }
                ],
                [],
            )

        if entry.get("active") is False:
            return (
                [],
                [
                    f"Service '{service_name}' is registered but disabled. Enable it with "
                    "manage_service (command=startup) before writing this connector."
                ],
            )

        return [], []

    def _validate_connector_schema(
        *,
        tool_name: str,
        action: str,
        payload: dict[str, object] | None,
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

        if action in {"deleteInput", "deleteOutput", "deleteIntegrationTables"}:
            if action == "deleteInput":
                input_name = payload_data.get("inputName")
                connector_name = payload_data.get("connectorName")
                has_identifier = (
                    isinstance(input_name, str)
                    and bool(input_name.strip())
                    or isinstance(connector_name, str)
                    and bool(connector_name.strip())
                )
                if not has_identifier:
                    return {
                        "status": "invalid",
                        "service_name": None,
                        "errors": [
                            {
                                "path": "payload",
                                "json_pointer": "/payload",
                                "reason": "required",
                                "message": "deleteInput requires inputName or connectorName",
                            }
                        ],
                        "warnings": [],
                    }
            elif action == "deleteOutput":
                connector_name = payload_data.get("connectorName")
                has_identifier = isinstance(connector_name, str) and bool(connector_name.strip())
                if not has_identifier:
                    return {
                        "status": "invalid",
                        "service_name": None,
                        "errors": [
                            {
                                "path": "payload",
                                "json_pointer": "/payload",
                                "reason": "required",
                                "message": "deleteOutput requires connectorName",
                            }
                        ],
                        "warnings": [],
                    }
            else:
                table_names = payload_data.get("tableNames")
                has_identifier = isinstance(table_names, list) and any(
                    isinstance(name, str) and name.strip() for name in table_names
                )
                if not has_identifier:
                    return {
                        "status": "invalid",
                        "service_name": None,
                        "errors": [
                            {
                                "path": "payload",
                                "json_pointer": "/payload",
                                "reason": "required",
                                "message": "deleteIntegrationTables requires tableNames (array)",
                            }
                        ],
                        "warnings": [],
                    }

            return {
                "status": "validated",
                "service_name": None,
                "errors": [],
                "warnings": [],
            }

        service_name_raw = payload_data.get("serviceName")
        if not isinstance(service_name_raw, str) or not service_name_raw.strip():
            return {
                "status": "unvalidated",
                "service_name": None,
                "errors": [],
                "warnings": [],
            }

        service_name = service_name_raw.strip().lower()
        service_errors, service_warnings = _check_service_registration(
            action=action,
            service_name=service_name,
        )
        is_output_action = action in {"createOutput", "alterOutput"}
        schema_registry = (
            _CONNECTOR_OUTPUT_SCHEMA_REGISTRY if is_output_action else (_CONNECTOR_SCHEMA_REGISTRY)
        )
        schema = schema_registry.get(service_name)
        if schema is None:
            return {
                "status": "invalid" if service_errors else "unvalidated",
                "service_name": service_name,
                "errors": service_errors,
                "warnings": service_warnings,
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

        errors: list[dict[str, object]] = list(service_errors)
        warnings: list[str] = list(service_warnings)

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
        payload: dict[str, object] | None,
        action: str,
    ) -> dict[str, object]:
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
        is_output_action = action in {"createOutput", "alterOutput", "deleteOutput"}
        connector_name = normalized_payload.get("connectorName")
        if (not isinstance(connector_name, str) or not connector_name.strip()) and is_input_action:
            input_name = normalized_payload.get("inputName")
            if isinstance(input_name, str) and input_name.strip():
                connector_name = input_name.strip()
                normalized_payload["connectorName"] = connector_name
        if (not isinstance(connector_name, str) or not connector_name.strip()) and is_output_action:
            output_name = normalized_payload.get("outputName")
            if isinstance(output_name, str) and output_name.strip():
                connector_name = output_name.strip()
                normalized_payload["connectorName"] = connector_name

        if not isinstance(connector_name, str) or not connector_name.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="connectorName is required in connector payload",
                expected_args={
                    "payload": "object (required)",
                    "payload.connectorName": "string (required)",
                    "payload.inputName": "string (accepted alias for input actions)",
                    "payload.outputName": "string (accepted alias for output actions)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload, "action": action},
                suggested_fix=(
                    "Provide payload.connectorName with a non-empty connector name. "
                    "For input actions, payload.inputName is also accepted; for output "
                    "actions, payload.outputName is also accepted."
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
        if is_output_action:
            output_name = normalized_payload.get("outputName")
            if not isinstance(output_name, str) or not output_name.strip():
                normalized_payload["outputName"] = connector_name

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

        if is_output_action:
            # Output payloads validate against the nested-settings canonical shape (matching
            # what FairCom's wire format expects), so nest flat convenience properties before
            # validation runs instead of only at write/preview time.
            transformed_for_validation = transform_connector_request(action, normalized_payload)
            if transformed_for_validation is not None:
                normalized_payload = transformed_for_validation

        validation = _validate_connector_schema(
            tool_name=tool_name,
            action=action,
            payload=normalized_payload,
        )
        if validation["status"] == "invalid":
            first_issue: dict[str, object] | None = None
            issues = validation.get("errors")
            if isinstance(issues, list) and issues and isinstance(issues[0], dict):
                first_issue = cast(dict[str, object], issues[0])

            if first_issue is None:
                validation_message = "connector payload failed local schema validation"
            else:
                issue_path = first_issue.get("path")
                issue_reason = first_issue.get("reason")
                issue_text = first_issue.get("message")
                validation_message = (
                    f"connector payload failed local schema validation: {issue_path or 'payload'}"
                )
                if isinstance(issue_reason, str) and issue_reason:
                    validation_message += f" ({issue_reason})"
                if isinstance(issue_text, str) and issue_text:
                    validation_message += f" - {issue_text}"

            raise _validation_failure(
                tool_name=tool_name,
                message=validation_message,
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

        return normalized_payload

    def _validate_manage_service_payload(
        *,
        tool_name: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        if payload is None or not payload:
            raise _validation_failure(
                tool_name=tool_name,
                message="payload is required",
                expected_args={"payload": "object (required)"},
                received_args={"payload": payload},
                suggested_fix="Provide a non-empty manageService payload.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                    },
                },
            )

        normalized_payload = dict(payload)
        service_name = normalized_payload.get("serviceName")
        if not isinstance(service_name, str) or not service_name.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="serviceName is required in manage_service payload",
                expected_args={"payload.serviceName": "string (required)"},
                received_args={"payload": normalized_payload},
                suggested_fix="Provide payload.serviceName with a non-empty value.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                    },
                },
            )
        normalized_payload["serviceName"] = service_name.strip()

        allowed_fields = {"serviceName", "command"}
        unexpected_fields = sorted(
            key for key in normalized_payload if isinstance(key, str) and key not in allowed_fields
        )
        if unexpected_fields:
            raise _validation_failure(
                tool_name=tool_name,
                message="manage_service payload contains unsupported fields",
                expected_args={
                    "payload": ("serviceName plus command: pause/resume/restart/shutdown/startup")
                },
                received_args={
                    "payload": normalized_payload,
                    "unsupported_fields": unexpected_fields,
                },
                suggested_fix="Use payload.serviceName and payload.command only.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                    },
                },
            )

        command_values = {
            "pause",
            "resume",
            "restart",
            "shutdown",
            "startup",
        }
        raw_command = normalized_payload.get("command")
        if not isinstance(raw_command, str) or not raw_command.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="manage_service payload requires payload.command",
                expected_args={
                    "payload": (
                        "include payload.command with one of: pause/resume/restart/shutdown/startup"
                    )
                },
                received_args={"payload": normalized_payload},
                suggested_fix="Provide payload.command with a supported value.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                    },
                },
            )

        normalized_command = raw_command.strip().lower()
        normalized_payload["command"] = normalized_command
        if normalized_command not in command_values:
            raise _validation_failure(
                tool_name=tool_name,
                message="Unsupported command value for manage_service",
                expected_args={"payload.command": f"one of {sorted(command_values)}"},
                received_args={"payload": normalized_payload},
                suggested_fix="Use a documented manage_service command value.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                    },
                },
            )

        return normalized_payload

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
        is_ready = True
        reason = "ok"
        if readiness_check is not None:
            result_holder: dict[str, bool] = {"value": True}

            def _run_readiness_check() -> None:
                result_holder["value"] = bool(readiness_check())

            thread = threading.Thread(target=_run_readiness_check, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            if thread.is_alive():
                is_ready = False
                reason = "timeout"
            else:
                is_ready = result_holder["value"]
                reason = "ok" if is_ready else "not_ready"

        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status, "reason": reason}, status_code=status_code)

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        is_ready = True
        reason = "ok"
        if readiness_check is not None:
            result_holder: dict[str, bool] = {"value": True}

            def _run_readiness_check() -> None:
                result_holder["value"] = bool(readiness_check())

            thread = threading.Thread(target=_run_readiness_check, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            if thread.is_alive():
                is_ready = False
                reason = "timeout"
            else:
                is_ready = result_holder["value"]
                reason = "ok" if is_ready else "not_ready"

        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status, "reason": reason}, status_code=status_code)

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

    @server.tool(name="describe_inputs")
    def describe_inputs(payload: dict[str, object] | None = None) -> object:
        def _normalize_input_descriptions(
            value: object,
            *,
            runtime_map: dict[str, dict[str, object]],
        ) -> object:
            if isinstance(value, list):
                return [
                    _normalize_input_descriptions(item, runtime_map=runtime_map) for item in value
                ]
            if isinstance(value, dict):
                normalized = {
                    key: _normalize_input_descriptions(nested, runtime_map=runtime_map)
                    for key, nested in value.items()
                }
                for container_name in (
                    "settings",
                    "config",
                    "configuration",
                    "options",
                    "inputSettings",
                ):
                    container = normalized.get(container_name)
                    if not isinstance(container, dict):
                        continue
                    if "enabled" not in normalized and "enabled" in container:
                        normalized["enabled"] = container.get("enabled")
                    container.pop("enabled", None)
                    if "description" not in normalized and "description" in container:
                        normalized["description"] = container.get("description")
                    container.pop("description", None)

                service_name = normalized.get("serviceName")
                if isinstance(service_name, str) and service_name.strip():
                    runtime_entry = runtime_map.get(service_name.strip().lower())
                    if runtime_entry is not None:
                        normalized.setdefault("runtime_service_state", runtime_entry)
                return normalized
            return value

        def _collect_service_names(value: object, names: set[str]) -> None:
            if isinstance(value, list):
                for item in value:
                    _collect_service_names(item, names)
                return
            if not isinstance(value, dict):
                return
            for key, nested in value.items():
                if key == "serviceName" and isinstance(nested, str) and nested.strip():
                    names.add(nested.strip())
                else:
                    _collect_service_names(nested, names)

        def _describe_with_runtime() -> object:
            described = connector_adapter.describe_inputs(payload)
            service_names: set[str] = set()
            _collect_service_names(described, service_names)
            runtime_map = _list_service_runtime_state(service_names)
            return _normalize_input_descriptions(described, runtime_map=runtime_map)

        return _run_tool(
            "describe_inputs",
            "metadata",
            _describe_with_runtime,
        )

    @server.tool(name="list_services")
    def list_services(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_services",
            "admin",
            lambda: client.admin_action("listServices", payload),
        )

    @server.tool(name="manage_service")
    def manage_service(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        validated_payload = _validate_manage_service_payload(
            tool_name="manage_service",
            payload=payload,
        )

        if dry_run:
            return {
                "mode": "dry_run",
                "status": "success",
                "tool_name": "manage_service",
                "action": "manageService",
                "payload": validated_payload,
                "execution_status": "not_executed",
                "preview": "Service management change preview only",
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                    "Use confirm_write=true to execute manageService upstream.",
                ],
            }

        if not confirm_write:
            raise _validation_failure(
                tool_name="manage_service",
                message="manage_service requires confirm_write=True",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={
                    "payload": payload,
                    "confirm_write": confirm_write,
                    "dry_run": dry_run,
                    "confirm_write_required": True,
                },
                suggested_fix="Set confirm_write=true to apply the service change.",
                example_payload={
                    "name": "manage_service",
                    "arguments": {
                        "payload": {"serviceName": "modbus", "command": "pause"},
                        "confirm_write": True,
                    },
                },
                reason_code="missing_write_confirmation",
            )

        result = _run_tool(
            "manage_service",
            "admin",
            lambda: client.admin_action("manageService", validated_payload),
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

    @server.tool(name="create_input")
    def create_input(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "create_input",
                "action": "createInput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        try:
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
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="create_input",
                action="createInput",
                payload=payload,
                exc=exc,
            )
            raise
        result = _execute_connector_write(
            tool_name="create_input",
            action="createInput",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "create_input",
                "connector",
                lambda: connector_adapter.create_input(resolved_payload),
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

    @server.tool(name="alter_input")
    def alter_input(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        _missing_value = object()

        def _extract_input_records(value: object) -> list[dict[str, object]]:
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if not isinstance(value, dict):
                return []

            direct_records: list[dict[str, object]] = []
            for key in ("inputs", "results", "data", "items"):
                nested = value.get(key)
                if isinstance(nested, list):
                    direct_records.extend(entry for entry in nested if isinstance(entry, dict))
            if direct_records:
                return direct_records
            return [value]

        def _coerce_comparable(value: object) -> object:
            if isinstance(value, str):
                stripped = value.strip()
                lowered = stripped.lower()
                if lowered in {"true", "yes", "on", "enabled", "running", "active"}:
                    return True
                if lowered in {"false", "no", "off", "disabled", "stopped", "inactive"}:
                    return False
                if stripped.isdigit():
                    try:
                        return int(stripped)
                    except ValueError:
                        return stripped
                return stripped
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value

        def _extract_observed_value(record: dict[str, object], key: str) -> object:
            if key in record:
                return record.get(key, _missing_value)
            settings = record.get("settings")
            if isinstance(settings, dict) and key in settings:
                return settings.get(key, _missing_value)
            return _missing_value

        def _verify_alter_input_mutation(resolved: dict[str, object]) -> dict[str, object]:
            verify_timeout_seconds = 6.0
            verify_poll_interval_seconds = 0.3
            input_name = resolved.get("inputName")
            if not isinstance(input_name, str) or not input_name.strip():
                connector_name = resolved.get("connectorName")
                if isinstance(connector_name, str) and connector_name.strip():
                    input_name = connector_name.strip()
            if not isinstance(input_name, str) or not input_name.strip():
                return {
                    "status": "skipped",
                    "reason": "missing_input_identity",
                    "message": "inputName/connectorName was not available for verification",
                }

            verify_keys = [
                key
                for key in (
                    "dataCollectionIntervalMilliseconds",
                    "enabled",
                    "description",
                    "tableName",
                    "transformName",
                )
                if key in resolved
            ]
            if not verify_keys:
                return {
                    "status": "skipped",
                    "reason": "no_verifiable_fields",
                    "message": "No verifiable mutable fields were present in the payload",
                }

            deadline = time.monotonic() + verify_timeout_seconds
            attempt_count = 0
            latest_mismatches: list[dict[str, object]] = []
            latest_record: dict[str, object] | None = None
            latest_reason = "input_not_found"
            observed_fields: set[str] = set()
            last_observed_values: dict[str, object] = {}

            while True:
                attempt_count += 1
                described = connector_adapter.describe_inputs({"inputName": input_name})
                records = _extract_input_records(described)
                matching_record: dict[str, object] | None = None
                for record in records:
                    candidate = record.get("inputName")
                    if isinstance(candidate, str) and candidate.strip() == input_name:
                        matching_record = record
                        break
                if matching_record is None and records:
                    matching_record = records[0]

                if matching_record is not None:
                    latest_record = matching_record
                    mismatches: list[dict[str, object]] = []
                    comparable_fields: list[str] = []
                    for key in verify_keys:
                        expected = resolved.get(key)
                        observed = _extract_observed_value(matching_record, key)
                        if observed is _missing_value:
                            continue

                        observed_fields.add(key)
                        comparable_fields.append(key)
                        last_observed_values[key] = observed
                        expected_normalized = _coerce_comparable(expected)
                        observed_normalized = _coerce_comparable(observed)
                        if observed_normalized != expected_normalized:
                            mismatches.append(
                                {
                                    "field": key,
                                    "expected": expected,
                                    "observed": observed,
                                    "expected_normalized": expected_normalized,
                                    "observed_normalized": observed_normalized,
                                }
                            )

                    if comparable_fields and not mismatches:
                        return {
                            "status": "verified",
                            "inputName": input_name,
                            "verified_fields": comparable_fields,
                            "requested_fields": verify_keys,
                            "attempts": attempt_count,
                            "poll_timeout_seconds": verify_timeout_seconds,
                        }

                    if comparable_fields:
                        latest_reason = "mutation_not_applied"
                        latest_mismatches = mismatches
                    else:
                        latest_reason = "verification_fields_not_observable"

                if time.monotonic() >= deadline:
                    break
                time.sleep(verify_poll_interval_seconds)

            raise UpstreamAPIError(
                "alter_input write acknowledged but post-commit verification timed out",
                details={
                    "errorCode": 0,
                    "verification_error_code": "post_commit_verification_timeout",
                    "request_action": "alterInput",
                    "inputName": input_name,
                    "reason_code": latest_reason,
                    "mismatches": latest_mismatches,
                    "observed_fields": sorted(observed_fields),
                    "requested_fields": verify_keys,
                    "last_observed_values": last_observed_values,
                    "verification_source": latest_record,
                    "verification_attempts": attempt_count,
                    "verification_timeout_seconds": verify_timeout_seconds,
                },
                retryable=False,
                hint=(
                    "The write call returned success but read-after-write verification did not "
                    "converge before timeout. Confirm the latest state with describe_inputs "
                    "before retrying to avoid duplicate writes."
                ),
            )

        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "alter_input",
                "action": "alterInput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        try:
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
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="alter_input",
                action="alterInput",
                payload=payload,
                exc=exc,
            )
            raise

        verification: dict[str, object] | None = None

        def _post_commit_verify(_result: object) -> None:
            nonlocal verification
            verification = _verify_alter_input_mutation(resolved_payload)

        try:
            result = _execute_connector_write(
                tool_name="alter_input",
                action="alterInput",
                target=_connector_target_name(resolved_payload),
                writer=lambda: _run_tool(
                    "alter_input",
                    "connector",
                    lambda: connector_adapter.alter_input(resolved_payload),
                ),
                post_commit_verifier=_post_commit_verify,
            )
        except FaircomError as exc:
            details = exc.details if isinstance(exc.details, dict) else {}
            error_code = details.get("errorCode")
            error_message = details.get("errorMessage")
            normalized_error_code = str(error_code).strip() if error_code is not None else ""
            message_text = error_message.lower() if isinstance(error_message, str) else ""
            looks_like_inactive_service = normalized_error_code == "12048" or (
                "service" in message_text and "active" in message_text
            )
            if looks_like_inactive_service:
                guidance = {
                    "reason_code": "service_inactive",
                    "message": (
                        "The target input service appears inactive. Use list_services to inspect "
                        "runtime state, then manage_service with "
                        "confirm_write=true to start up "
                        "the service before retrying alter_input."
                    ),
                    "service_name": resolved_payload.get("serviceName"),
                    "list_services_example": {
                        "name": "list_services",
                        "arguments": {
                            "payload": {
                                "serviceNames": [resolved_payload.get("serviceName")],
                            }
                        },
                    },
                    "manage_service_example": {
                        "name": "manage_service",
                        "arguments": {
                            "payload": {
                                "serviceName": resolved_payload.get("serviceName"),
                                "command": "startup",
                            },
                            "confirm_write": True,
                        },
                    },
                }
                if isinstance(exc.details, dict):
                    exc.details.setdefault("recovery", guidance)
                else:
                    exc.details = {"recovery": guidance}
            raise
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": bool(
                        verification and verification.get("status") == "verified"
                    ),
                    "mutation_verification": verification,
                }
            )
            return enriched
        return result

    @server.tool(name="delete_input")
    def delete_input(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "delete_input",
                "action": "deleteInput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        result = _execute_connector_write(
            tool_name="delete_input",
            action="deleteInput",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "delete_input",
                "connector",
                lambda: connector_adapter.delete_input(resolved_payload),
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

    @server.tool(name="list_outputs")
    def list_outputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_outputs",
            "metadata",
            lambda: connector_adapter.list_outputs(payload),
        )

    @server.tool(name="describe_outputs")
    def describe_outputs(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_outputs",
            "metadata",
            lambda: connector_adapter.describe_outputs(payload),
        )

    @server.tool(name="create_output")
    def create_output(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "create_output",
                "action": "createOutput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        result = _execute_connector_write(
            tool_name="create_output",
            action="createOutput",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "create_output",
                "connector",
                lambda: connector_adapter.create_output(resolved_payload),
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

    @server.tool(name="alter_output")
    def alter_output(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "alter_output",
                "action": "alterOutput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        result = _execute_connector_write(
            tool_name="alter_output",
            action="alterOutput",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "alter_output",
                "connector",
                lambda: connector_adapter.alter_output(resolved_payload),
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

    @server.tool(name="delete_output")
    def delete_output(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "delete_output",
                "action": "deleteOutput",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
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
        result = _execute_connector_write(
            tool_name="delete_output",
            action="deleteOutput",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "delete_output",
                "connector",
                lambda: connector_adapter.delete_output(resolved_payload),
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

    def _require_table_payload(
        *,
        tool_name: str,
        payload: dict[str, object] | None,
        require_table_names: bool = False,
    ) -> dict[str, object]:
        if payload is None or not isinstance(payload, dict) or not payload:
            raise _validation_failure(
                tool_name=tool_name,
                message="payload is required",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload},
                suggested_fix="Provide a non-empty integration table payload object.",
                example_payload={
                    "name": tool_name,
                    "arguments": {"payload": {"tableName": "modbus_factory_floor_raw"}},
                },
            )
        if require_table_names:
            table_names = payload.get("tableNames")
            if not isinstance(table_names, list) or not any(
                isinstance(name, str) and name.strip() for name in table_names
            ):
                raise _validation_failure(
                    tool_name=tool_name,
                    message="payload.tableNames is required and must be a non-empty array",
                    expected_args={"payload.tableNames": "array of string (required)"},
                    received_args={"payload": payload},
                    suggested_fix="Provide payload.tableNames as a non-empty array of table names.",
                    example_payload={
                        "name": tool_name,
                        "arguments": {"payload": {"tableNames": ["modbus_factory_floor_raw"]}},
                    },
                )
        else:
            table_name = payload.get("tableName")
            if not isinstance(table_name, str) or not table_name.strip():
                raise _validation_failure(
                    tool_name=tool_name,
                    message="payload.tableName is required",
                    expected_args={"payload.tableName": "string (required)"},
                    received_args={"payload": payload},
                    suggested_fix="Provide payload.tableName with a non-empty table name.",
                    example_payload={
                        "name": tool_name,
                        "arguments": {"payload": {"tableName": "modbus_factory_floor_raw"}},
                    },
                )
        return dict(payload)

    def _table_write_preview(
        *,
        tool_name: str,
        action: str,
        payload: dict[str, object] | None,
        require_table_names: bool = False,
    ) -> dict[str, object]:
        try:
            resolved_payload = _require_table_payload(
                tool_name=tool_name,
                payload=payload,
                require_table_names=require_table_names,
            )
        except ValidationFailure as exc:
            return {
                "mode": "dry_run",
                "status": "invalid",
                "tool_name": tool_name,
                "action": action,
                "payload": payload,
                "execution_status": "not_executed",
                "preview": "Integration table payload failed local validation",
                "validation_errors": [
                    {
                        "path": "payload",
                        "json_pointer": "/payload",
                        "reason": "invalid_arguments",
                        "message": str(exc.message),
                    }
                ],
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                ],
                "hint": "Fix validation_errors and run dry_run again before confirm_write=True.",
            }
        return {
            "mode": "dry_run",
            "status": "valid",
            "tool_name": tool_name,
            "action": action,
            "payload": resolved_payload,
            "execution_status": "not_executed",
            "preview": "Integration table action would execute",
            "warnings": [
                "Dry run is a local preview only and does not call FairCom backend APIs.",
            ],
            "hint": "Set confirm_write=true to apply this change.",
        }

    @server.tool(name="list_integration_tables")
    def list_integration_tables(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_integration_tables",
            "metadata",
            lambda: connector_adapter.list_integration_tables(payload),
        )

    @server.tool(name="describe_integration_tables")
    def describe_integration_tables(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_integration_tables",
            "metadata",
            lambda: connector_adapter.describe_integration_tables(payload),
        )

    @server.tool(name="create_integration_table")
    def create_integration_table(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "create_integration_table",
                "action": "createIntegrationTable",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "create_integration_table",
                "connector",
                lambda: _table_write_preview(
                    tool_name="create_integration_table",
                    action="createIntegrationTable",
                    payload=payload,
                ),
            )
        try:
            resolved_payload = _require_table_payload(
                tool_name="create_integration_table",
                payload=payload,
            )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="create_integration_table",
                    message="create_integration_table requires confirm_write=True",
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "create_integration_table",
                        "arguments": {"payload": {"tableName": "modbus_factory_floor_raw"}},
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="create_integration_table",
                action="createIntegrationTable",
                payload=payload,
                exc=exc,
            )
            raise
        result = _execute_connector_write(
            tool_name="create_integration_table",
            action="createIntegrationTable",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "create_integration_table",
                "connector",
                lambda: connector_adapter.create_integration_table(resolved_payload),
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

    @server.tool(name="alter_integration_table")
    def alter_integration_table(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        _missing_value = object()

        def _extract_table_records(value: object) -> list[dict[str, object]]:
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if not isinstance(value, dict):
                return []

            direct_records: list[dict[str, object]] = []
            for key in ("tables", "results", "data", "items"):
                nested = value.get(key)
                if isinstance(nested, list):
                    direct_records.extend(entry for entry in nested if isinstance(entry, dict))
            if direct_records:
                return direct_records
            return [value]

        def _coerce_comparable(value: object) -> object:
            if isinstance(value, str):
                stripped = value.strip()
                lowered = stripped.lower()
                if lowered in {"true", "yes", "on", "enabled", "running", "active"}:
                    return True
                if lowered in {"false", "no", "off", "disabled", "stopped", "inactive"}:
                    return False
                if stripped.isdigit():
                    try:
                        return int(stripped)
                    except ValueError:
                        return stripped
                return stripped
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value

        def _extract_field_names(record: dict[str, object]) -> set[str]:
            fields = record.get("fields")
            names: set[str] = set()
            if isinstance(fields, list):
                for entry in fields:
                    if isinstance(entry, dict):
                        name = entry.get("name")
                        if isinstance(name, str):
                            names.add(name)
            return names

        def _extract_transform_step_code_names(record: dict[str, object]) -> set[str]:
            steps = record.get("transformSteps")
            names: set[str] = set()
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        code_name = step.get("codeName")
                        if isinstance(code_name, str):
                            names.add(code_name)
            return names

        def _verify_alter_integration_table_mutation(
            resolved: dict[str, object],
        ) -> dict[str, object]:
            verify_timeout_seconds = 6.0
            verify_poll_interval_seconds = 0.3
            table_name = resolved.get("tableName")
            if not isinstance(table_name, str) or not table_name.strip():
                return {
                    "status": "skipped",
                    "reason": "missing_table_identity",
                    "message": "tableName was not available for verification",
                }

            new_table_name = resolved.get("newTableName")
            expected_table_name = (
                new_table_name.strip()
                if isinstance(new_table_name, str) and new_table_name.strip()
                else table_name
            )

            describe_request: dict[str, object] = {"tableName": expected_table_name}
            if "databaseName" in resolved:
                describe_request["databaseName"] = resolved["databaseName"]
            if "ownerName" in resolved:
                describe_request["ownerName"] = resolved["ownerName"]

            scalar_keys = [
                key
                for key in (
                    "disableTransformSteps",
                    "logTransformOverwrites",
                    "retentionPolicy",
                    "retentionPeriod",
                    "retentionUnit",
                )
                if key in resolved
            ]
            requested_add_fields = resolved.get("addFields")
            add_field_names: set[str] = (
                {
                    name
                    for entry in requested_add_fields
                    if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
                }
                if isinstance(requested_add_fields, list)
                else set()
            )
            requested_delete_fields = resolved.get("deleteFields")
            delete_field_names: set[str] = (
                {name for name in requested_delete_fields if isinstance(name, str)}
                if isinstance(requested_delete_fields, list)
                else set()
            )
            requested_transform_steps = resolved.get("transformSteps")
            expected_transform_code_names: set[str] = (
                {
                    name
                    for step in requested_transform_steps
                    if isinstance(step, dict) and isinstance((name := step.get("codeName")), str)
                }
                if isinstance(requested_transform_steps, list)
                else set()
            )
            renaming = bool(isinstance(new_table_name, str) and new_table_name.strip())

            if not any(
                (
                    scalar_keys,
                    add_field_names,
                    delete_field_names,
                    expected_transform_code_names,
                    renaming,
                )
            ):
                return {
                    "status": "skipped",
                    "reason": "no_verifiable_fields",
                    "message": "No verifiable mutable fields were present in the payload",
                }

            deadline = time.monotonic() + verify_timeout_seconds
            attempt_count = 0
            latest_mismatches: list[dict[str, object]] = []
            latest_record: dict[str, object] | None = None
            latest_reason = "table_not_found"

            while True:
                attempt_count += 1
                described = connector_adapter.describe_integration_tables(
                    {"tables": [describe_request]}
                )
                records = _extract_table_records(described)
                matching_record: dict[str, object] | None = None
                for record in records:
                    candidate = record.get("tableName")
                    if isinstance(candidate, str) and candidate.strip() == expected_table_name:
                        matching_record = record
                        break
                if matching_record is None and records:
                    matching_record = records[0]

                if matching_record is not None:
                    latest_record = matching_record
                    mismatches: list[dict[str, object]] = []

                    if renaming:
                        observed_name = matching_record.get("tableName")
                        if observed_name != expected_table_name:
                            mismatches.append(
                                {
                                    "field": "newTableName",
                                    "expected": expected_table_name,
                                    "observed": observed_name,
                                }
                            )

                    for key in scalar_keys:
                        expected = resolved.get(key)
                        observed = matching_record.get(key, _missing_value)
                        if observed is _missing_value:
                            continue
                        if _coerce_comparable(observed) != _coerce_comparable(expected):
                            mismatches.append(
                                {"field": key, "expected": expected, "observed": observed}
                            )

                    if add_field_names:
                        observed_fields = _extract_field_names(matching_record)
                        missing = add_field_names - observed_fields
                        if missing:
                            mismatches.append(
                                {
                                    "field": "addFields",
                                    "expected": sorted(add_field_names),
                                    "observed": sorted(observed_fields),
                                    "missing": sorted(missing),
                                }
                            )

                    if delete_field_names:
                        observed_fields = _extract_field_names(matching_record)
                        still_present = delete_field_names & observed_fields
                        if still_present:
                            mismatches.append(
                                {
                                    "field": "deleteFields",
                                    "expected_removed": sorted(delete_field_names),
                                    "observed": sorted(observed_fields),
                                    "still_present": sorted(still_present),
                                }
                            )

                    if expected_transform_code_names:
                        observed_steps = _extract_transform_step_code_names(matching_record)
                        missing_steps = expected_transform_code_names - observed_steps
                        if missing_steps:
                            mismatches.append(
                                {
                                    "field": "transformSteps",
                                    "expected_code_names": sorted(expected_transform_code_names),
                                    "observed_code_names": sorted(observed_steps),
                                    "missing_code_names": sorted(missing_steps),
                                }
                            )

                    if not mismatches:
                        return {
                            "status": "verified",
                            "tableName": expected_table_name,
                            "attempts": attempt_count,
                            "poll_timeout_seconds": verify_timeout_seconds,
                        }

                    latest_reason = "mutation_not_applied"
                    latest_mismatches = mismatches

                if time.monotonic() >= deadline:
                    break
                time.sleep(verify_poll_interval_seconds)

            raise UpstreamAPIError(
                "alter_integration_table write acknowledged but post-commit verification timed out",
                details={
                    "errorCode": 0,
                    "verification_error_code": "post_commit_verification_timeout",
                    "request_action": "alterIntegrationTable",
                    "tableName": expected_table_name,
                    "reason_code": latest_reason,
                    "mismatches": latest_mismatches,
                    "verification_source": latest_record,
                    "verification_attempts": attempt_count,
                    "verification_timeout_seconds": verify_timeout_seconds,
                },
                retryable=False,
                hint=(
                    "The write call returned success but read-after-write verification did not "
                    "converge before timeout. FairCom's alterIntegrationTable is known to "
                    "silently no-op for some fields/transformSteps. Confirm the latest state "
                    "with describe_integration_tables before retrying, or recreate the table "
                    "instead (delete_integration_tables + create_integration_table with all "
                    "fields/transformSteps declared upfront)."
                ),
            )

        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "alter_integration_table",
                "action": "alterIntegrationTable",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "alter_integration_table",
                "connector",
                lambda: _table_write_preview(
                    tool_name="alter_integration_table",
                    action="alterIntegrationTable",
                    payload=payload,
                ),
            )
        try:
            resolved_payload = _require_table_payload(
                tool_name="alter_integration_table",
                payload=payload,
            )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="alter_integration_table",
                    message="alter_integration_table requires confirm_write=True",
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "alter_integration_table",
                        "arguments": {"payload": {"tableName": "modbus_factory_floor_raw"}},
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="alter_integration_table",
                action="alterIntegrationTable",
                payload=payload,
                exc=exc,
            )
            raise

        verification: dict[str, object] | None = None

        def _post_commit_verify(_result: object) -> None:
            nonlocal verification
            verification = _verify_alter_integration_table_mutation(resolved_payload)

        result = _execute_connector_write(
            tool_name="alter_integration_table",
            action="alterIntegrationTable",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "alter_integration_table",
                "connector",
                lambda: connector_adapter.alter_integration_table(resolved_payload),
            ),
            post_commit_verifier=_post_commit_verify,
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": bool(
                        verification and verification.get("status") == "verified"
                    ),
                    "mutation_verification": verification,
                }
            )
            return enriched
        return result

    @server.tool(name="delete_integration_tables")
    def delete_integration_tables(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "delete_integration_tables",
                "action": "deleteIntegrationTables",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "delete_integration_tables",
                "connector",
                lambda: _table_write_preview(
                    tool_name="delete_integration_tables",
                    action="deleteIntegrationTables",
                    payload=payload,
                    require_table_names=True,
                ),
            )
        try:
            resolved_payload = _require_table_payload(
                tool_name="delete_integration_tables",
                payload=payload,
                require_table_names=True,
            )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="delete_integration_tables",
                    message="delete_integration_tables requires confirm_write=True",
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "delete_integration_tables",
                        "arguments": {"payload": {"tableNames": ["modbus_factory_floor_raw"]}},
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="delete_integration_tables",
                action="deleteIntegrationTables",
                payload=payload,
                exc=exc,
            )
            raise
        result = _execute_connector_write(
            tool_name="delete_integration_tables",
            action="deleteIntegrationTables",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "delete_integration_tables",
                "connector",
                lambda: connector_adapter.delete_integration_tables(resolved_payload),
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

    @server.tool(name="test_integration_table_transform_steps")
    def test_integration_table_transform_steps(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "test_integration_table_transform_steps",
                "action": "testIntegrationTableTransformSteps",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "test_integration_table_transform_steps",
                "connector",
                lambda: _table_write_preview(
                    tool_name="test_integration_table_transform_steps",
                    action="testIntegrationTableTransformSteps",
                    payload=payload,
                ),
            )
        try:
            resolved_payload = _require_table_payload(
                tool_name="test_integration_table_transform_steps",
                payload=payload,
            )
            test_transform_scope = resolved_payload.get("testTransformScope")
            if test_transform_scope not in _VALID_TEST_TRANSFORM_SCOPES:
                raise _validation_failure(
                    tool_name="test_integration_table_transform_steps",
                    message=(
                        "payload.testTransformScope is required and must be one of: "
                        + ", ".join(sorted(_VALID_TEST_TRANSFORM_SCOPES))
                    ),
                    expected_args={
                        "payload.testTransformScope": (
                            "one of "
                            + ", ".join(sorted(_VALID_TEST_TRANSFORM_SCOPES))
                            + " (required)"
                        ),
                    },
                    received_args={"testTransformScope": test_transform_scope},
                    suggested_fix=(
                        "Set payload.testTransformScope to one of: "
                        + ", ".join(sorted(_VALID_TEST_TRANSFORM_SCOPES))
                        + ". FairCom's upstream error does not list these values."
                    ),
                    example_payload={
                        "name": "test_integration_table_transform_steps",
                        "arguments": {
                            "payload": {
                                "tableName": "modbus_factory_floor_raw",
                                "testTransformScope": "firstRecord",
                            }
                        },
                    },
                    reason_code="invalid_arguments",
                )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="test_integration_table_transform_steps",
                    message=("test_integration_table_transform_steps requires confirm_write=True"),
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "test_integration_table_transform_steps",
                        "arguments": {"payload": {"tableName": "modbus_factory_floor_raw"}},
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="test_integration_table_transform_steps",
                action="testIntegrationTableTransformSteps",
                payload=payload,
                exc=exc,
            )
            raise
        result = _execute_connector_write(
            tool_name="test_integration_table_transform_steps",
            action="testIntegrationTableTransformSteps",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "test_integration_table_transform_steps",
                "connector",
                lambda: connector_adapter.test_integration_table_transform_steps(resolved_payload),
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

    @server.tool(name="list_topics")
    def list_topics(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "list_topics",
            "metadata",
            lambda: connector_adapter.list_topics(payload),
        )

    @server.tool(name="describe_topics")
    def describe_topics(payload: dict[str, object] | None = None) -> object:
        return _run_tool(
            "describe_topics",
            "metadata",
            lambda: connector_adapter.describe_topics(payload),
        )

    def _require_topic_payload(
        *,
        tool_name: str,
        payload: dict[str, object] | None,
        require_binding_fields: bool,
    ) -> dict[str, object]:
        if payload is None or not isinstance(payload, dict) or not payload:
            raise _validation_failure(
                tool_name=tool_name,
                message="payload is required",
                expected_args={
                    "payload": "object (required)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload},
                suggested_fix="Provide a non-empty MQ topic payload object.",
                example_payload={
                    "name": tool_name,
                    "arguments": {
                        "payload": {
                            "topic": "factory/line-1/mixing_tank/temperature",
                            "databaseName": "faircom",
                            "tableName": "modbus_mixing_tank_temp",
                        }
                    },
                },
            )

        topic = payload.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="payload.topic is required and must be non-empty",
                expected_args={"payload.topic": "string (required)"},
                received_args={"payload": payload},
                suggested_fix="Provide payload.topic naming the MQTT topic to configure.",
                example_payload={
                    "name": tool_name,
                    "arguments": {
                        "payload": {
                            "topic": "factory/line-1/mixing_tank/temperature",
                            "databaseName": "faircom",
                            "tableName": "modbus_mixing_tank_temp",
                        }
                    },
                },
            )

        if require_binding_fields:
            for key in ("tableName", "databaseName"):
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise _validation_failure(
                        tool_name=tool_name,
                        message=f"payload.{key} is required and must be non-empty",
                        expected_args={f"payload.{key}": "string (required)"},
                        received_args={"payload": payload},
                        suggested_fix=(
                            f"Provide payload.{key} to bind the topic to an integration table."
                        ),
                        example_payload={
                            "name": tool_name,
                            "arguments": {
                                "payload": {
                                    "topic": "factory/line-1/mixing_tank/temperature",
                                    "databaseName": "faircom",
                                    "tableName": "modbus_mixing_tank_temp",
                                }
                            },
                        },
                    )

        return dict(payload)

    @server.tool(name="configure_topic")
    def configure_topic(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        # configureTopic is an upsert (create-or-update a topic binding), unlike
        # createInput/createOutput, so it is not gated the same way create_* tools are.
        _missing_value = object()

        def _extract_topic_records(value: object) -> list[dict[str, object]]:
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if not isinstance(value, dict):
                return []
            direct_records: list[dict[str, object]] = []
            for key in ("topics", "results", "data", "items"):
                nested = value.get(key)
                if isinstance(nested, list):
                    direct_records.extend(entry for entry in nested if isinstance(entry, dict))
            if direct_records:
                return direct_records
            return [value]

        def _coerce_comparable(value: object) -> object:
            if isinstance(value, str):
                stripped = value.strip()
                lowered = stripped.lower()
                if lowered in {"true", "yes", "on", "enabled"}:
                    return True
                if lowered in {"false", "no", "off", "disabled"}:
                    return False
                if stripped.isdigit():
                    try:
                        return int(stripped)
                    except ValueError:
                        return stripped
                return stripped
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value

        def _verify_configure_topic_mutation(resolved: dict[str, object]) -> dict[str, object]:
            verify_timeout_seconds = 6.0
            verify_poll_interval_seconds = 0.3
            topic = resolved.get("topic")
            if not isinstance(topic, str) or not topic.strip():
                return {
                    "status": "skipped",
                    "reason": "missing_topic_identity",
                    "message": "payload.topic was not available for verification",
                }

            verify_keys = [
                key
                for key in (
                    "tableName",
                    "databaseName",
                    "transformName",
                    "downgradeQoS",
                    "maxDeliveryRatePerSecond",
                )
                if key in resolved
            ]
            if not verify_keys:
                return {
                    "status": "skipped",
                    "reason": "no_verifiable_fields",
                    "message": "No verifiable fields were present in the payload",
                }

            deadline = time.monotonic() + verify_timeout_seconds
            attempt_count = 0
            latest_mismatches: list[dict[str, object]] = []
            latest_record: dict[str, object] | None = None
            latest_reason = "topic_not_found"

            while True:
                attempt_count += 1
                described = connector_adapter.describe_topics({"topics": [topic]})
                records = _extract_topic_records(described)
                matching_record: dict[str, object] | None = None
                for record in records:
                    candidate = record.get("topic")
                    if isinstance(candidate, str) and candidate.strip() == topic:
                        matching_record = record
                        break
                if matching_record is None and records:
                    matching_record = records[0]

                if matching_record is not None:
                    latest_record = matching_record
                    mismatches: list[dict[str, object]] = []
                    comparable_fields: list[str] = []
                    for key in verify_keys:
                        expected = resolved.get(key)
                        observed = matching_record.get(key, _missing_value)
                        if observed is _missing_value:
                            continue
                        comparable_fields.append(key)
                        if _coerce_comparable(observed) != _coerce_comparable(expected):
                            mismatches.append(
                                {"field": key, "expected": expected, "observed": observed}
                            )

                    if comparable_fields and not mismatches:
                        return {
                            "status": "verified",
                            "topic": topic,
                            "verified_fields": comparable_fields,
                            "attempts": attempt_count,
                            "poll_timeout_seconds": verify_timeout_seconds,
                        }

                    if comparable_fields:
                        latest_reason = "mutation_not_applied"
                        latest_mismatches = mismatches
                    else:
                        latest_reason = "verification_fields_not_observable"

                if time.monotonic() >= deadline:
                    break
                time.sleep(verify_poll_interval_seconds)

            raise UpstreamAPIError(
                "configure_topic write acknowledged but post-commit verification timed out",
                details={
                    "errorCode": 0,
                    "verification_error_code": "post_commit_verification_timeout",
                    "request_action": "configureTopic",
                    "topic": topic,
                    "reason_code": latest_reason,
                    "mismatches": latest_mismatches,
                    "verification_source": latest_record,
                    "verification_attempts": attempt_count,
                    "verification_timeout_seconds": verify_timeout_seconds,
                },
                retryable=False,
                hint=(
                    "The write call returned success but read-after-write verification did not "
                    "converge before timeout. Confirm the latest state with describe_topics "
                    "before retrying."
                ),
            )

        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "configure_topic",
                "action": "configureTopic",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "configure_topic",
                "connector",
                lambda: {
                    "mode": "dry_run",
                    "status": "success",
                    "tool_name": "configure_topic",
                    "action": "configureTopic",
                    "payload": payload,
                    "execution_status": "not_executed",
                    "preview": "configureTopic is an upsert; would create or update this topic",
                    "warnings": [
                        "Dry run is a local preview only and does not call FairCom backend APIs."
                    ],
                },
            )
        try:
            resolved_payload = _require_topic_payload(
                tool_name="configure_topic",
                payload=payload,
                require_binding_fields=True,
            )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="configure_topic",
                    message="configure_topic requires confirm_write=True",
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "configure_topic",
                        "arguments": {
                            "payload": {
                                "topic": "factory/line-1/mixing_tank/temperature",
                                "databaseName": "faircom",
                                "tableName": "modbus_mixing_tank_temp",
                            },
                            "confirm_write": True,
                        },
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="configure_topic",
                action="configureTopic",
                payload=payload,
                exc=exc,
            )
            raise

        verification: dict[str, object] | None = None

        def _post_commit_verify(_result: object) -> None:
            nonlocal verification
            verification = _verify_configure_topic_mutation(resolved_payload)

        result = _execute_connector_write(
            tool_name="configure_topic",
            action="configureTopic",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "configure_topic",
                "connector",
                lambda: connector_adapter.configure_topic(resolved_payload),
            ),
            post_commit_verifier=_post_commit_verify,
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": bool(
                        verification and verification.get("status") == "verified"
                    ),
                    "mutation_verification": verification,
                }
            )
            return enriched
        return result

    @server.tool(name="delete_topic")
    def delete_topic(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        audit_log.record(
            event_type="connector_write_attempt",
            details={
                "tool": "delete_topic",
                "action": "deleteTopic",
                "target": _connector_target_name(payload),
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return _run_tool(
                "delete_topic",
                "connector",
                lambda: {
                    "mode": "dry_run",
                    "status": "success",
                    "tool_name": "delete_topic",
                    "action": "deleteTopic",
                    "payload": payload,
                    "execution_status": "not_executed",
                    "preview": "Topic would be deleted",
                    "warnings": [
                        "Dry run is a local preview only and does not call FairCom backend APIs."
                    ],
                },
            )
        try:
            resolved_payload = _require_topic_payload(
                tool_name="delete_topic",
                payload=payload,
                require_binding_fields=False,
            )
            if not confirm_write:
                raise _validation_failure(
                    tool_name="delete_topic",
                    message="delete_topic requires confirm_write=True",
                    expected_args={
                        "payload": "object (required)",
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "payload": resolved_payload,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "delete_topic",
                        "arguments": {
                            "payload": {"topic": "factory/line-1/mixing_tank/temperature"},
                            "confirm_write": True,
                        },
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            _record_connector_validation_rejection(
                tool_name="delete_topic",
                action="deleteTopic",
                payload=payload,
                exc=exc,
            )
            raise
        result = _execute_connector_write(
            tool_name="delete_topic",
            action="deleteTopic",
            target=_connector_target_name(resolved_payload),
            writer=lambda: _run_tool(
                "delete_topic",
                "connector",
                lambda: connector_adapter.delete_topic(resolved_payload),
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
                    "manage_service": ["payload", "confirm_write", "dry_run"],
                    "list_outputs": ["payload"],
                    "describe_outputs": ["payload"],
                    "create_output": ["payload", "confirm_write", "dry_run"],
                    "alter_output": ["payload", "confirm_write", "dry_run"],
                    "delete_output": ["payload", "confirm_write", "dry_run"],
                    "list_integration_tables": ["payload"],
                    "describe_integration_tables": ["payload"],
                    "create_integration_table": ["payload", "confirm_write", "dry_run"],
                    "alter_integration_table": ["payload", "confirm_write", "dry_run"],
                    "delete_integration_tables": ["payload", "confirm_write", "dry_run"],
                    "test_integration_table_transform_steps": [
                        "payload",
                        "confirm_write",
                        "dry_run",
                    ],
                    "list_topics": ["payload"],
                    "describe_topics": ["payload"],
                    "configure_topic": ["payload", "confirm_write", "dry_run"],
                    "delete_topic": ["payload", "confirm_write", "dry_run"],
                    "list_code_packages": [
                        "name_like",
                        "database_name",
                        "owner_name",
                        "code_type_filter",
                        "status_filter",
                        "max_records",
                    ],
                    "describe_code_packages": [
                        "code_names",
                        "database_name",
                        "owner_name",
                        "code_format",
                    ],
                    "register_code_package": [
                        "code_name",
                        "code",
                        "code_type",
                        "code_status",
                        "database_name",
                        "owner_name",
                        "comment",
                        "description",
                        "metadata",
                        "input_fields",
                        "output_field_definitions",
                        "confirm_write",
                        "dry_run",
                    ],
                    "clone_code_package": [
                        "code_name",
                        "new_code_name",
                        "database_name",
                        "owner_name",
                        "confirm_write",
                        "dry_run",
                    ],
                    "revert_code_package": [
                        "code_name",
                        "version",
                        "database_name",
                        "owner_name",
                        "confirm_write",
                        "dry_run",
                    ],
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
                    "manage_service": {},
                    "list_outputs": {},
                    "describe_outputs": {},
                    "create_output": {},
                    "alter_output": {},
                    "delete_output": {},
                    "list_integration_tables": {},
                    "describe_integration_tables": {},
                    "create_integration_table": {},
                    "alter_integration_table": {},
                    "delete_integration_tables": {},
                    "test_integration_table_transform_steps": {},
                    "list_code_packages": {},
                    "describe_code_packages": {},
                    "register_code_package": {},
                    "clone_code_package": {},
                    "revert_code_package": {},
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
                                "tableName": "modbus_energy_raw",
                                "modbusProtocol": "TCP",
                                "modbusServer": "127.0.0.1",
                                "modbusServerPort": 502,
                                "modbusDataAddressType": "zeroBased",
                                "propertyMapList": [
                                    {
                                        "modbusDataAccess": "holdingregister",
                                        "modbusDataType": "int16SignedAB",
                                        "modbusDataLen": 1,
                                    }
                                ],
                            },
                            "confirm_write": True,
                        },
                    },
                    "manage_service": {
                        "name": "manage_service",
                        "arguments": {
                            "payload": {"serviceName": "modbus", "command": "pause"},
                            "confirm_write": True,
                        },
                    },
                    "create_output": {
                        "name": "create_output",
                        "arguments": {
                            "payload": {
                                "outputName": "writeTemperatureToModbus",
                                "serviceName": "modbus",
                                "databaseName": "faircom",
                                "ownerName": "admin",
                                "tableName": "modbusTableTCP",
                                "sourceFields": ["source_payload"],
                                "settings": {
                                    "modbusProtocol": "TCP",
                                    "modbusServer": "127.0.0.1",
                                    "modbusServerPort": 502,
                                    "propertyMapList": [
                                        {
                                            "propertyPath": "source_payload.temperature",
                                            "modbusDataAddress": 1399,
                                            "modbusDataAccess": "holdingregister",
                                            "modbusUnitId": 5,
                                            "modbusDataLen": 1,
                                        }
                                    ],
                                },
                            },
                            "confirm_write": True,
                        },
                    },
                    "create_integration_table": {
                        "name": "create_integration_table",
                        "arguments": {
                            "payload": {
                                "tableName": "modbus_factory_floor_raw",
                                "transformSteps": [
                                    {
                                        "transformStepMethod": "javascript",
                                        "transformStepService": "v8TransformService",
                                        "codeName": "decode_mixing_tank",
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
                    "register_code_package": {
                        "name": "register_code_package",
                        "arguments": {
                            "code_name": "decode_mixing_tank",
                            "code": "record.value = record.source_payload.value;",
                            "code_type": "integrationTableTransform",
                            "input_fields": ["source_payload.value"],
                            "output_field_definitions": [{"name": "value", "type": "double"}],
                            "confirm_write": True,
                        },
                    },
                    "validate_connector_payloads": {
                        "name": "validate_connector_payloads",
                        "arguments": {
                            "action": "createInput",
                            "payloads": [
                                {
                                    "connectorName": "modbus_energy_input",
                                    "serviceName": "modbus",
                                    "tableName": "modbus_energy_raw",
                                    "modbusProtocol": "TCP",
                                    "modbusServer": "127.0.0.1",
                                    "modbusServerPort": 502,
                                    "propertyMapList": [
                                        {
                                            "modbusDataAccess": "holdingregister",
                                            "modbusDataType": "int16SignedAB",
                                            "modbusDataLen": 1,
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
                    "manage_service": "complete",
                    "create_output": "complete",
                    "create_integration_table": "requires_existing_code_package",
                    "register_code_package": "complete",
                    "describe_connector_schema": "complete",
                    "validate_connector_payloads": "complete",
                },
                "connector_payload_profiles": connector_contract_profiles,
                "connector_schema_profiles": sorted(_CONNECTOR_SCHEMA_REGISTRY.keys()),
                "connector_output_schema_profiles": sorted(
                    _CONNECTOR_OUTPUT_SCHEMA_REGISTRY.keys()
                ),
            },
        )

    @server.tool(name="describe_connector_schema")
    def describe_connector_schema(service_name: str, direction: str = "input") -> object:
        normalized = service_name.strip().lower()
        normalized_direction = direction.strip().lower() if direction else "input"
        registry = (
            _CONNECTOR_OUTPUT_SCHEMA_REGISTRY
            if normalized_direction == "output"
            else _CONNECTOR_SCHEMA_REGISTRY
        )
        schema = registry.get(normalized)
        if schema is None:
            supported = sorted(registry.keys())
            raise _validation_failure(
                tool_name="describe_connector_schema",
                message="Unsupported connector service_name",
                expected_args={
                    "service_name": f"one of {supported} for direction={normalized_direction!r}",
                    "direction": "input or output",
                },
                received_args={"service_name": service_name, "direction": direction},
                suggested_fix="Call with a supported connector profile name and direction.",
                example_payload={
                    "name": "describe_connector_schema",
                    "arguments": {"service_name": "modbus", "direction": "input"},
                },
            )

        return _run_tool(
            "describe_connector_schema",
            "admin",
            lambda: {
                "service_name": normalized,
                "direction": normalized_direction,
                "schema_version": "2026-08-06",
                "schema": schema,
                "known_good_example": schema.get("example"),
            },
        )

    @server.tool(name="validate_connector_payloads")
    def validate_connector_payloads(
        action: str = "createInput",
        payload: dict[str, object] | None = None,
        payloads: list[dict[str, object]] | None = None,
    ) -> object:
        action_aliases = {
            "manage_service": "manageService",
            "manage-service": "manageService",
            "manageservice": "manageService",
        }
        normalized_action = action_aliases.get(action, action)

        allowed_actions = {
            "createInput",
            "alterInput",
            "deleteInput",
            "createOutput",
            "alterOutput",
            "deleteOutput",
            "createIntegrationTable",
            "alterIntegrationTable",
            "deleteIntegrationTables",
            "manageService",
        }
        if normalized_action not in allowed_actions:
            raise _validation_failure(
                tool_name="validate_connector_payloads",
                message="Unsupported action for preflight",
                expected_args={
                    "action": (
                        "one of createInput/alterInput/deleteInput/"
                        "createOutput/alterOutput/deleteOutput/"
                        "createIntegrationTable/alterIntegrationTable/deleteIntegrationTables/"
                        "manageService"
                    ),
                    "payload": "object (optional, single preflight)",
                    "payloads": "array<object> (optional, batch preflight)",
                },
                received_args={"action": action, "payload": payload, "payloads": payloads},
                suggested_fix="Use one of the supported connector actions for preflight.",
                example_payload={
                    "name": "validate_connector_payloads",
                    "arguments": {
                        "action": "createIntegrationTable",
                        "payload": {
                            "tableName": "inline_decode_asset01",
                            "transformSteps": [
                                {
                                    "transformStepMethod": "javascript",
                                    "transformStepService": "v8TransformService",
                                    "codeName": "decode_asset01",
                                }
                            ],
                        },
                    },
                },
            )

        if payload is not None and payloads is not None:
            raise _validation_failure(
                tool_name="validate_connector_payloads",
                message="Provide either payload or payloads, but not both",
                expected_args={
                    "action": (
                        "one of createInput/alterInput/deleteInput/"
                        "createOutput/alterOutput/deleteOutput/"
                        "createIntegrationTable/alterIntegrationTable/deleteIntegrationTables/"
                        "manageService"
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
                            "tableName": "modbus_energy_raw",
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
                        "createOutput/alterOutput/deleteOutput/"
                        "createIntegrationTable/alterIntegrationTable/deleteIntegrationTables"
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
                                "tableName": "modbus_energy_raw",
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

        items: list[object] = cast(list[object], payloads) if payloads is not None else [payload]

        def _sanitize_jsonish(value: object, *, depth: int = 0) -> object:
            if depth > 8:
                return "..."
            if value is None or isinstance(value, (str, bool, int, float)):
                return value
            if isinstance(value, list):
                return [_sanitize_jsonish(item, depth=depth + 1) for item in value]
            if isinstance(value, dict):
                sanitized: dict[str, object] = {}
                for key, nested in value.items():
                    if isinstance(key, str):
                        sanitized[key] = _sanitize_jsonish(nested, depth=depth + 1)
                return sanitized
            return str(value)

        def _coerce_validation_errors(
            raw_errors: object, *, fallback: str
        ) -> list[dict[str, object]]:
            if not isinstance(raw_errors, list):
                return [
                    {
                        "path": "payload",
                        "json_pointer": "/payload",
                        "reason": "invalid_arguments",
                        "message": fallback,
                    }
                ]

            allowed_keys = {
                "path",
                "json_pointer",
                "reason",
                "message",
                "expected",
                "received",
                "allowed_values",
                "nearest_match",
                "corrected_snippet",
                "registered_services",
            }
            sanitized_errors: list[dict[str, object]] = []
            for item in raw_errors:
                if not isinstance(item, dict):
                    continue
                normalized: dict[str, object] = {}
                for key, value in item.items():
                    if isinstance(key, str) and key in allowed_keys:
                        normalized[key] = _sanitize_jsonish(value)
                if "path" not in normalized:
                    normalized["path"] = "payload"
                if "json_pointer" not in normalized:
                    normalized["json_pointer"] = "/payload"
                if "reason" not in normalized:
                    normalized["reason"] = "invalid_arguments"
                if "message" not in normalized:
                    normalized["message"] = fallback
                sanitized_errors.append(normalized)

            if sanitized_errors:
                return sanitized_errors
            return [
                {
                    "path": "payload",
                    "json_pointer": "/payload",
                    "reason": "invalid_arguments",
                    "message": fallback,
                }
            ]

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
                    if normalized_action == "manageService":
                        normalized_payload = _validate_manage_service_payload(
                            tool_name="validate_connector_payloads",
                            payload=item,
                        )
                    elif normalized_action in {
                        "createIntegrationTable",
                        "alterIntegrationTable",
                        "deleteIntegrationTables",
                    }:
                        normalized_payload = _require_table_payload(
                            tool_name="validate_connector_payloads",
                            payload=item,
                            require_table_names=normalized_action == "deleteIntegrationTables",
                        )
                    else:
                        normalized_payload = _require_connector_payload(
                            tool_name="validate_connector_payloads",
                            payload=item,
                            action=normalized_action,
                        )
                except ValidationFailure as exc:
                    invalid_count += 1
                    received_args = exc.details.get("received_args", {})
                    validation_errors = received_args.get("validation_errors")
                    sanitized_errors = _coerce_validation_errors(
                        validation_errors,
                        fallback=str(exc.message),
                    )

                    results.append(
                        {
                            "index": index,
                            "status": "invalid",
                            "action": action,
                            "errors": sanitized_errors,
                        }
                    )
                    continue

                valid_count += 1
                validation: dict[str, object]
                forwarded_payload: dict[str, object] | None
                if normalized_action == "manageService":
                    validation = {
                        "service_name": normalized_payload.get("serviceName"),
                        "warnings": [],
                    }
                    forwarded_payload = normalized_payload
                elif normalized_action in {
                    "createIntegrationTable",
                    "alterIntegrationTable",
                    "deleteIntegrationTables",
                }:
                    validation = {"service_name": None, "warnings": []}
                    forwarded_payload = normalized_payload
                else:
                    validation = _validate_connector_schema(
                        tool_name="validate_connector_payloads",
                        action=normalized_action,
                        payload=normalized_payload,
                    )
                    forwarded_payload = transform_connector_request(
                        normalized_action,
                        normalized_payload,
                    )
                results.append(
                    {
                        "index": index,
                        "status": "valid",
                        "action": normalized_action,
                        "schema_service": validation["service_name"],
                        "warnings": validation.get("warnings", []),
                        "normalized_payload": normalized_payload,
                        "forwarded_payload": forwarded_payload,
                        "errors": [],
                    }
                )

            return {
                "mode": "preflight",
                "action": normalized_action,
                "summary": {
                    "total": len(items),
                    "valid": valid_count,
                    "invalid": invalid_count,
                    "all_valid": invalid_count == 0,
                },
                "results": results,
            }

        return _run_tool("validate_connector_payloads", "admin", _run_preflight)

    @server.tool(name="list_code_packages")
    def list_code_packages(
        name_like: str | None = None,
        database_name: str = "faircom",
        owner_name: str = "admin",
        code_type_filter: list[str] | None = None,
        status_filter: list[str] | None = None,
        max_records: int = 200,
    ) -> object:
        payload: dict[str, object] = {
            "databaseName": database_name,
            "ownerName": owner_name,
            "maxRecords": max_records,
        }
        if isinstance(name_like, str) and name_like.strip():
            payload["partialName"] = name_like.strip()
        if code_type_filter:
            invalid_types = [t for t in code_type_filter if t not in _CODE_PACKAGE_TYPE_ENUM]
            if invalid_types:
                raise _validation_failure(
                    tool_name="list_code_packages",
                    message="Unsupported code_type_filter value(s)",
                    expected_args={"code_type_filter": f"subset of {_CODE_PACKAGE_TYPE_ENUM}"},
                    received_args={"code_type_filter": code_type_filter},
                    suggested_fix="Use only supported FairCom code package types.",
                    example_payload={
                        "name": "list_code_packages",
                        "arguments": {"code_type_filter": ["integrationTableTransform"]},
                    },
                )
            payload["codeTypeFilter"] = list(code_type_filter)
        if status_filter:
            payload["statusFilter"] = list(status_filter)
        return _run_tool(
            "list_code_packages",
            "metadata",
            lambda: connector_adapter.list_code_packages(payload),
        )

    @server.tool(name="describe_code_packages")
    def describe_code_packages(
        code_names: list[str],
        database_name: str = "faircom",
        owner_name: str = "admin",
        code_format: str = "utf8",
    ) -> object:
        normalized_names = [
            name.strip() for name in code_names if isinstance(name, str) and name.strip()
        ]
        if not normalized_names:
            raise _validation_failure(
                tool_name="describe_code_packages",
                message="code_names is required",
                expected_args={"code_names": "array of string (required)"},
                received_args={"code_names": code_names},
                suggested_fix="Provide one or more non-empty code_names.",
                example_payload={
                    "name": "describe_code_packages",
                    "arguments": {"code_names": ["decode_mixing_tank"]},
                },
            )
        payload = {
            "databaseName": database_name,
            "ownerName": owner_name,
            "codeNames": normalized_names,
            "codeFormat": code_format,
        }
        return _run_tool(
            "describe_code_packages",
            "metadata",
            lambda: connector_adapter.describe_code_packages(payload),
        )

    def _code_package_exists(
        *,
        code_name: str,
        database_name: str,
        owner_name: str,
    ) -> bool:
        try:
            result = connector_adapter.describe_code_packages(
                {
                    "databaseName": database_name,
                    "ownerName": owner_name,
                    "codeNames": [code_name],
                }
            )
        except UpstreamAPIError:
            # FairCom's describeCodePackages raises an application error (rather than
            # returning an empty list) when the code package name doesn't exist yet.
            return False
        if not isinstance(result, dict):
            return False
        nested = result.get("result")
        data = nested.get("data") if isinstance(nested, dict) else None
        return isinstance(data, list) and len(data) > 0

    def _validate_javascript_code(*, tool_name: str, code_name: str, code: str) -> None:
        try:
            import esprima  # type: ignore[import-not-found, import-untyped]

            esprima.parseScript(code)
        except Exception as exc:
            parser_message = str(exc).strip() or "Unknown JavaScript syntax error"
            details: dict[str, object] = {"code_name": code_name, "parser_message": parser_message}
            line_number = getattr(exc, "lineNumber", None)
            if isinstance(line_number, int):
                details["line"] = line_number
            line_context = f" at line {line_number}" if isinstance(line_number, int) else ""
            raise _validation_failure(
                tool_name=tool_name,
                message=f"JavaScript syntax validation failed{line_context}: {parser_message}",
                expected_args={"code": "valid JavaScript source"},
                received_args=details,
                suggested_fix=f"Fix JavaScript syntax errors{line_context} and retry.",
                example_payload={
                    "name": tool_name,
                    "arguments": {"code": "function transform(row){ return row; }"},
                },
                reason_code="invalid_arguments",
            ) from exc

    @server.tool(name="register_code_package")
    def register_code_package(
        code_name: str,
        code: str,
        code_type: str = "integrationTableTransform",
        *,
        code_status: str = "active",
        database_name: str = "faircom",
        owner_name: str = "admin",
        comment: str = "",
        description: str = "",
        metadata: dict[str, object] | None = None,
        input_fields: list[str] | None = None,
        output_field_definitions: list[dict[str, object]] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        normalized_name = code_name.strip()
        normalized_code = code.strip()
        normalized_type = code_type.strip()

        resolved_metadata: dict[str, object] = dict(metadata) if metadata else {}
        if input_fields is not None:
            resolved_metadata["inputFields"] = input_fields
        if output_field_definitions is not None:
            resolved_metadata["outputFieldDefinitions"] = output_field_definitions

        metadata_warnings: list[str] = []
        if normalized_type == "integrationTableTransform":
            if not resolved_metadata.get("inputFields"):
                metadata_warnings.append(
                    "metadata.inputFields is not set. The FairCom Edge Explorer wizard cannot "
                    "find this code package as a usable Integration Table Transform without it. "
                    "Pass input_fields (list of field names the transform reads)."
                )
            if not resolved_metadata.get("outputFieldDefinitions"):
                metadata_warnings.append(
                    "metadata.outputFieldDefinitions is not set. The FairCom Edge Explorer "
                    "wizard cannot find this code package as a usable Integration Table "
                    "Transform without it. Pass output_field_definitions (list of "
                    "{name, type} objects the transform writes)."
                )

        audit_log.record(
            event_type="code_package_write_attempt",
            details={
                "tool": "register_code_package",
                "operation": "upsert",
                "code_name": normalized_name,
                "database_name": database_name,
                "owner_name": owner_name,
                "code_type": normalized_type,
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        try:
            if not normalized_name:
                raise _validation_failure(
                    tool_name="register_code_package",
                    message="code_name is required",
                    expected_args={"code_name": "string (required)"},
                    received_args={"code_name": code_name},
                    suggested_fix="Provide a non-empty code_name.",
                    example_payload={
                        "name": "register_code_package",
                        "arguments": {
                            "code_name": "decode_mixing_tank",
                            "code": "record.value = record.source_payload.value;",
                            "confirm_write": True,
                        },
                    },
                )
            if not normalized_code:
                raise _validation_failure(
                    tool_name="register_code_package",
                    message="code is required",
                    expected_args={"code": "string (required)"},
                    received_args={"code": code},
                    suggested_fix="Provide non-empty JavaScript code.",
                    example_payload={
                        "name": "register_code_package",
                        "arguments": {
                            "code_name": "decode_mixing_tank",
                            "code": "record.value = record.source_payload.value;",
                            "confirm_write": True,
                        },
                    },
                )
            if normalized_type not in _CODE_PACKAGE_CREATE_TYPE_ENUM:
                raise _validation_failure(
                    tool_name="register_code_package",
                    message="Unsupported code_type",
                    expected_args={"code_type": f"one of {_CODE_PACKAGE_CREATE_TYPE_ENUM}"},
                    received_args={"code_type": code_type},
                    suggested_fix=(
                        "createCodePackage/alterCodePackage only accept "
                        f"{_CODE_PACKAGE_CREATE_TYPE_ENUM}."
                    ),
                    example_payload={
                        "name": "register_code_package",
                        "arguments": {
                            "code_name": "decode_mixing_tank",
                            "code": "record.value = record.source_payload.value;",
                            "code_type": "integrationTableTransform",
                            "confirm_write": True,
                        },
                    },
                )

            _validate_javascript_code(
                tool_name="register_code_package", code_name=normalized_name, code=normalized_code
            )

            if dry_run:
                audit_log.record(
                    event_type="code_package_write_result",
                    details={
                        "tool": "register_code_package",
                        "code_name": normalized_name,
                        "operation": "upsert",
                        "outcome": "previewed",
                        "dry_run": True,
                        "execution_status": "not_executed",
                    },
                )
                return {
                    "mode": "dry_run",
                    "status": "success",
                    "tool_name": "register_code_package",
                    "code_name": normalized_name,
                    "code_type": normalized_type,
                    "database_name": database_name,
                    "owner_name": owner_name,
                    "execution_status": "not_executed",
                    "preview": "createCodePackage or alterCodePackage would execute",
                    "warnings": [
                        "Dry run is a local preview only and does not call FairCom backend APIs.",
                        *metadata_warnings,
                    ],
                    "hint": "Set confirm_write=True to apply code package registration.",
                }

            if not confirm_write:
                raise _validation_failure(
                    tool_name="register_code_package",
                    message="register_code_package requires confirm_write=True",
                    expected_args={
                        "confirm_write": "true for non-dry-run changes",
                        "dry_run": "true to preview change",
                    },
                    received_args={
                        "code_name": normalized_name,
                        "confirm_write": confirm_write,
                        "dry_run": dry_run,
                    },
                    suggested_fix=(
                        "Set confirm_write=true to apply the change or dry_run=true to preview it."
                    ),
                    example_payload={
                        "name": "register_code_package",
                        "arguments": {
                            "code_name": normalized_name,
                            "code": normalized_code,
                            "confirm_write": True,
                        },
                    },
                    reason_code="missing_write_confirmation",
                )
        except ValidationFailure as exc:
            audit_log.record(
                event_type="code_package_write_result",
                details={
                    "tool": "register_code_package",
                    "code_name": normalized_name,
                    "operation": "upsert",
                    "outcome": "rejected",
                    "reason_code": exc.details.get("reason_code", "validation_error"),
                    "error_message": exc.message,
                },
            )
            raise

        package_payload: dict[str, object] = {
            "databaseName": database_name,
            "ownerName": owner_name,
            "codeName": normalized_name,
            "codeLanguage": "javascript",
            "codeType": normalized_type,
            "codeStatus": code_status,
            "comment": comment,
            "description": description,
            "metadata": resolved_metadata,
            "code": normalized_code,
            "codeFormat": "utf8",
        }

        def _run_registration() -> object:
            exists = _code_package_exists(
                code_name=normalized_name,
                database_name=database_name,
                owner_name=owner_name,
            )
            if exists:
                return connector_adapter.alter_code_package(package_payload)
            return connector_adapter.create_code_package(package_payload)

        result = _execute_code_package_write(
            tool_name="register_code_package",
            code_name=normalized_name,
            operation="upsert",
            writer=lambda: _run_tool("register_code_package", "write", _run_registration),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {
                    "dry_run_applied": False,
                    "confirm_write_required": True,
                    "mutation_applied": True,
                    "warnings": metadata_warnings,
                }
            )
            return enriched
        return result

    @server.tool(name="clone_code_package")
    def clone_code_package(
        code_name: str,
        new_code_name: str,
        database_name: str = "faircom",
        owner_name: str = "admin",
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        normalized_name = code_name.strip()
        normalized_new_name = new_code_name.strip()
        payload = {
            "databaseName": database_name,
            "ownerName": owner_name,
            "codeName": normalized_name,
            "newCodeName": normalized_new_name,
        }
        audit_log.record(
            event_type="code_package_write_attempt",
            details={
                "tool": "clone_code_package",
                "operation": "clone",
                "code_name": normalized_name,
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return {
                "mode": "dry_run",
                "status": "success",
                "tool_name": "clone_code_package",
                "execution_status": "not_executed",
                "preview": "cloneCodePackage would execute",
                "payload": payload,
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                ],
                "hint": "Set confirm_write=True to apply.",
            }
        if not normalized_name or not normalized_new_name:
            raise _validation_failure(
                tool_name="clone_code_package",
                message="code_name and new_code_name are required",
                expected_args={
                    "code_name": "string (required)",
                    "new_code_name": "string (required)",
                },
                received_args={"code_name": code_name, "new_code_name": new_code_name},
                suggested_fix="Provide non-empty code_name and new_code_name.",
                example_payload={
                    "name": "clone_code_package",
                    "arguments": {
                        "code_name": "decode_mixing_tank",
                        "new_code_name": "decode_mixing_tank_v2",
                        "confirm_write": True,
                    },
                },
            )
        if not confirm_write:
            raise _validation_failure(
                tool_name="clone_code_package",
                message="clone_code_package requires confirm_write=True",
                expected_args={"confirm_write": "true for non-dry-run changes"},
                received_args={"confirm_write": confirm_write, "dry_run": dry_run},
                suggested_fix="Set confirm_write=true to apply the change.",
                example_payload={
                    "name": "clone_code_package",
                    "arguments": {
                        "code_name": normalized_name,
                        "new_code_name": normalized_new_name,
                        "confirm_write": True,
                    },
                },
                reason_code="missing_write_confirmation",
            )
        result = _execute_code_package_write(
            tool_name="clone_code_package",
            code_name=normalized_name,
            operation="clone",
            writer=lambda: _run_tool(
                "clone_code_package",
                "write",
                lambda: connector_adapter.clone_code_package(payload),
            ),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {"dry_run_applied": False, "confirm_write_required": True, "mutation_applied": True}
            )
            return enriched
        return result

    @server.tool(name="revert_code_package")
    def revert_code_package(
        code_name: str,
        version: int,
        database_name: str = "faircom",
        owner_name: str = "admin",
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        normalized_name = code_name.strip()
        payload = {
            "databaseName": database_name,
            "ownerName": owner_name,
            "codeName": normalized_name,
            "version": version,
        }
        audit_log.record(
            event_type="code_package_write_attempt",
            details={
                "tool": "revert_code_package",
                "operation": "revert",
                "code_name": normalized_name,
                "dry_run": dry_run,
                "confirm_write": confirm_write,
            },
        )
        if dry_run:
            return {
                "mode": "dry_run",
                "status": "success",
                "tool_name": "revert_code_package",
                "execution_status": "not_executed",
                "preview": "revertCodePackage would execute",
                "payload": payload,
                "warnings": [
                    "Dry run is a local preview only and does not call FairCom backend APIs.",
                ],
                "hint": "Set confirm_write=True to apply.",
            }
        if not normalized_name:
            raise _validation_failure(
                tool_name="revert_code_package",
                message="code_name is required",
                expected_args={"code_name": "string (required)"},
                received_args={"code_name": code_name},
                suggested_fix="Provide a non-empty code_name.",
                example_payload={
                    "name": "revert_code_package",
                    "arguments": {
                        "code_name": "decode_mixing_tank",
                        "version": 1,
                        "confirm_write": True,
                    },
                },
            )
        if not confirm_write:
            raise _validation_failure(
                tool_name="revert_code_package",
                message="revert_code_package requires confirm_write=True",
                expected_args={"confirm_write": "true for non-dry-run changes"},
                received_args={"confirm_write": confirm_write, "dry_run": dry_run},
                suggested_fix="Set confirm_write=true to apply the change.",
                example_payload={
                    "name": "revert_code_package",
                    "arguments": {
                        "code_name": normalized_name,
                        "version": version,
                        "confirm_write": True,
                    },
                },
                reason_code="missing_write_confirmation",
            )
        result = _execute_code_package_write(
            tool_name="revert_code_package",
            code_name=normalized_name,
            operation="revert",
            writer=lambda: _run_tool(
                "revert_code_package",
                "write",
                lambda: connector_adapter.revert_code_package(payload),
            ),
        )
        if isinstance(result, dict):
            enriched = dict(result)
            enriched.update(
                {"dry_run_applied": False, "confirm_write_required": True, "mutation_applied": True}
            )
            return enriched
        return result

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
        def _strip_tool_aliases(payload: dict[str, object]) -> dict[str, object]:
            tools = payload.get("tools")
            if not isinstance(tools, list):
                return payload
            stripped_payload = dict(payload)
            stripped_tools: list[object] = []
            for tool in tools:
                if isinstance(tool, dict):
                    stripped_tool = dict(tool)
                    stripped_tool.pop("aliases", None)
                    stripped_tools.append(stripped_tool)
                else:
                    stripped_tools.append(tool)
            stripped_payload["tools"] = stripped_tools
            return stripped_payload

        return _run_tool(
            "capabilities_summary",
            "admin",
            lambda: _strip_tool_aliases(
                {
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
                        "read_write_enabled": "write"
                        in resolved_config.security.tool_group_allowlist,
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
                                "Return canonical argument keys, aliases, transport notes, "
                                "minimal payload examples, and self-repair guidance for "
                                "AI client bootstrap."
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
                            "name": "manage_service",
                            "aliases": ["manageService"],
                            "group": "admin",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Manage connector service runtime state with confirmation "
                                "guardrails and dry-run preview."
                            ),
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
                            "name": "list_integration_tables",
                            "aliases": ["listIntegrationTables"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "List FairCom Edge integration tables visible to the "
                                "configured access context."
                            ),
                        },
                        {
                            "name": "describe_integration_tables",
                            "aliases": ["describeIntegrationTables"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "Describe integration tables, including their transformSteps."
                            ),
                        },
                        {
                            "name": "create_integration_table",
                            "aliases": ["createIntegrationTable"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Create an integration table (optionally with transformSteps) "
                                "with confirmation guardrails and dry-run preview."
                            ),
                        },
                        {
                            "name": "list_code_packages",
                            "aliases": ["listCodePackages"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "List registered code package names for a database/owner."
                            ),
                        },
                        {
                            "name": "describe_code_packages",
                            "aliases": ["describeCodePackages"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "Describe registered code packages, including source code."
                            ),
                        },
                        {
                            "name": "register_code_package",
                            "aliases": ["registerCodePackage"],
                            "group": "write",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Create or update a code package via the FairCom admin API "
                                "(createCodePackage/alterCodePackage)."
                            ),
                        },
                        {
                            "name": "clone_code_package",
                            "aliases": ["cloneCodePackage"],
                            "group": "write",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": "Clone an existing code package under a new name.",
                        },
                        {
                            "name": "revert_code_package",
                            "aliases": ["revertCodePackage"],
                            "group": "write",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": "Revert a code package to a prior version.",
                        },
                        {
                            "name": "alter_integration_table",
                            "aliases": ["alterIntegrationTable"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Alter an integration table (fields, transformSteps, retention) "
                                "with confirmation guardrails and dry-run preview."
                            ),
                        },
                        {
                            "name": "delete_integration_tables",
                            "aliases": ["deleteIntegrationTables"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Delete integration tables with confirmation guardrails "
                                "and dry-run preview."
                            ),
                        },
                        {
                            "name": "test_integration_table_transform_steps",
                            "aliases": ["testIntegrationTableTransformSteps"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Test new transform steps against an integration table before "
                                "running them for real. payload.testTransformScope is required: "
                                "one of allRecords, stop, firstRecord, lastRecord, specificRecords."
                            ),
                        },
                        {
                            "name": "list_topics",
                            "aliases": ["listTopics"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "List MQTT topic names the server is tracking (JSON MQ API)."
                            ),
                        },
                        {
                            "name": "describe_topics",
                            "aliases": ["describeTopics"],
                            "group": "metadata",
                            "risk_level": "low",
                            "idempotent": True,
                            "stability": "stable",
                            "description": (
                                "Describe MQTT topics, including their bound integration table "
                                "and transform settings."
                            ),
                        },
                        {
                            "name": "configure_topic",
                            "aliases": ["configureTopic"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Create or update (upsert) an MQTT topic binding to an "
                                "integration table, with confirmation guardrails and dry-run "
                                "preview. This is the delivery path for MQTT output; there is "
                                "no mqtt createOutput service."
                            ),
                        },
                        {
                            "name": "delete_topic",
                            "aliases": ["deleteTopic"],
                            "group": "connector",
                            "risk_level": "critical",
                            "idempotent": False,
                            "stability": "stable",
                            "description": (
                                "Delete an MQTT topic binding with confirmation guardrails "
                                "and dry-run preview."
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
                }
            ),
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
        readiness_state = {"configured": readiness_check is not None, "status": "not_configured"}
        if readiness_check is not None:
            ready_holder: dict[str, bool] = {"value": False}

            def _run_readiness_check() -> None:
                ready_holder["value"] = bool(readiness_check())

            thread = threading.Thread(target=_run_readiness_check, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            if thread.is_alive():
                readiness_state = {
                    "configured": True,
                    "status": "timeout",
                    "timeout_seconds": 2.0,
                }
            elif ready_holder["value"]:
                readiness_state = {"configured": True, "status": "ready"}
            else:
                readiness_state = {"configured": True, "status": "not_ready"}

        upstream_state: dict[str, object] = {"configured": hasattr(client, "admin_action")}
        admin_action = getattr(client, "admin_action", None)
        if callable(admin_action):
            upstream_holder: dict[str, object] = {"error": None}

            def _run_upstream_probe() -> None:
                try:
                    admin_action("listServices", None)
                except Exception as exc:  # pragma: no cover - defensive branch
                    upstream_holder["error"] = exc

            thread = threading.Thread(target=_run_upstream_probe, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            if thread.is_alive():
                upstream_state.update(
                    {
                        "status": "timeout",
                        "timeout_seconds": 2.0,
                    }
                )
            elif upstream_holder["error"] is not None:
                probe_error = cast(Exception, upstream_holder["error"])
                normalized = normalize_exception(probe_error)
                upstream_state.update(
                    {
                        "status": "degraded",
                        "error_code": str(normalized.code),
                        "error_message": normalized.message,
                    }
                )
            else:
                upstream_state.update({"status": "ok"})
        else:
            upstream_state.update({"status": "not_configured"})

        overall_status = "ok"
        if readiness_state.get("configured") and readiness_state.get("status") != "ready":
            overall_status = "degraded"
        if upstream_state.get("configured") and upstream_state.get("status") != "ok":
            overall_status = "degraded"

        return _run_tool(
            "observability_health",
            "admin",
            lambda: {
                "service": "faircom-mcp",
                "status": overall_status,
                "details": {
                    "metrics_enabled": resolved_config.observability.enable_metrics,
                    "tracing_enabled": resolved_config.observability.enable_tracing,
                    "diagnostics_enabled": resolved_config.security.diagnostics_enabled,
                    "readiness": readiness_state,
                    "upstream": upstream_state,
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
        except UnicodeDecodeError:
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
