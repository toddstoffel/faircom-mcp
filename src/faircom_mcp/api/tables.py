from __future__ import annotations

from typing import Any

from faircom_mcp.api.client import FaircomAPIClient


class TableAdapter:
    def __init__(self, client: FaircomAPIClient) -> None:
        self._client = client

    def list_tables(self, name_like: str | None = None) -> Any:
        payload: dict[str, str] = {}
        if name_like:
            payload["tableNameLike"] = name_like
        return self._client.post_action("list_tables", payload or None)

    def describe_table(self, table_name: str) -> Any:
        payload = {"tableName": table_name}
        return self._client.post_action("describe_table", payload)

    def list_table_columns(self, table_name: str) -> dict[str, Any]:
        description = self.describe_table(table_name)
        columns = self._extract_list(description, "columns")
        return {
            "table_name": table_name,
            "columns": columns,
            "column_count": len(columns),
        }

    def list_table_indexes(self, table_name: str) -> dict[str, Any]:
        description = self.describe_table(table_name)
        indexes = self._extract_list(description, "indexes")
        if not indexes:
            indexes = self._extract_list(description, "indices")
        return {
            "table_name": table_name,
            "indexes": indexes,
            "index_count": len(indexes),
        }

    @staticmethod
    def _extract_list(description: Any, key: str) -> list[Any]:
        if not isinstance(description, dict):
            return []

        value = description.get(key)
        if isinstance(value, list):
            return value
        return []
