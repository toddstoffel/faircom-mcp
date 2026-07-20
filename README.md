# FairCom MCP Server

Production-grade MCP server for FairCom JSON API with enterprise Linux operational standards.

## Development Setup
1. Install package + dev dependencies into local system/user Python:

```bash
python3 -m pip install --user -e '.[dev]'
```

2. Ensure user-level scripts are on `PATH` (needed for `faircom-mcp-server` and `cyclonedx-py`):

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

## Quality Commands
```bash
make format
make lint
make typecheck
make test
make test-edge
```

## Container Runtime
Build and run with Docker:

```bash
make container-build
make container-run
```

Or with compose:

```bash
make compose-up
make compose-down
```

## Transport Validation
HTTP mode:

```bash
faircom-mcp-server --transport http
curl -fsS http://127.0.0.1:8000/healthz
```

STDIO mode:

```bash
faircom-mcp-server --transport stdio
```

## Paginated SQL Queries
Use paginated SQL query mode for large result sets:

```text
tool: sql_query_page
inputs:
    statement: "SELECT * FROM my_table ORDER BY id"
    params: []
    page: 1
    page_size: 500
```

Response includes deterministic iteration metadata:

```text
has_more: <bool>
next_page: <int|null>
next_cursor: <int|null>
```

## Metadata And Admin Tools
The server also exposes metadata and runtime inspection tools:

```text
list_table_columns(table_name)
list_table_indexes(table_name)
runtime_status()
```

## Tool-Group Policy Controls
Restrict tool groups with an allowlist:

```bash
export FAIRCOM_TOOL_GROUP_ALLOWLIST="metadata,query,write,admin,diagnostics"
```

Supported groups:
- `metadata`: table and schema metadata tools
- `query`: read-only SQL query tools
- `write`: mutating SQL execute tools
- `admin`: runtime inspection tools
- `diagnostics`: diagnostics endpoints

If a blocked group is invoked, the server returns a validation error with policy details.

## Metrics And Diagnostics
Prometheus-style metrics are enabled by default at:

```text
GET /metrics
```

Optional diagnostics endpoints:

```bash
export FAIRCOM_ENABLE_DIAGNOSTICS_UI=true
export FAIRCOM_DIAGNOSTICS_TOKEN="replace-with-strong-token"
```

When enabled, diagnostics endpoints require either header `x-diagnostics-token`
or query parameter `token`:

```text
GET /diagnostics
GET /diagnostics/json
```

Optional tracing integration can be enabled with:

```bash
export FAIRCOM_ENABLE_TRACING=true
```

## Packaging Artifacts
Linux service and package metadata live under `packaging/`:
- `packaging/systemd/`: systemd unit and environment template
- `packaging/logrotate/`: log rotation policy
- `packaging/sysusers.d/`: service account provisioning
- `packaging/tmpfiles.d/`: runtime/state directory provisioning
- `packaging/rpm/`: RPM spec and notes
- `packaging/deb/`: DEB control and maintainer scripts

Validate packaging tree consistency:

```bash
make package-verify
```

Build installable Linux packages (requires `fpm`):

```bash
make package-build
```

Validate package install/uninstall lifecycle in distro containers:

```bash
make package-validate
```

Generate release integrity artifacts (CycloneDX SBOM + SHA256 checksums):

```bash
make release-integrity
```

Install `fpm` locally:

macOS (Homebrew):

```bash
brew install rpm ruby
gem install --no-document fpm
```

Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y rpm ruby ruby-dev build-essential
sudo gem install --no-document fpm
```

## Operations Docs
- `docs/operations-runbook.md`: service lifecycle, startup probe, logs, and troubleshooting
- `docs/release-notes-template.md`: release notes skeleton for tagged releases
- `docs/support-matrix.md`: validated Linux distribution and package support targets
- `docs/testing.md`: test layers and FairCom Edge runtime validation

## Release Integrity Artifacts
- `dist/sbom.cdx.json`: CycloneDX SBOM for the release environment
- `dist/SHA256SUMS`: checksums for release artifacts in `dist/`

## Project Structure
- `src/faircom_mcp/transports`: MCP transport adapters (HTTP/SSE/STDIO)
- `src/faircom_mcp/api`: FairCom JSON API client and adapters
- `src/faircom_mcp/tools`: MCP tool handlers and schemas
- `src/faircom_mcp/security`: auth, policy, and write guardrails
- `packaging/`: Linux packaging and service artifacts (RPM/DEB/systemd/logrotate)

## Notes
- `private_notes/` is local-only planning context and ignored by git.
- This project uses direct FairCom JSON API integration and does not depend on FairCom CLI tools.
