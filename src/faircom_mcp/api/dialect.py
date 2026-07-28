from __future__ import annotations

import re

_UNSUPPORTED_FEATURE_TOKENS = ("LIMIT", "OFFSET", "FETCH")


def detect_unsupported_features(statement: str) -> list[str]:
    return sorted(
        {
            token
            for token in _UNSUPPORTED_FEATURE_TOKENS
            if re.search(rf"\b{token}\b", statement, flags=re.IGNORECASE)
        }
    )


def normalize_select_first_to_top(statement: str) -> tuple[str, list[dict[str, str]]]:
    match = re.match(r"(\s*SELECT\s+)FIRST\s+(\d+)\b", statement, flags=re.IGNORECASE)
    if not match:
        return statement, []

    prefix = match.group(1)
    limit_value = match.group(2)
    remainder = statement[match.end() :]
    normalized = f"{prefix}TOP {limit_value}{remainder}"
    return normalized, [{"from": "FIRST", "to": "TOP"}]
