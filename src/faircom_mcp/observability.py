from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tool_calls: dict[tuple[str, str], int] = defaultdict(int)
        self._tool_seconds: dict[str, float] = defaultdict(float)
        self._compat_events: dict[tuple[str, str], int] = defaultdict(int)

    def record_tool_call(self, *, tool: str, status: str, duration_seconds: float) -> None:
        with self._lock:
            self._tool_calls[(tool, status)] += 1
            self._tool_seconds[tool] += max(0.0, duration_seconds)

    def record_compat_event(self, *, tool: str, event: str) -> None:
        with self._lock:
            self._compat_events[(tool, event)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tool_calls": {
                    f"{tool}:{status}": count
                    for (tool, status), count in sorted(self._tool_calls.items())
                },
                "tool_seconds_total": {
                    tool: round(total, 6) for tool, total in sorted(self._tool_seconds.items())
                },
                "compatibility_events": {
                    f"{tool}:{event}": count
                    for (tool, event), count in sorted(self._compat_events.items())
                },
            }

    def to_prometheus(self) -> str:
        lines = [
            "# HELP faircom_mcp_tool_calls_total Total tool calls by tool and status",
            "# TYPE faircom_mcp_tool_calls_total counter",
        ]

        with self._lock:
            for (tool, status), count in sorted(self._tool_calls.items()):
                lines.append(
                    f'faircom_mcp_tool_calls_total{{tool="{tool}",status="{status}"}} {count}'
                )

            lines.extend(
                [
                    "# HELP faircom_mcp_tool_seconds_total Cumulative tool execution seconds",
                    "# TYPE faircom_mcp_tool_seconds_total counter",
                ]
            )
            for tool, total in sorted(self._tool_seconds.items()):
                lines.append(f'faircom_mcp_tool_seconds_total{{tool="{tool}"}} {total:.6f}')

            lines.extend(
                [
                    "# HELP faircom_mcp_compatibility_events_total Compatibility and self-repair events",
                    "# TYPE faircom_mcp_compatibility_events_total counter",
                ]
            )
            for (tool, event), count in sorted(self._compat_events.items()):
                lines.append(
                    (
                        "faircom_mcp_compatibility_events_total"
                        f'{{tool="{tool}",event="{event}"}} {count}'
                    )
                )

        return "\n".join(lines) + "\n"


class AuditLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    def record(self, *, event_type: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._events.append(
                {
                    "type": event_type,
                    "details": details or {},
                }
            )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]


def build_tracer(*, enabled: bool) -> Any:
    if not enabled:
        return None

    try:
        from opentelemetry import trace
    except Exception:
        return None

    return trace.get_tracer("faircom_mcp.server")


@contextmanager
def maybe_span(
    tracer: Any,
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Iterator[None]:
    if tracer is None:
        yield
        return

    with tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        yield
