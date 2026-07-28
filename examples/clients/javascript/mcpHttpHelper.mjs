const DEFAULT_HEADERS = {
  "Content-Type": "application/json",
  Accept: "application/json, text/event-stream",
};

export class MCPHttpClient {
  constructor(endpoint) {
    this.endpoint = endpoint;
    this.sessionId = null;
  }

  async initialize() {
    const payload = {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "javascript-helper", version: "1.0" },
      },
    };

    const response = await this.#post(payload, this.sessionId);
    this.sessionId = response.headers.get("mcp-session-id") || response.headers.get("Mcp-Session-Id");
    return this.#parseResponse(response);
  }

  async callTool(name, args) {
    if (!this.sessionId) {
      await this.initialize();
    }

    const payload = {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: { name, arguments: args },
    };

    const response = await this.#post(payload, this.sessionId);
    const parsed = await this.#parseResponse(response);

    if (this.#needsReinitialize(parsed)) {
      await this.initialize();
      const retryResponse = await this.#post(payload, this.sessionId);
      return this.#parseResponse(retryResponse);
    }

    return parsed;
  }

  async safeQuery(statement) {
    return this.callTool("sql_query", { statement });
  }

  async safeWritePreview(statement) {
    return this.callTool("sql_execute", { statement, dry_run: true });
  }

  async #post(payload, sessionId) {
    const headers = { ...DEFAULT_HEADERS };
    if (sessionId) {
      headers["Mcp-Session-Id"] = sessionId;
    }

    return fetch(this.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
  }

  async #parseResponse(response) {
    if (!response.ok) {
      throw new Error(`MCP request failed: ${response.status}`);
    }
    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    if (contentType.includes("application/json")) {
      return response.json();
    }
    if (contentType.includes("text/event-stream")) {
      const text = await response.text();
      for (const line of text.split("\n")) {
        if (!line.startsWith("data:")) {
          continue;
        }
        const payload = line.slice("data:".length).trim();
        if (!payload) {
          continue;
        }
        try {
          const parsed = JSON.parse(payload);
          if (parsed && typeof parsed === "object") {
            return parsed;
          }
        } catch {
          // Continue scanning subsequent SSE data lines.
        }
      }
      throw new Error("Unable to parse SSE MCP response payload");
    }
    return response.json();
  }

  #needsReinitialize(payload) {
    const payloadText = JSON.stringify(payload).toLowerCase();
    return payloadText.includes("missing_session") || payloadText.includes("stale_session");
  }
}

async function main() {
  const client = new MCPHttpClient("http://127.0.0.1:8000/mcp");
  await client.initialize();

  const queryResult = await client.safeQuery("SELECT TOP 1 id FROM demo_assets ORDER BY id");
  console.log("queryResult", JSON.stringify(queryResult, null, 2));

  const previewResult = await client.safeWritePreview(
    "UPDATE demo_assets SET status='active' WHERE id = 1"
  );
  console.log("previewResult", JSON.stringify(previewResult, null, 2));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
