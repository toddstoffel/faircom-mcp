from __future__ import annotations

import pytest

from faircom_mcp.errors import ValidationFailure
from faircom_mcp.security import SqlStatementPolicy


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
