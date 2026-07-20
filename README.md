# FairCom MCP Server

Production-grade MCP server for FairCom JSON API.

FairCom MCP lets MCP-compatible clients query and operate FairCom data with explicit safety controls, Linux package deployment, and service-grade operations support.

## Start Here

If you are an operator deploying to Linux hosts:
- Continue in this README.

If you are a maintainer building, packaging, or releasing:
- Use `BUILD.md`.

## Why FairCom MCP

This project is designed for teams that need MCP access to FairCom with production operational discipline.

Key outcomes:
- Connect MCP clients to FairCom metadata and SQL tools over HTTP (`/mcp`).
- Enforce write intent explicitly (`confirm_write=true` for `sql_execute`).
- Control exposed tool groups (metadata/query/write/admin/diagnostics).
- Run as a Linux service with package-managed installation and log rotation.
- Expose health/readiness/metrics for runtime observability.

## Capability Summary

| Area | FairCom MCP |
|---|---|
| Primary purpose | MCP server for FairCom JSON API |
| MCP transport | Streamable HTTP on `POST /mcp` |
| Operational model | Linux package install (`.deb`/`.rpm`) + systemd |
| SQL read tools | `sql_query`, `sql_query_page` |
| SQL write safety | `sql_execute` requires `confirm_write=true` |
| Metadata tools | Tables, columns, indexes |
| Runtime observability | `/health`, `/ready`, `/metrics`, diagnostics endpoints |
| Deployment hardening | systemd unit + environment file + logrotate policy |

## Quickstart (5 Minutes)

Run with Docker against an existing FairCom Edge server:

```bash
docker run -d --name faircom-mcp \
  -p 8000:8000 \
  -e FAIRCOM_API_BASE_URL=http://<faircom-host>:8080 \
  -e FAIRCOM_API_USERNAME=ADMIN \
  -e FAIRCOM_API_PASSWORD=ADMIN \
  faircom-mcp:deb --transport http
```

Validate server health:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Initialize MCP session:

```bash
curl -i -sS -X POST http://127.0.0.1:8000/mcp \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "quickstart-client", "version": "1.0"}
    }
  }'
```

Use returned `mcp-session-id` as `Mcp-Session-Id` for subsequent MCP requests.

## Install From Linux Package

Install from your distributed artifact in `dist/packages/`.

Debian/Ubuntu:

```bash
sudo apt-get install -y ./faircom-mcp_<version>_all.deb
```

RHEL/Rocky/Alma/Fedora:

```bash
sudo dnf install -y ./faircom-mcp-<version>-1.noarch.rpm
```

Package installs:
- systemd service: `/usr/lib/systemd/system/faircom-mcp.service`
- environment file: `/etc/faircom-mcp/faircom-mcp.env`
- logrotate policy: `/etc/logrotate.d/faircom-mcp`

## Configure FairCom Connectivity

Edit `/etc/faircom-mcp/faircom-mcp.env`.

Required:
- `FAIRCOM_API_BASE_URL`
- one authentication mode:
  - `FAIRCOM_API_USERNAME` + `FAIRCOM_API_PASSWORD`, or
  - `FAIRCOM_API_TOKEN`

Recommended baseline:

```bash
FAIRCOM_API_BASE_URL=https://faircom.example.com:9443
FAIRCOM_API_USERNAME=ADMIN
FAIRCOM_API_PASSWORD=ADMIN
FAIRCOM_HTTP_HOST=0.0.0.0
FAIRCOM_HTTP_PORT=8000
FAIRCOM_TLS_VERIFY=true
```

If using internal/self-signed certificates:

```bash
FAIRCOM_TLS_VERIFY=false
```

## Authentication Behavior (Important)

FairCom MCP uses FairCom JSON action session semantics:

- Username/password mode:
  - MCP server creates a FairCom session with `api: "admin"`, action `createSession`.
  - Request shape uses `params.username` and `params.password`.
  - Returned FairCom `authToken` is cached and sent on subsequent DB actions.

- Token mode (`FAIRCOM_API_TOKEN`):
  - Token is sent as FairCom JSON `authToken` in action requests.
  - This token must be a valid FairCom session token for the target server.

- API-level errors:
  - FairCom may return HTTP 200 with non-zero `errorCode`.
  - MCP server treats non-zero `errorCode` as failure and surfaces it as tool error.

## Start Service (Package Install)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now faircom-mcp.service
sudo systemctl status faircom-mcp.service --no-pager
```

After config changes:

```bash
sudo systemctl restart faircom-mcp.service
```

## API Endpoints

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

`/mcp` is MCP JSON-RPC over HTTP (SSE response framing).

Required MCP request headers:

```text
Accept: application/json, text/event-stream
Content-Type: application/json
```

Session behavior:
- `initialize` response includes `mcp-session-id` header.
- Send `Mcp-Session-Id: <value>` on `tools/list`, `tools/call`, etc.

## MCP Client Configuration Examples

### VS Code / GitHub Copilot

Add in user settings JSON or `.vscode/mcp.json`:

```json
{
  "mcpServers": {
    "faircom-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### Claude Code CLI

Add in `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "faircom-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## Available MCP Tools

```text
list_tables(name_like?)
describe_table(table_name)
list_table_columns(table_name)
list_table_indexes(table_name)
sql_query(statement, params?)
sql_query_page(statement, params?, page, page_size)
sql_execute(statement, params?, confirm_write)
runtime_status()
```

Safety and policy behavior:
- `sql_execute` requires `confirm_write=true`; otherwise request is rejected.
- Tool groups can be restricted with `FAIRCOM_TOOL_GROUP_ALLOWLIST`.
- SQL allow/deny policy can be tuned with `FAIRCOM_SQL_ALLOWLIST` and `FAIRCOM_SQL_DENYLIST`.

Paginated query metadata:

```text
has_more: <bool>
next_page: <int|null>
next_cursor: <int|null>
```

## Optional Runtime Controls

Tool-group allowlist:

```bash
FAIRCOM_TOOL_GROUP_ALLOWLIST=metadata,query,write,admin,diagnostics
```

Diagnostics and observability:

```bash
FAIRCOM_ENABLE_DIAGNOSTICS_UI=true
FAIRCOM_DIAGNOSTICS_TOKEN=replace-with-strong-token
FAIRCOM_ENABLE_METRICS=true
FAIRCOM_ENABLE_TRACING=false
```

Diagnostics access:
- `GET /diagnostics`
- `GET /diagnostics/json`
- Provide token in header `x-diagnostics-token` or query `token`.

## Validation Commands

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/metrics
```

## Documentation Map

- `BUILD.md`: build, packaging, CI/CD, and release flow for maintainers
- `docs/operations-runbook.md`: operations lifecycle and troubleshooting
- `docs/support-matrix.md`: validated distro and packaging support targets
- `docs/testing.md`: test strategy and FairCom Edge runtime validation
- `docs/release-notes-template.md`: release note scaffold

## Project Notes

- Integrates directly with FairCom JSON API.
- Does not depend on FairCom CLI tools.
- Supports HTTP MCP transport and package-based Linux deployments.
