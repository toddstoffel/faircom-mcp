from __future__ import annotations

from typing import Any, cast

from faircom_mcp.api.sql import SQLAdapter
from faircom_mcp.api.tables import TableAdapter
from faircom_mcp.errors import ValidationFailure
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

    assert tables_result["action"] == "listTables"
    assert tables_result["payload"] == {"tableNameLike": "cust%"}
    assert describe_result["action"] == "describeTables"
    assert describe_result["payload"] == {"tableNames": ["customers"]}
    assert client.calls == [
        ("listTables", {"tableNameLike": "cust%"}),
        ("describeTables", {"tableNames": ["customers"]}),
    ]


def test_table_adapter_extracts_columns_and_indexes() -> None:
    client = StubClient()
    adapter = TableAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "columns": [{"name": "id"}, {"name": "name"}],
        "indexes": [{"name": "pk_customers"}],
    }

    columns = adapter.list_table_columns("customers")
    indexes = adapter.list_table_indexes("customers")

    assert columns == {
        "table_name": "customers",
        "columns": [{"name": "id"}, {"name": "name"}],
        "column_count": 2,
    }
    assert indexes == {
        "table_name": "customers",
        "indexes": [{"name": "pk_customers"}],
        "index_count": 1,
    }


def test_sql_adapter_calls_expected_actions() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    query_result = adapter.query("SELECT * FROM customers WHERE id = ?", [123])
    exec_result = adapter.execute("DELETE FROM customers WHERE id = ?", [123])

    assert query_result["action"] == "getRecordsUsingSQL"
    assert query_result["payload"] == {
        "sql": "SELECT * FROM customers WHERE id = ?",
        "sqlParams": [{"name": "p1", "value": 123}],
    }
    assert exec_result["action"] == "runSQLStatements"
    assert exec_result["payload"] == {
        "sqlStatements": ["DELETE FROM customers WHERE id = ?"],
        "inParams": [{"name": "p1", "value": 123}],
    }


def test_sql_adapter_paginated_query_calls_expected_action() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    # Simulate a page with exactly page_size rows so metadata should indicate another page.
    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "result": {"data": [{"id": 1}, {"id": 2}], "moreRecords": False},
    }

    page_result = adapter.query_page(
        "SELECT * FROM customers ORDER BY id",
        ["active"],
        page=2,
        page_size=250,
    )

    assert page_result["action"] == "getRecordsUsingSQL"
    assert page_result["payload"] == {
        "sql": "SELECT * FROM customers ORDER BY id",
        "sqlParams": [{"name": "p1", "value": "active"}],
        "skipRecords": 250,
        "maxRecords": 250,
    }
    assert page_result["has_more"] is False
    assert page_result["next_page"] is None
    assert page_result["next_cursor"] is None


def test_sql_adapter_paginated_query_adds_cursor_metadata() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    client.post_action = lambda action, payload=None: {
        "action": action,
        "payload": payload,
        "result": {"data": [{"id": 1}, {"id": 2}, {"id": 3}], "moreRecords": True},
    }

    page_result = adapter.query_page(
        "SELECT * FROM customers ORDER BY id",
        page=1,
        page_size=3,
    )

    assert page_result["has_more"] is True
    assert page_result["next_page"] == 2
    assert page_result["next_cursor"] == 2


def test_sql_adapter_paginated_query_validates_paging_inputs() -> None:
    client = StubClient()
    adapter = SQLAdapter(cast(Any, client))

    try:
        adapter.query_page("SELECT 1", page=0)
    except ValidationFailure as exc:
        assert exc.details == {"page": 0}
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected page validation failure")

    try:
        adapter.query_page("SELECT 1", page_size=0)
    except ValidationFailure as exc:
        assert exc.details == {"page_size": 0}
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected page_size validation failure")


def test_sql_adapter_enforces_policy_before_request() -> None:
    client = StubClient()
    adapter = SQLAdapter(
        cast(Any, client),
        policy=SqlStatementPolicy(allowlist=("SELECT",), denylist=("DROP",)),
    )

    result = adapter.query("SELECT * FROM customers")

    assert result["action"] == "getRecordsUsingSQL"
    assert client.calls == [("getRecordsUsingSQL", {"sql": "SELECT * FROM customers"})]
