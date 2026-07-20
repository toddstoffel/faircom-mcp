from __future__ import annotations

from dataclasses import dataclass

from faircom_mcp.errors import ValidationFailure


def _normalize_sql(statement: str) -> str:
	return " ".join(statement.strip().split()).upper()


@dataclass(slots=True, frozen=True)
class SqlStatementPolicy:
	allowlist: tuple[str, ...] = ()
	denylist: tuple[str, ...] = ()

	def validate(self, statement: str, *, operation: str) -> None:
		normalized_statement = _normalize_sql(statement)

		if self.allowlist and not any(
			normalized_statement.startswith(prefix) for prefix in self.allowlist
		):
			raise ValidationFailure(
				"SQL operation is not permitted by policy",
				details={
					"operation": operation,
					"policy": "allowlist",
				},
			)

		if any(fragment in normalized_statement for fragment in self.denylist):
			raise ValidationFailure(
				"SQL operation is not permitted by policy",
				details={
					"operation": operation,
					"policy": "denylist",
				},
			)


@dataclass(slots=True, frozen=True)
class ToolGroupPolicy:
	allowlist: tuple[str, ...] = (
		"metadata",
		"query",
		"write",
		"admin",
		"diagnostics",
	)

	def validate(self, group: str) -> None:
		normalized_group = group.strip().lower()
		if normalized_group in self.allowlist:
			return

		raise ValidationFailure(
			"Tool group is not permitted by policy",
			details={
				"policy": "tool_group_allowlist",
				"group": normalized_group,
			},
		)
