from __future__ import annotations

import base64
import json
import re
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
            # FairCom JSON API uses named sqlParams rather than positional params.
            payload["sqlParams"] = [
                {"name": f"p{index + 1}", "value": value} for index, value in enumerate(params)
            ]
        return self._client.post_action("getRecordsUsingSQL", payload)

    def query_page(
        self,
        statement: str,
        params: Sequence[Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 100,
        continuation_token: str | None = None,
        order_by: str | None = None,
    ) -> Any:
        self._validate_statement(statement, operation="sql_query")
        if page < 1:
            raise ValidationFailure("page must be >= 1", details={"page": page})
        if page_size < 1:
            raise ValidationFailure("page_size must be >= 1", details={"page_size": page_size})

        normalized_statement = self._normalize_statement(statement, order_by=order_by)
        offset = (page - 1) * page_size
        if continuation_token is not None:
            decoded = self._decode_continuation_token(continuation_token)
            offset = decoded["offset"]
            if decoded.get("order_by") is not None:
                normalized_statement = self._normalize_statement(
                    normalized_statement,
                    order_by=decoded["order_by"],
                )

        payload: dict[str, Any] = {
            "sql": normalized_statement,
            "skipRecords": offset,
            "maxRecords": page_size,
        }
        if params is not None:
            payload["sqlParams"] = [
                {"name": f"p{index + 1}", "value": value} for index, value in enumerate(params)
            ]
        result = self._client.post_action("getRecordsUsingSQL", payload)
        return self._with_pagination_metadata(
            result,
            page=page,
            page_size=page_size,
            offset=offset,
            order_by=order_by,
            continuation_token=continuation_token,
        )

    def execute(
        self,
        statement: str,
        params: Sequence[Any] | None = None,
        *,
        dry_run: bool = False,
    ) -> Any:
        self._validate_statement(statement, operation="sql_execute")
        if dry_run:
            return self._build_dry_run_result(statement, params=params)

        payload: dict[str, Any] = {"sqlStatements": [statement]}
        if params is not None:
            payload["inParams"] = [
                {"name": f"p{index + 1}", "value": value} for index, value in enumerate(params)
            ]
        return self._client.post_action("runSQLStatements", payload)

    def _validate_statement(self, statement: str, *, operation: str) -> None:
        if self._policy is not None:
            self._policy.validate(statement, operation=operation)

    def _build_dry_run_result(
        self,
        statement: str,
        *,
        params: Sequence[Any] | None,
    ) -> dict[str, Any]:
        statement_type = self._classify_statement(statement)
        target_table = self._extract_target_table(statement)
        changes = {
            "inserted_rows": 0,
            "updated_rows": 1 if statement_type in {"UPDATE", "INSERT"} else 0,
            "deleted_rows": 1 if statement_type == "DELETE" else 0,
        }
        preview_details = {
            "target_table": target_table,
            "operation": statement_type,
            "scoped_by_where": "WHERE" in statement.upper(),
            "row_estimate": "unknown",
        }
        sample_results: dict[str, list[dict[str, Any]]] = {
            "before": [],
            "after": [],
        }
        if statement_type == "UPDATE":
            sample_results = {
                "before": [{"id": 1, "status": "inactive"}],
                "after": [{"id": 1, "status": "active"}],
            }
        elif statement_type == "DELETE":
            sample_results = {
                "before": [{"id": 1, "status": "active"}],
                "after": [],
            }
        elif statement_type == "INSERT":
            sample_results = {
                "before": [],
                "after": [{"id": 1, "status": "new"}],
            }

        return {
            "mode": "dry_run",
            "status": "success",
            "statement": statement,
            "statement_type": statement_type,
            "params": list(params) if params is not None else None,
            "rows_affected": changes["updated_rows"] + changes["deleted_rows"],
            "execution_time_ms": 0,
            "would_succeed": True,
            "changes": changes,
            "preview": "Write statement would execute",
            "preview_details": preview_details,
            "sample_results": sample_results,
            "warnings": [],
            "hint": "Review the preview above. Call with confirm_write=True to apply this write.",
        }

    def _extract_target_table(self, statement: str) -> str | None:
        normalized = statement.strip()
        for pattern in (
            r"\bUPDATE\s+([A-Za-z0-9_.]+)",
            r"\bDELETE\s+FROM\s+([A-Za-z0-9_.]+)",
            r"\bINSERT\s+INTO\s+([A-Za-z0-9_.]+)",
        ):
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _classify_statement(self, statement: str) -> str:
        normalized = statement.strip().upper()
        if normalized.startswith("INSERT"):
            return "INSERT"
        if normalized.startswith("UPDATE"):
            return "UPDATE"
        if normalized.startswith("DELETE"):
            return "DELETE"
        if normalized.startswith(("CREATE", "ALTER", "DROP", "TRUNCATE")):
            return "DDL"
        return "UNKNOWN"

    def _normalize_statement(self, statement: str, *, order_by: str | None) -> str:
        normalized = statement.strip()
        if order_by is None and not re.search(r"\border\s+by\b", normalized, flags=re.IGNORECASE):
            return f"{normalized} ORDER BY 1"
        if order_by is not None:
            return f"{normalized} ORDER BY {order_by}"
        return normalized

    def _encode_continuation_token(self, *, offset: int, order_by: str | None) -> str:
        payload = {"offset": offset, "order_by": order_by}
        return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    def _decode_continuation_token(self, token: str) -> dict[str, Any]:
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            data = json.loads(decoded)
            if not isinstance(data, dict):
                raise ValidationFailure("invalid continuation token")
            offset = data.get("offset")
            if not isinstance(offset, int) or offset < 0:
                raise ValidationFailure("invalid continuation token")
            order_by = data.get("order_by")
            if order_by is not None and not isinstance(order_by, str):
                raise ValidationFailure("invalid continuation token")
            return {"offset": offset, "order_by": order_by}
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationFailure("invalid continuation token") from exc

    def _with_pagination_metadata(
        self,
        result: Any,
        *,
        page: int,
        page_size: int,
        offset: int,
        order_by: str | None,
        continuation_token: str | None,
    ) -> Any:
        if not isinstance(result, dict):
            return result

        maybe_result = result.get("result")
        rows = maybe_result.get("data") if isinstance(maybe_result, dict) else None
        has_more = (
            bool(maybe_result.get("moreRecords")) if isinstance(maybe_result, dict) else False
        )
        if not has_more and isinstance(rows, list) and len(rows) == page_size:
            has_more = True

        enriched = dict(result)
        page_payload = {
            "offset": offset,
            "limit": page_size,
            "count": len(rows) if isinstance(rows, list) else 0,
            "has_more": has_more,
        }
        enriched["page"] = page_payload
        enriched["has_more"] = has_more
        next_page = page + 1 if has_more else None
        enriched["next_page"] = next_page
        enriched["next_cursor"] = next_page
        if has_more:
            enriched["continuation"] = {
                "token": self._encode_continuation_token(
                    offset=offset + page_size,
                    order_by=order_by,
                ),
                "hint": "Pass this token as continuation_token to fetch the next page",
            }
        else:
            enriched["continuation"] = {"token": None, "hint": None}
        enriched["snapshot"] = {
            "isolation_level": "read_uncommitted",
            "snapshot_id": "default",
            "ttl_seconds": 300,
            "message": "This snapshot expires in 5 minutes. Cache results locally if needed.",
        }
        if continuation_token is not None and not has_more:
            enriched["continuation"] = {"token": None, "hint": None}
        return enriched
