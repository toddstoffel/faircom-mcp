from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from faircom_mcp.api.client import FaircomAPIClient
from faircom_mcp.security import SqlStatementPolicy


class SQLAdapter:
    def __init__(
        self,
        client: FaircomAPIClient,
        policy: SqlStatementPolicy | None = None,
    ) -> None:
        self._client = client
        self._policy = policy

    def query(self, statement: str, params: Sequence[Any] | None = None) -> Any:
        self._validate_statement(statement, operation="sql_query")
        payload: dict[str, Any] = {"sql": statement}
        if params is not None:
            payload["params"] = list(params)
        return self._client.post_action("sql_query", payload)

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> Any:
        self._validate_statement(statement, operation="sql_execute")
        payload: dict[str, Any] = {"sql": statement}
        if params is not None:
            payload["params"] = list(params)
        return self._client.post_action("sql_execute", payload)

    def _validate_statement(self, statement: str, *, operation: str) -> None:
        if self._policy is not None:
            self._policy.validate(statement, operation=operation)
