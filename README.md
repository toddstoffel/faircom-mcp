# FairCom MCP Server

Connect AI assistants and LLMs to FairCom databases with production-grade safety controls, Linux packaging, and operational discipline.

```
┌─────────────────────────────────────────────────────────────┐
│  Your AI Assistant (Claude, Copilot, etc.)                  │
└────────────────────┬────────────────────────────────────────┘
                     │ MCP Protocol
                     │ (HTTP + JSON-RPC)
┌────────────────────▼────────────────────────────────────────┐
│  FairCom MCP Server                                         │
│  • Session management                                       │
│  • Write safety enforcement (confirm_write=true)           │
│  • Tool exposure control                                    │
│  • Rate limiting, observability                            │
└────────────────────┬────────────────────────────────────────┘
                     │ FairCom JSON API
                     │ (HTTP REST)
┌────────────────────▼────────────────────────────────────────┐
│  FairCom Database                                           │
│  (Edge, DB, RTG, ISAM, MQ)                                 │
└─────────────────────────────────────────────────────────────┘
```

**Why FairCom MCP?**

- ✅ **Open source** – Apache 2.0, transparent, community-driven
- ✅ **Production ready** – systemd service, log rotation, health checks
- ✅ **Safe by default** – Explicit write confirmation, tool allowlisting
- ✅ **Portfolio-wide** – Works with Edge, DB, RTG, ISAM, MQ
- ✅ **AI-native** – Purpose-built for Claude, Copilot, local LLMs

## Use Cases

### 1. Business Intelligence & Reporting
**Empower business users to ask natural language questions about FairCom data.**

*Example: "What were our top 5 products by revenue last quarter?"*

The AI assistant translates this to SQL, queries FairCom, and summarizes results with visualizations.

```python
# FairCom MCP exposes:
# sql_query(statement, params?) → fetch data
# list_tables(name_like?) → discover schema
# list_table_columns(table_name) → understand structure
```

### 2. Data Integration & ETL
**Automate data pipelines that read/write to FairCom.**

*Example: Sync customer data from SaaS → FairCom using AI-guided transformations.*

```python
# The AI assistant can:
# 1. List available tables (list_tables)
# 2. Inspect target schema (describe_table)
# 3. Execute transformations (sql_execute with confirm_write=true)
# 4. Validate results (sql_query to spot-check)
```

### 3. Operational Analytics
**Real-time status monitoring and anomaly detection.**

*Example: "Show me any orders with payment processing delays."*

```python
# FairCom MCP provides:
# - /metrics → Prometheus-compatible metrics
# - /diagnostics → System health
# - sql_query → Run diagnostic queries
# Combine for full observability loop
```

### 4. Domain-Specific AI Chatbots
**Build internal tools (CRM, inventory, compliance).**

*Example: Chatbot for warehouse staff to check inventory levels, process returns.*

```python
# Sandbox the chatbot with:
# FAIRCOM_TOOL_GROUP_ALLOWLIST=metadata,query
# (write tools disabled for read-only workflows)
#
# FAIRCOM_SQL_DENYLIST=DELETE,DROP
# (prevent destructive operations)
```

## Quick Start (5 Minutes)

### Option 1: Docker (Fastest)

```bash
# Start FairCom MCP pointing to your FairCom instance
docker run -d --name faircom-mcp \
  -p 8000:8000 \
  -e FAIRCOM_API_BASE_URL=http://faircom-host:8080 \
  -e FAIRCOM_API_USERNAME=ADMIN \
  -e FAIRCOM_API_PASSWORD=ADMIN \
  toddstoffel/faircom-mcp:latest --transport http
```

### Option 2: Linux Package (Production)

**Debian/Ubuntu:**
```bash
sudo apt-get install -y ./faircom-mcp_0.1.3_all.deb
sudo systemctl enable --now faircom-mcp
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y ./faircom-mcp-0.1.3-1.noarch.rpm
sudo systemctl enable --now faircom-mcp
```

### Verify it's running:

```bash
# Health check
curl -fsS http://127.0.0.1:8000/health
# Output: {"status":"healthy"}

# List available tables
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1.0"}
    }
  }' | head -20
```

## Tutorial: Query Your First Table

Let's query FairCom using Claude or a local LLM via FairCom MCP.

