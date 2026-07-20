from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from faircom_mcp.api.client import FaircomAPIClient
from faircom_mcp.errors import ValidationFailure
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

    def query_page(
        self,
        statement: str,
        params: Sequence[Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> Any:
        self._validate_statement(statement, operation="sql_query")
        if page < 1:
            raise ValidationFailure("page must be >= 1", details={"page": page})
        if page_size < 1:
            raise ValidationFailure("page_size must be >= 1", details={"page_size": page_size})

        payload: dict[str, Any] = {
            "sql": statement,
            "page": page,
            "pageSize": page_size,
        }
        if params is not None:
            payload["params"] = list(params)
        result = self._client.post_action("sql_query", payload)
        return self._with_pagination_metadata(result, page=page, page_size=page_size)

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> Any:
        self._validate_statement(statement, operation="sql_execute")
        payload: dict[str, Any] = {"sql": statement}
        if params is not None:
            payload["params"] = list(params)
        return self._client.post_action("sql_execute", payload)

    def _validate_statement(self, statement: str, *, operation: str) -> None:
        if self._policy is not None:
            self._policy.validate(statement, operation=operation)

    def _with_pagination_metadata(self, result: Any, *, page: int, page_size: int) -> Any:
        if not isinstance(result, dict):
            return result

        rows = result.get("rows")
        has_more = isinstance(rows, list) and len(rows) == page_size

        enriched = dict(result)
        enriched["has_more"] = has_more
        next_page = page + 1 if has_more else None
        enriched["next_page"] = next_page
        enriched["next_cursor"] = next_page
        return enriched
