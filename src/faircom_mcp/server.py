from __future__ import annotations

import html
import json
import logging
import time
import types
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from faircom_mcp.api.client import FaircomAPIClient, create_client
from faircom_mcp.api.connectors import ConnectorAdapter
from faircom_mcp.api.dialect import detect_unsupported_features, normalize_select_first_to_top
from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.config import AppConfig, load_config
from faircom_mcp.errors import FaircomError, ValidationFailure
from faircom_mcp.observability import AuditLog, RuntimeMetrics, build_tracer, maybe_span


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
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "mode": "dry_run",
            "status": "success",
            "tool_name": tool_name,
            "action": action,
            "payload": payload,
            "would_succeed": True,
            "preview": "Connector change would execute",
            "preview_details": {
                "action": action,
                "target": payload or {},
                "row_estimate": "unknown",
                "upstream_validated": False,
                "schema_validated": False,
            },
            "warnings": [
                "Dry run is a local preview only and does not call FairCom backend APIs.",
                "Dry run does not validate connector schema fields beyond local argument checks.",
            ],
            "hint": (
                "Review the preview above. Then run list_inputs/describe_inputs to verify upstream "
                "connectivity before calling with confirm_write=True."
            ),
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
        connector_name = normalized_payload.get("connectorName")
        if (not isinstance(connector_name, str) or not connector_name.strip()) and action in {
            "createInput",
            "alterInput",
            "deleteInput",
        }:
            input_name = normalized_payload.get("inputName")
            if isinstance(input_name, str) and input_name.strip():
                connector_name = input_name.strip()
                normalized_payload["connectorName"] = connector_name

        if not isinstance(connector_name, str) or not connector_name.strip():
            raise _validation_failure(
                tool_name=tool_name,
                message="connectorName is required in connector payload",
                expected_args={
                    "payload": "object (required)",
                    "payload.connectorName": "string (required)",
                    "payload.inputName": "string (accepted alias for input actions)",
                    "confirm_write": "true for non-dry-run changes",
                    "dry_run": "true to preview change",
                },
                received_args={"payload": payload, "action": action},
                suggested_fix=(
                    "Provide payload.connectorName with a non-empty connector name. "
                    "For input actions, payload.inputName is also accepted."
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
        if action in {"createInput", "alterInput", "deleteInput"}:
            input_name = normalized_payload.get("inputName")
            if not isinstance(input_name, str) or not input_name.strip():
                normalized_payload["inputName"] = connector_name

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
<h1>FairCom MCP Diagnostics</h1>
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="create_input",
            payload=payload,
            action="createInput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return create_input(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="alter_input")
    def alter_input(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="alter_input",
            payload=payload,
            action="alterInput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return alter_input(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="delete_input")
    def delete_input(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="delete_input",
            payload=payload,
            action="deleteInput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="create_output",
            payload=payload,
            action="createOutput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return create_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="alter_output")
    def alter_output(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="alter_output",
            payload=payload,
            action="alterOutput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return alter_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

    @server.tool(name="delete_output")
    def delete_output(
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        resolved_payload = _require_connector_payload(
            tool_name="delete_output",
            payload=payload,
            action="deleteOutput",
        )
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
                    payload=resolved_payload,
                ),
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
        payload: dict[str, object] | None = None,
        confirm_write: bool = False,
        dry_run: bool = False,
    ) -> object:
        return delete_output(payload=payload, confirm_write=confirm_write, dry_run=dry_run)

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
        return _run_tool(
            "get_usage_contract",
            "admin",
            lambda: {
                "contract_version": "2026-07-28",
                "updated_at": "2026-07-28",
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
                            "payload": {"connectorName": "demo_input", "type": "input"},
                            "confirm_write": True,
                        },
                    },
                    "create_output": {
                        "name": "create_output",
                        "arguments": {
                            "payload": {"connectorName": "demo_output", "type": "output"},
                            "confirm_write": True,
                        },
                    },
                },
            },
        )

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
                    "version": "0.1.11",
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
