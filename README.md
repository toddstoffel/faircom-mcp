# FairCom MCP Server

> [!IMPORTANT]
> Developers and maintainers: use [BUILD.md](BUILD.md) for build, packaging, and release instructions. This README is product and usage focused.

Connect AI assistants and LLMs to FairCom databases with explicit write controls, Linux packaging, and operational tooling.

> Current release: v${PROJECT_VERSION}. The install examples and release automation in this repository are aligned to this version.

Set the release version once per shell session so the examples stay aligned with the package source of truth:

```bash
PROJECT_VERSION="$(make version)"
```

```
┌─────────────────────────────────────────────────────────────┐
│  Your AI Assistant (Claude, Copilot, etc.)                  │
└────────────────────┬────────────────────────────────────────┘
                     │ MCP Protocol
                     │ (HTTP + JSON-RPC)
┌────────────────────▼────────────────────────────────────────┐
│  FairCom MCP Server                                         │
│  • Session management                                       │
│  • Write safety enforcement (confirm_write=true)            │
│  • Tool exposure control                                    │
│  • Rate limiting, observability                             │
└────────────────────┬────────────────────────────────────────┘
                     │ FairCom JSON API
                     │ (HTTP REST)
┌────────────────────▼────────────────────────────────────────┐
│  FairCom Database                                           │
│  (Edge, DB, RTG, ISAM, MQ)                                  │
└─────────────────────────────────────────────────────────────┘
```

**Why FairCom MCP?**

- **Open source**: Apache 2.0
- **Operationally ready**: systemd service, log rotation, health checks
- **Safe by default**: explicit write confirmation and tool allowlisting
- **Broad compatibility**: works with Edge, DB, RTG, ISAM, and MQ
- **MCP-focused**: intended for Claude, Copilot, and local LLM workflows

## Safe Write Workflow

Use the write controls to make destructive operations predictable and reviewable.

1. Start with a read-only query to confirm the target data.
2. Preview writes with `dry_run=True` before applying anything.
3. Review the preview output, especially the scoped `WHERE` clause and row impact.
4. Apply the change only with `confirm_write=True` and `dry_run=False`.
5. Check the audit trail and metrics endpoints after execution.

```python
# Preview a deletion without changing data
preview = faircom_mcp.sql_execute(
    "DELETE FROM staging_orders WHERE created_at < '2026-01-01'",
    dry_run=True,
)

if preview["would_succeed"]:
    # Only after review, run the real write
    faircom_mcp.sql_execute(
        "DELETE FROM staging_orders WHERE created_at < '2026-01-01'",
        confirm_write=True,
        dry_run=False,
    )
```

For production use, prefer an `operator` or `admin` policy bundle and keep dry-runs in the loop for high-risk statements such as `DELETE`, `UPDATE`, or `DROP`.

## Use Cases

### 1. Business Intelligence & Reporting
**Let users ask natural-language questions about FairCom data.**

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
  faircomteam/faircom-mcp:latest --transport http
```

If FairCom is running on your local host machine, use:

```bash
-e FAIRCOM_API_BASE_URL=http://host.docker.internal:8080
```

### Option 2: Linux Package (Production)

**Debian/Ubuntu:**
```bash
sudo apt-get install -y "./faircom-mcp_${PROJECT_VERSION}_all.deb"
sudo systemctl enable --now faircom-mcp
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y "./faircom-mcp-${PROJECT_VERSION}-1.noarch.rpm"
sudo systemctl enable --now faircom-mcp
```

### Verify it's running:

```bash
# Health check
curl -fsS http://127.0.0.1:8000/health
# Output: {"status":"ok"}

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

## Docker Hub Usage

The official image repository is:

- `faircomteam/faircom-mcp`

Recommended tag usage:

- `latest`: Recommended default tag for standard users
- `v*` tags (for example `vX.Y.Z`): Immutable release tags for production pinning

Pull examples:

```bash
# Default current image
docker pull faircomteam/faircom-mcp:latest

# Pin to an immutable release for production
docker pull faircomteam/faircom-mcp:vX.Y.Z
```

Run example (recommended default):

```bash
docker run -d --name faircom-mcp \
  -p 8000:8000 \
  -e FAIRCOM_API_BASE_URL=http://faircom-host:8080 \
  -e FAIRCOM_API_USERNAME=ADMIN \
  -e FAIRCOM_API_PASSWORD=ADMIN \
  faircomteam/faircom-mcp:latest --transport http
```

Notes:

- Use `latest` for normal usage and quick evaluation.
- Use release tag pins (`v*`) only when you need immutable version locking.
- `latest` and `v*` tags are published together from the same release tag workflow.

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

## Connector Management

FairCom MCP exposes connector inspection and lifecycle operations for FairCom Edge input and output connectors.

Read-oriented connector tools:

- `list_inputs(payload?)`
- `describe_inputs(payload?)`
- `list_outputs(payload?)`
- `describe_outputs(payload?)`

