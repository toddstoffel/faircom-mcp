from __future__ import annotations

from dataclasses import dataclass

from faircom_mcp.errors import PolicyViolation

DEFAULT_TOOL_GROUP_ALLOWLIST = (
    "metadata",
    "query",
    "write",
    "connector",
    "admin",
    "diagnostics",
)

_POLICY_PRESETS = {
    "default": DEFAULT_TOOL_GROUP_ALLOWLIST,
    "read_only": ("metadata", "query", "diagnostics"),
    "analyst": ("metadata", "query", "diagnostics"),
    "operator": ("metadata", "query", "write", "connector", "diagnostics"),
    "admin": DEFAULT_TOOL_GROUP_ALLOWLIST,
}


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.strip().split()).upper()


def get_tool_group_allowlist_for_preset(preset: str | None) -> tuple[str, ...]:
    if preset is None:
        return DEFAULT_TOOL_GROUP_ALLOWLIST

    normalized = preset.strip().lower()
    return _POLICY_PRESETS.get(normalized, DEFAULT_TOOL_GROUP_ALLOWLIST)


@dataclass(slots=True, frozen=True)
class SqlStatementPolicy:
    allowlist: tuple[str, ...] = ()
    denylist: tuple[str, ...] = ()

    def validate(self, statement: str, *, operation: str) -> None:
        normalized_statement = _normalize_sql(statement)

        if self.allowlist and not any(
            normalized_statement.startswith(prefix) for prefix in self.allowlist
        ):
            raise PolicyViolation(
                "SQL operation is not permitted by policy",
                details={
                    "operation": operation,
                    "policy": "allowlist",
                },
                hint="Use an allowed SQL verb or adjust the SQL allowlist.",
            )

        if any(fragment in normalized_statement for fragment in self.denylist):
            raise PolicyViolation(
                "SQL operation is not permitted by policy",
                details={
                    "operation": operation,
                    "policy": "denylist",
                },
                hint="Remove the denied SQL fragment or request an approved change.",
            )


@dataclass(slots=True, frozen=True)
class ToolGroupPolicy:
    allowlist: tuple[str, ...] = DEFAULT_TOOL_GROUP_ALLOWLIST
    policy_name: str | None = None

    def validate(self, group: str) -> None:
        normalized_group = group.strip().lower()
        if normalized_group in self.allowlist:
            return

        raise PolicyViolation(
            "Tool group is not permitted by policy",
            details={
                "policy": "tool_group_allowlist",
                "group": normalized_group,
                "policy_name": self.policy_name or "default",
            },
            hint="Choose an allowed tool group for this role or request an expanded policy.",
        )
