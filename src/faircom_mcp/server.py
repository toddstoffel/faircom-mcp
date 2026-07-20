from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from faircom_mcp.api.client import FaircomAPIClient, create_client
from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.config import AppConfig, load_config
from faircom_mcp.errors import ValidationFailure


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
    logger = logging.getLogger("faircom_mcp")

    server = FastMCP("faircom-mcp")

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        is_ready = bool(readiness_check() if readiness_check is not None else True)
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status}, status_code=status_code)

    @server.tool(name="list_tables")
    def list_tables(name_like: str | None = None) -> object:
        return tables.list_tables(name_like=name_like)

    @server.tool(name="describe_table")
    def describe_table(table_name: str) -> object:
        return tables.describe_table(table_name)

    @server.tool(name="sql_query")
    def sql_query(statement: str, params: list[object] | None = None) -> object:
        return sql.query(statement, params=params)

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
        return sql.execute(statement, params=params)

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
