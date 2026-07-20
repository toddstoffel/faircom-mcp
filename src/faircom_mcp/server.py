from __future__ import annotations

import html
import json
import logging
import time
from collections.abc import Callable
from typing import Literal

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from faircom_mcp.api.client import FaircomAPIClient, create_client
from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.config import AppConfig, load_config
from faircom_mcp.errors import ValidationFailure
from faircom_mcp.observability import RuntimeMetrics, build_tracer, maybe_span


def create_server(
    config: AppConfig | None = None,
    *,
    client_factory: Callable[[AppConfig], FaircomAPIClient] = create_client,
    readiness_check: Callable[[], bool] | None = None,
) -> FastMCP:
    resolved_config = config or load_config()
    client = client_factory(resolved_config)
    tables = TableAdapter(client)
    sql = SQLAdapter(client, policy=resolved_config.security.to_sql_policy())
    tool_group_policy = resolved_config.security.to_tool_group_policy()
    metrics = RuntimeMetrics()
    tracer = build_tracer(enabled=resolved_config.observability.enable_tracing)
    logger = logging.getLogger("faircom_mcp")

    server = FastMCP("faircom-mcp")

    def _run_tool(tool_name: str, group: str, action: Callable[[], object]) -> object:
        tool_group_policy.validate(group)
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
        except Exception:
            metrics.record_tool_call(
                tool=tool_name,
                status="error",
                duration_seconds=time.perf_counter() - started,
            )
            raise

        metrics.record_tool_call(
            tool=tool_name,
            status="success",
            duration_seconds=time.perf_counter() - started,
        )
        return result

    def _is_diagnostics_authorized(request: Request) -> bool:
        token = resolved_config.security.diagnostics_token
        if not token:
            return False

        provided = request.headers.get("x-diagnostics-token")
        if provided is None:
            provided = request.query_params.get("token")
        return provided == token

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

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
    def list_tables(name_like: str | None = None) -> object:
        return _run_tool(
            "list_tables",
            "metadata",
            lambda: tables.list_tables(name_like=name_like),
        )

    @server.tool(name="describe_table")
    def describe_table(table_name: str) -> object:
        return _run_tool(
            "describe_table",
            "metadata",
            lambda: tables.describe_table(table_name),
        )

    @server.tool(name="list_table_columns")
    def list_table_columns(table_name: str) -> object:
        return _run_tool(
            "list_table_columns",
            "metadata",
            lambda: tables.list_table_columns(table_name),
        )

    @server.tool(name="list_table_indexes")
    def list_table_indexes(table_name: str) -> object:
        return _run_tool(
            "list_table_indexes",
            "metadata",
            lambda: tables.list_table_indexes(table_name),
        )

    @server.tool(name="sql_query")
    def sql_query(statement: str, params: list[object] | None = None) -> object:
        return _run_tool(
            "sql_query",
            "query",
            lambda: sql.query(statement, params=params),
        )

    @server.tool(name="sql_query_page")
    def sql_query_page(
        statement: str,
        params: list[object] | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> object:
        return _run_tool(
            "sql_query_page",
            "query",
            lambda: sql.query_page(
                statement,
                params=params,
                page=page,
                page_size=page_size,
            ),
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

    @server.tool(name="sql_execute")
    def sql_execute(
        statement: str,
        params: list[object] | None = None,
        confirm_write: bool = False,
    ) -> object:
        logger.info(
            "sql_execute requested",
            extra={"statement": statement, "params": params, "confirm_write": confirm_write},
        )
        if not confirm_write:
            raise ValidationFailure(
                "sql_execute requires confirm_write=True",
                details={"tool": "sql_execute"},
            )
        return _run_tool(
            "sql_execute",
            "write",
            lambda: sql.execute(statement, params=params),
        )

    _ = resolved_config
    return server


def create_http_app(
    config: AppConfig | None = None,
    *,
    readiness_check: Callable[[], bool] | None = None,
    transport: Literal["http", "sse"] = "http",
) -> Starlette:
    server = create_server(config, readiness_check=readiness_check)
    return server.http_app(transport=transport)


def create_stdio_server(
    config: AppConfig | None = None,
    *,
    readiness_check: Callable[[], bool] | None = None,
) -> FastMCP:
    return create_server(config, readiness_check=readiness_check)
