from __future__ import annotations

import re
from typing import Any

from faircom_mcp.api.client import FaircomAPIClient
from faircom_mcp.errors import UpstreamAPIError


class TableAdapter:
    def __init__(self, client: FaircomAPIClient) -> None:
        self._client = client

    def list_tables(
        self,
        name_like: str | None = None,
        *,
        database: str | None = None,
    ) -> Any:
        result = self._client.post_action("listTables", None)

        # Edge rejects tableNameLike for listTables (14702), so apply LIKE filtering locally.
        if name_like is None:
            return result

        if not isinstance(result, dict):
            return result

        pattern = self._compile_sql_like_pattern(name_like)
        source_count = 0
        matched_count = 0
        unknown_name_count = 0
        updated_result = dict(result)
        filter_applied = False

        def _filter_entries(entries: list[Any]) -> list[Any]:
            nonlocal source_count
            nonlocal matched_count
            nonlocal unknown_name_count

            source_count = len(entries)
            filtered_entries: list[Any] = []
            for entry in entries:
                table_name = self._extract_table_name(entry)
                if table_name is None:
                    unknown_name_count += 1
                    continue
                if pattern.fullmatch(table_name):
                    filtered_entries.append(entry)
            matched_count = len(filtered_entries)
            return filtered_entries

        tables = updated_result.get("tables")
        if isinstance(tables, list):
            updated_result["tables"] = _filter_entries(tables)
            filter_applied = True
        else:
            result_block = updated_result.get("result")
            if isinstance(result_block, dict):
                data = result_block.get("data")
                if isinstance(data, list):
                    filtered_block = dict(result_block)
                    filtered_block["data"] = _filter_entries(data)
                    updated_result["result"] = filtered_block
                    filter_applied = True

        updated_result["filter"] = {
            "name_like": name_like,
            "applied": filter_applied,
            "source_count": source_count,
            "matched_count": matched_count,
            "unknown_name_count": unknown_name_count,
            "reason": (
                "local_like_filter" if filter_applied else "unsupported_list_tables_response_shape"
            ),
        }

        # The current adapter/runtime path also does not apply database scoping for listTables.
        _ = database
        return updated_result

    def describe_table(self, table_name: str) -> Any:
        payload = {"tableNames": [table_name]}
        try:
            result = self._client.post_action("describeTables", payload)
        except UpstreamAPIError:
            # describeTables (JSON DB API) only manages normal tables; integration
            # tables live in the JSON Hub API instead, so a not-found here isn't final.
            return self._describe_integration_table_fallback(table_name)
        if not isinstance(result, dict):
            return result

        data = (
            result.get("result", {}).get("data") if isinstance(result.get("result"), dict) else None
        )
        if isinstance(data, list) and data:
            return data[0]
        return self._describe_integration_table_fallback(table_name)

    def _describe_integration_table_fallback(self, table_name: str) -> Any:
        try:
            hub_result = self._client.hub_action(
                "describeIntegrationTables", {"tables": [{"tableName": table_name}]}
            )
        except UpstreamAPIError:
            return {}
        record = self._extract_integration_table_record(hub_result, table_name)
        if record is None:
            return {}
        normalized = dict(record)
        fields = record.get("fields")
        if isinstance(fields, list) and "columns" not in normalized:
            normalized["columns"] = fields
        return normalized

    @staticmethod
    def _extract_integration_table_record(value: Any, table_name: str) -> dict[str, Any] | None:
        candidates: list[Any] = []
        if isinstance(value, dict):
            for key in ("tables", "result", "results", "data", "items"):
                nested = value.get(key)
                if isinstance(nested, list):
                    candidates.extend(nested)
                elif isinstance(nested, dict):
                    inner = nested.get("data")
                    if isinstance(inner, list):
                        candidates.extend(inner)
            if not candidates:
                candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(value)

        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("tableName") == table_name:
                return candidate
        for candidate in candidates:
            if isinstance(candidate, dict) and "tableName" in candidate:
                return candidate
        return None

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

    @staticmethod
    def _compile_sql_like_pattern(name_like: str) -> re.Pattern[str]:
        regex_parts: list[str] = []
        for char in name_like:
            if char == "%":
                regex_parts.append(".*")
            elif char == "_":
                regex_parts.append(".")
            else:
                regex_parts.append(re.escape(char))
        regex = "".join(regex_parts)
        return re.compile(f"^{regex}$")

    @staticmethod
    def _extract_table_name(entry: Any) -> str | None:
        if isinstance(entry, str):
            return entry
        if not isinstance(entry, dict):
            return None

        for key in ("tableName", "table_name", "name", "table"):
            value = entry.get(key)
            if isinstance(value, str):
                return value
        return None