**Step 1: Initialize MCP Session**

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "my-client", "version": "1.0"}
    }
  }' 2>&1 | grep -i "mcp-session-id"

# Save the session ID from the response, e.g.: abc123
SESSION_ID="abc123"
```

**Step 2: List Tables**

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' 2>&1 | grep -A 5 "list_tables"
```

**Step 3: Describe a Table**

```bash
# Let's examine the "customers" table
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "describe_table",
      "arguments": {"table_name": "customers"}
    }
  }' 2>&1 | tail -20
```

**Step 4: Query Data**

```bash
# Count customers
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": "sql_query",
      "arguments": {
        "statement": "SELECT COUNT(*) as total FROM customers"
      }
    }
  }' 2>&1 | tail -20
```

**Step 5: Configure in Claude/Copilot**

For **Claude Desktop**:
```json
{
  "mcpServers": {
    "faircom": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

For **GitHub Copilot** (VS Code):
```json
{
  "mcpServers": {
    "faircom": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Then ask your AI assistant: *"Show me a count of customers by region"* – it will use FairCom MCP to execute the query.

## Configuration

Edit `/etc/faircom-mcp/faircom-mcp.env` (package install) or pass as environment variables (Docker):

```bash
# Required: FairCom connectivity
FAIRCOM_API_BASE_URL=https://faircom.example.com:9443
FAIRCOM_API_USERNAME=ADMIN           # or use FAIRCOM_API_TOKEN
FAIRCOM_API_PASSWORD=ADMIN

# Optional: Server binding
FAIRCOM_HTTP_HOST=0.0.0.0
FAIRCOM_HTTP_PORT=8000

# Optional: TLS
FAIRCOM_TLS_VERIFY=true              # Set to false for self-signed certs

# Optional: Safety controls
FAIRCOM_TOOL_GROUP_ALLOWLIST=metadata,query,write,admin,diagnostics
FAIRCOM_SQL_ALLOWLIST=SELECT,INSERT,UPDATE,DELETE
FAIRCOM_SQL_DENYLIST=DROP,TRUNCATE,ALTER
```

## Available Tools

| Tool | Purpose | Safety |
|---|---|---|
| `list_tables(name_like?)` | Discover tables | Read-only |
| `describe_table(table_name)` | Get columns, indexes, constraints | Read-only |
| `list_table_columns(table_name)` | Column names and types | Read-only |
| `list_table_indexes(table_name)` | Index details | Read-only |
| `sql_query(statement, params?)` | Execute SELECT (read-only) | Read-only |
| `sql_query_page(statement, params?, page, page_size)` | Paginated SELECT | Read-only |
| `sql_execute(statement, params?, confirm_write)` | INSERT/UPDATE/DELETE (requires `confirm_write=true`) | Write |
| `runtime_status()` | Health, version, diagnostics | Read-only |

## Observability & Operations

### Health Endpoints

```bash
GET  /health       # Simple health check (JSON)
GET  /healthz      # Kubernetes-style liveness
GET  /ready        # Readiness check (JSON)
GET  /readyz       # Kubernetes-style readiness
GET  /metrics      # Prometheus-compatible metrics
GET  /diagnostics  # Human-readable diagnostics
GET  /diagnostics/json  # Machine-readable diagnostics
```

### Logs

Package install:
```bash
journalctl -u faircom-mcp -f       # Follow logs
journalctl -u faircom-mcp --since 1h # Last hour
```

Docker:
```bash
docker logs -f faircom-mcp
```

### Log Rotation

Package install includes logrotate policy:
```bash
/var/log/faircom-mcp/faircom-mcp.log {
  daily
  rotate 7
  compress
  delaycompress
  notifempty
  missingok
}
```

## Development

See [BUILD.md](BUILD.md) for building, testing, and releasing.

## Community

- **Issues**: [GitHub Issues](https://github.com/toddstoffel/faircom-mcp/issues)
- **Discussions**: [GitHub Discussions](https://github.com/toddstoffel/faircom-mcp/discussions)
- **Contributing**: See [CONTRIBUTING.md](CONTRIBUTING.md) (coming soon)

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for terms.

## Support

For FairCom-specific questions: https://www.faircom.com/support  
For MCP integration issues: Open a GitHub issue

---

**Built for the FairCom community.** Query with confidence. Automate with safety.
