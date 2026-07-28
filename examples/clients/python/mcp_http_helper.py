from __future__ import annotations

import json
from typing import Any

import httpx


class MCPHttpClient:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._session_id: str | None = None

    def initialize(self) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "python-helper", "version": "1.0"},
            },
        }
        response = self._post(payload)
        self._session_id = response.headers.get("mcp-session-id") or response.headers.get(
            "Mcp-Session-Id"
        )
        return self._parse_response(response)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._session_id:
            self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

        response = self._post(payload, session_id=self._session_id)
        parsed = self._parse_response(response)

        if self._needs_reinitialize(parsed):
            self.initialize()
            retry_response = self._post(payload, session_id=self._session_id)
            return self._parse_response(retry_response)

        return parsed

    def safe_query(self, statement: str) -> dict[str, Any]:
        return self.call_tool("sql_query", {"statement": statement})

    def safe_write_preview(self, statement: str) -> dict[str, Any]:
        return self.call_tool("sql_execute", {"statement": statement, "dry_run": True})

    def _post(self, payload: dict[str, Any], session_id: str | None = None) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        return httpx.post(self._endpoint, headers=headers, content=json.dumps(payload), timeout=15.0)

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            return response.json()
        if "text/event-stream" in content_type:
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return response.json()

    @staticmethod
    def _needs_reinitialize(payload: dict[str, Any]) -> bool:
        payload_text = json.dumps(payload).lower()
        return ("missing_session" in payload_text) or ("stale_session" in payload_text)


def main() -> None:
    client = MCPHttpClient("http://127.0.0.1:8000/mcp")
    client.initialize()

    query_result = client.safe_query("SELECT TOP 1 id FROM demo_assets ORDER BY id")
    print("query_result", json.dumps(query_result, indent=2))

    preview_result = client.safe_write_preview(
        "UPDATE demo_assets SET status='active' WHERE id = 1"
    )
    print("preview_result", json.dumps(preview_result, indent=2))


if __name__ == "__main__":
    main()