CamelCase parity aliases are also available for API-name alignment:

- `listInputs(payload?)`
- `describeInputs(payload?)`
- `listOutputs(payload?)`
- `describeOutputs(payload?)`

Write-oriented connector tools:

- `create_input(payload, confirm_write=False, dry_run=False)`
- `alter_input(payload, confirm_write=False, dry_run=False)`
- `delete_input(payload, confirm_write=False, dry_run=False)`
- `create_output(payload, confirm_write=False, dry_run=False)`
- `alter_output(payload, confirm_write=False, dry_run=False)`
- `delete_output(payload, confirm_write=False, dry_run=False)`

CamelCase parity aliases are also available for write operations:

- `createInput(payload, confirm_write=False, dry_run=False)`
- `alterInput(payload, confirm_write=False, dry_run=False)`
- `deleteInput(payload, confirm_write=False, dry_run=False)`
- `createOutput(payload, confirm_write=False, dry_run=False)`
- `alterOutput(payload, confirm_write=False, dry_run=False)`
- `deleteOutput(payload, confirm_write=False, dry_run=False)`

Connector writes follow the same explicit safety model as SQL writes:

1. Use `dry_run=True` first to preview the intended connector change.
2. Review the returned action and target payload.
3. Re-run with `confirm_write=True` to apply the change.

Example preview:

```json
{
  "name": "create_output",
  "arguments": {
    "payload": {
      "connectorName": "mqtt_1",
      "type": "output"
    },
    "dry_run": true
  }
}
```

Example apply:

```json
{
  "name": "create_output",
  "arguments": {
    "payload": {
      "connectorName": "mqtt_1",
      "type": "output"
    },
    "confirm_write": true
  }
}
```

The server does not auto-discover device register maps or connector-specific address models. Supply the connector payload details required by the FairCom Edge configuration API for the connector family you are managing.

## FairCom JSON API Surface

FairCom's JSON API is split into three separate namespaces, selected by the `api` field on every request:

- `db` — SQL query/execute and table metadata (`sql_query`, `sql_query_page`, `sql_execute`, table tools).
- `hub` — Edge connector lifecycle (`createInput`/`createOutput` and friends), plus integration tables and their `transformSteps`.
- `admin` — code packages, accounts, and other server administration actions.

There is no single unified endpoint that covers all three — for example, a JavaScript transform is not one object. It is a code package registered through `admin` and then attached to an integration table's `transformSteps` through `hub`. FairCom MCP routes each tool call to the correct namespace and payload shape automatically so you don't need to track this split yourself, but if you see an upstream error referencing an `api` value, this is why.

FAIRCOM_TLS_VERIFY=true              # Set to false for self-signed certs

# Optional: Safety controls
FAIRCOM_POLICY_PRESET=default   # default, read_only, analyst, operator, admin
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
| `sql_execute(statement, params?, confirm_write, dry_run)` | INSERT/UPDATE/DELETE (requires `confirm_write=true` unless `dry_run=true`) | Write |
| `list_services(payload?)` | List Edge connector services and runtime state | Read-only |
| `manage_service(payload, confirm_write, dry_run)` | Start/stop/restart a connector service | Write |
| `describe_connector_schema(payload?)` | Local payload schema profiles and known-good examples per connector service | Read-only |
| `validate_connector_payloads(payload)` | Preflight-validate connector payloads without mutating backend state | Read-only |
| `get_usage_contract()` | Canonical args, aliases, transport/session guidance, examples | Read-only |
| `runtime_status()` | Health, version, diagnostics | Read-only |
| `capabilities_summary()` | Discover enabled tool groups and policy preset | Read-only |
| `observability_metrics()` | Snapshot of internal runtime metrics | Read-only |
| `observability_audit()` | Snapshot of the write/audit event log | Read-only |
| `observability_health()` | Readiness/liveness state as an MCP tool call | Read-only |

