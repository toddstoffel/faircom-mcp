from __future__ import annotations

import pytest

from faircom_mcp.errors import ValidationFailure
from faircom_mcp.security import SqlStatementPolicy, ToolGroupPolicy


def test_sql_policy_allows_expected_statement() -> None:
    policy = SqlStatementPolicy(allowlist=("SELECT",), denylist=("DROP",))

    policy.validate("select * from customers", operation="sql_query")


def test_sql_policy_blocks_disallowed_verb() -> None:
    policy = SqlStatementPolicy(allowlist=("SELECT",), denylist=())

    with pytest.raises(ValidationFailure) as exc:
        policy.validate("delete from customers", operation="sql_execute")

    assert exc.value.details["policy"] == "allowlist"


def test_sql_policy_blocks_denylisted_fragment() -> None:
    policy = SqlStatementPolicy(allowlist=(), denylist=("DROP", "TRUNCATE"))

    with pytest.raises(ValidationFailure) as exc:
        policy.validate("drop table customers", operation="sql_execute")

    assert exc.value.details["policy"] == "denylist"


def test_tool_group_policy_allows_group() -> None:
    policy = ToolGroupPolicy(allowlist=("metadata", "query"))

    policy.validate("query")


def test_tool_group_policy_blocks_disallowed_group() -> None:
    policy = ToolGroupPolicy(allowlist=("metadata", "query"))

    with pytest.raises(ValidationFailure) as exc:
        policy.validate("write")

    assert exc.value.details["policy"] == "tool_group_allowlist"
