from __future__ import annotations

from typing import Any, cast

from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.security import SqlStatementPolicy


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def post_action(
        self,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((action, payload))
        return {"action": action, "payload": payload}


def test_table_adapter_calls_expected_actions() -> None:
    client = StubClient()
    adapter = TableAdapter(cast(Any, client))

    tables_result = adapter.list_tables("cust%")
    describe_result = adapter.describe_table("customers")

    assert tables_result["action"] == "list_tables"
    assert tables_result["payload"] == {"tableNameLike": "cust%"}
    assert describe_result["action"] == "describe_table"
    assert describe_result["payload"] == {"tableName": "customers"}
    assert client.calls == [
        ("list_tables", {"tableNameLike": "cust%"}),
        ("describe_table", {"tableName": "customers"}),
    ]


def test_sql_adapter_calls_expected_actions() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    query_result = adapter.query("SELECT * FROM customers WHERE id = ?", [123])
    exec_result = adapter.execute("DELETE FROM customers WHERE id = ?", [123])

    assert query_result["action"] == "sql_query"
    assert query_result["payload"] == {
        "sql": "SELECT * FROM customers WHERE id = ?",
        "params": [123],
    }
    assert exec_result["action"] == "sql_execute"
    assert exec_result["payload"] == {
        "sql": "DELETE FROM customers WHERE id = ?",
        "params": [123],
    }


def test_sql_adapter_enforces_policy_before_request() -> None:
    client = StubClient()
    adapter = SQLAdapter(
        cast(Any, client),
        policy=SqlStatementPolicy(allowlist=("SELECT",), denylist=("DROP",)),
    )

    result = adapter.query("SELECT * FROM customers")

    assert result["action"] == "sql_query"
    assert client.calls == [("sql_query", {"sql": "SELECT * FROM customers"})]

