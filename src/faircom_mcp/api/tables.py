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