See [Connector Management](#connector-management) for input/output connector tools, and [Integration Tables & Code Packages](#integration-tables--code-packages) for transform pipeline tools.

## Integration Tables & Code Packages

Integration tables capture data landed by an input connector and apply `transformSteps` to it. A transform step's JavaScript logic lives in a separately registered code package; a table then references it by `codeName`. There is no single "transform" object — FairCom splits this across the `hub` API (integration tables) and the `admin` API (code packages), and FairCom MCP routes each tool call to the correct one for you.

Read-oriented tools:

- `list_integration_tables(payload?)` — list integration tables visible to the configured access context.
- `describe_integration_tables(payload)` — describe tables including their `fields` and `transformSteps`. Pass a `tables` array, not a bare `tableName`.
- `list_code_packages(payload?)` — list registered code package names for a database/owner.
- `describe_code_packages(payload)` — describe registered code packages, including source code.

Write-oriented tools (same `dry_run` / `confirm_write` safety model as SQL and connector writes):

- `create_integration_table(payload, confirm_write, dry_run)` — create a table, optionally with `fields` and `transformSteps` in the same call.
- `alter_integration_table(payload, confirm_write, dry_run)` — alter a table's fields, transform steps, or retention policy. The server polls `describe_integration_tables` after the write and reports `mutation_applied` / `mutation_verification` in the response, because FairCom can return success while silently not applying some field or transform-step changes.
- `delete_integration_tables(payload, confirm_write, dry_run)`
- `register_code_package(payload, confirm_write, dry_run)` — create or update a code package (`createCodePackage`/`alterCodePackage`).
- `clone_code_package(payload, confirm_write, dry_run)` — clone an existing code package under a new name.
- `revert_code_package(payload, confirm_write, dry_run)` — revert a code package to a prior version. There is no delete; re-registering the same `code_name` is how you update it.
- `test_integration_table_transform_steps(payload, confirm_write, dry_run)` — dry-run transform steps against a table. `payload.testTransformScope` is required and validated against the known enum (`allRecords`, `stop`, `firstRecord`, `lastRecord`, `specificRecords`) since FairCom's own error does not list valid values.

Important, field-tested gotchas:

- Declare every target field in `create_integration_table`'s `fields` array up front. Neither the transform nor `alter_integration_table` can reliably add fields to an existing table afterward.
- Put `databaseName` and `ownerName` inside each transform step object, not only at the table's top level, or FairCom rejects the step with a missing-default-database error.
- A `transformStepMethod` of `"javascript"` requires `transformStepService: "v8TransformService"` alongside it.

## Common AI Client Mistakes (And Fixes)

These are the most common payload issues across Claude, Copilot, ChatGPT, Gemini, and custom agents.

### 1) Wrong key for `sql_query`

Wrong:
```json
{"name":"sql_query","arguments":{"sql":"SELECT COUNT(*) FROM demo_assets"}}
```

Correct canonical form:
```json
{"name":"sql_query","arguments":{"statement":"SELECT COUNT(*) FROM demo_assets"}}
```

Notes:
- The server accepts aliases `sql` and `query`, normalizes to `statement`, and returns normalization metadata.
- The server also normalizes `SELECT FIRST N ...` to `SELECT TOP N ...` for FairCom compatibility.

### 2) Wrong key for table metadata tools

Wrong:
```json
{"name":"describe_table","arguments":{"table":"demo_assets"}}
```

Correct canonical form:
```json
{"name":"describe_table","arguments":{"table_name":"demo_assets"}}
```

Notes:
- The server accepts `table` alias and normalizes to `table_name`.

### 3) list_tables filtering key mismatch

Wrong:
```json
{"name":"list_tables","arguments":{"table_like":"demo_%"}}
```

Correct canonical form:
```json
{"name":"list_tables","arguments":{"name_like":"demo_%"}}
```

Notes:
- `table_like` is accepted as an alias and normalized to `name_like`.
- `database` is accepted for compatibility; current adapter may ignore backend scoping and reports that explicitly.

### 4) SQL dialect mismatch (`LIMIT/OFFSET/FETCH`)

Risky for this backend:
```sql
SELECT * FROM demo_assets ORDER BY id DESC LIMIT 25 OFFSET 10
```

Preferred FairCom-compatible style:
```sql
SELECT SKIP 10 TOP 25 * FROM demo_assets ORDER BY id DESC
```

Notes:
- The server returns a structured validation error with `suggested_fix` and `example_payload` for unsupported SQL feature patterns.
- Unsupported SQL tokens are reported explicitly in `unsupported_sql_feature` (for example: `LIMIT`, `OFFSET`, `FETCH`).

## Session Recovery Quick Fix

If you receive a missing/stale session error:

1. Call `initialize` again.
2. Capture the new `Mcp-Session-Id`.
3. Retry the failed `tools/call` request with the new session id.

Tip:
- Call `get_usage_contract` once at startup to load canonical argument keys and aliases.
- A versioned contract snapshot is also published at `docs/mcp-usage-contract.v2026-07-28.json`.

## JSON Mode vs SSE Mode

- `--transport http`: best for JSON-only clients.
- `--transport sse`: best for clients that parse `text/event-stream` framing.
- `--transport stdio`: local process transport for MCP hosts.

If your parser is brittle against SSE envelopes, run the server in HTTP mode and keep request/response handling strictly JSON.

## Official JSON-RPC Helper Clients

Reference helper clients are available for strict JSON-RPC integrations:

- `examples/clients/python/mcp_http_helper.py`
- `examples/clients/javascript/mcpHttpHelper.mjs`

These helpers implement the compatibility workflow used by this server:

- initialize session before tool calls
- reuse `Mcp-Session-Id`
- force `Accept: application/json` for deterministic JSON mode
- reinitialize once and retry when `reason_code` indicates `missing_session` or `stale_session`
- preview writes using `sql_execute` with `dry_run=true`

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
