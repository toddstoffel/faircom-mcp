# FairCom MCP Server

Production-grade MCP server for FairCom JSON API with enterprise Linux operational standards.

## Status
- Phase 0-5 complete.
- Phase 6 complete: container runtime, Linux packaging artifacts, release workflow,
  and package lifecycle validation.

## Development Setup
1. Create and activate a Python 3.11+ virtual environment.
2. Install package + dev dependencies:

```bash
python -m pip install -e .[dev]
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
curl -fsS http://127.0.0.1:8000/health
```

STDIO mode:

```bash
faircom-mcp-server --transport stdio
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
