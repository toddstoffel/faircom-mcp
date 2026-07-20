# FairCom MCP Server

Production-grade MCP server for FairCom JSON API with enterprise Linux operational standards.

## Who This Is For
This README is for operators installing the server as a Linux package (`.deb` or `.rpm`) and connecting it to an existing FairCom deployment.

Build and release workflows for maintainers are documented in `BUILD.md`.

## Install The Package
Install from your distributed artifact in `dist/packages/`.

Debian/Ubuntu:

```bash
sudo apt-get install -y ./faircom-mcp_<version>_all.deb
```

RHEL/Rocky/Alma/Fedora:

```bash
sudo dnf install -y ./faircom-mcp-<version>-1.noarch.rpm
```

The package installs:
- systemd service: `/usr/lib/systemd/system/faircom-mcp.service`
- environment file: `/etc/faircom-mcp/faircom-mcp.env`
- logrotate policy: `/etc/logrotate.d/faircom-mcp`

## Configure For Your FairCom Deployment
Edit `/etc/faircom-mcp/faircom-mcp.env` and set your FairCom API endpoint and authentication.

Required settings:
- `FAIRCOM_API_BASE_URL`: Base URL for your FairCom JSON API, for example `https://faircom.example.com:9443`
- authentication: either
    - `FAIRCOM_API_TOKEN`, or
    - both `FAIRCOM_API_USERNAME` and `FAIRCOM_API_PASSWORD`

Recommended baseline:

```bash
FAIRCOM_API_BASE_URL=https://faircom.example.com:9443
FAIRCOM_API_TOKEN=replace-with-api-token
FAIRCOM_HTTP_HOST=0.0.0.0
FAIRCOM_HTTP_PORT=8000
FAIRCOM_TLS_VERIFY=true
```

If your FairCom endpoint uses an internal/self-signed certificate, temporarily disable TLS verification:

```bash
FAIRCOM_TLS_VERIFY=false
```

Optional SQL write guardrails:

```bash
FAIRCOM_SQL_ALLOWLIST=
FAIRCOM_SQL_DENYLIST=
```

Optional tool-group allowlist:

```bash
FAIRCOM_TOOL_GROUP_ALLOWLIST=metadata,query,write,admin,diagnostics
```

Optional diagnostics and observability:

```bash
FAIRCOM_ENABLE_DIAGNOSTICS_UI=true
FAIRCOM_DIAGNOSTICS_TOKEN=replace-with-strong-token
FAIRCOM_ENABLE_METRICS=true
FAIRCOM_ENABLE_TRACING=false
```

## Start The Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now faircom-mcp.service
sudo systemctl status faircom-mcp.service --no-pager
```

After editing config, restart:

```bash
sudo systemctl restart faircom-mcp.service
```

## Validate Installation
Health checks:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Compatibility aliases are also available at `/healthz` and `/readyz`.

Metrics endpoint (enabled by default):

```bash
curl -fsS http://127.0.0.1:8000/metrics
```

Diagnostics endpoints (only when enabled and token configured):

```text
GET /diagnostics
GET /diagnostics/json
```

Pass diagnostics token using header `x-diagnostics-token` or query parameter `token`.

## API Endpoints
HTTP service endpoints exposed by FairCom MCP:

```text
GET  /health
GET  /healthz
GET  /ready
GET  /readyz
GET  /metrics
GET  /diagnostics
GET  /diagnostics/json
POST /mcp
```

Use `/mcp` for MCP JSON-RPC over HTTP. The server responds as SSE (`text/event-stream`) with JSON-RPC payload in `event: message` frames.

### MCP HTTP Protocol Usage
Required request headers for MCP calls:

```text
Accept: application/json, text/event-stream
Content-Type: application/json
```

Session behavior:
- `initialize` response includes `mcp-session-id` header
- send `Mcp-Session-Id: <value>` on subsequent calls (`tools/list`, `tools/call`, etc.)

Example initialize request:

```bash
curl -sS -X POST http://127.0.0.1:8000/mcp \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    --data '{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "example-client", "version": "1.0"}
        }
    }'
```

Example tool invocation (`runtime_status`) after initialization:

```bash
curl -sS -X POST http://127.0.0.1:8000/mcp \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    -H 'Mcp-Session-Id: <session-id-from-initialize>' \
    --data '{
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "runtime_status",
            "arguments": {}
        }
    }'
```

## Available MCP Tools
Core SQL and metadata tools:

```text
list_tables(table_name?)
describe_table(table_name)
list_table_columns(table_name)
list_table_indexes(table_name)
sql_query(statement, params?)
sql_query_page(statement, params?, page, page_size)
sql_execute(statement, params?)
runtime_status()
```

Paginated query responses include deterministic iteration metadata:

```text
has_more: <bool>
next_page: <int|null>
next_cursor: <int|null>
```

Supported tool groups:
- `metadata`: table and schema metadata tools
- `query`: read-only SQL query tools
- `write`: mutating SQL execute tools
- `admin`: runtime inspection tools
- `diagnostics`: diagnostics endpoints

If a blocked group is invoked, the server returns a validation error with policy details.

## Operations Docs
- `docs/operations-runbook.md`: service lifecycle, startup probe, logs, and troubleshooting
- `docs/release-notes-template.md`: release notes skeleton for tagged releases
- `docs/support-matrix.md`: validated Linux distribution and package support targets
- `docs/testing.md`: test layers and FairCom Edge runtime validation

## Notes
- This project uses direct FairCom JSON API integration and does not depend on FairCom CLI tools.
