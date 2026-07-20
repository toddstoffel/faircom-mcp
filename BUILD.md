# Build And Release Guide

This document is for maintainers who build, test, and package FairCom MCP.
End-user install and configuration instructions are in `README.md`.

## Maintainer Prerequisites
- Python 3.11+
- Docker (required on macOS for Linux-valid package builds)
- Ruby `fpm` (Ruby package manager implementation, not Fortran `fpm`)

## Development Setup
Install package + dev dependencies into local system/user Python:

```bash
python3 -m pip install --user -e '.[dev]'
```

Ensure user-level scripts are on `PATH`:

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

## Quality Gates
```bash
make format
make lint
make typecheck
make test
make test-edge
```

## Container Runtime (Local)
Build and run with Docker:

```bash
make container-build
make container-run
```

Or with Compose:

```bash
make compose-up
make compose-down
```

## Package Build And Validation
Validate packaging tree consistency:

```bash
make package-verify
```

Build installable Linux packages (`.deb`, `.rpm`):

```bash
make package-build
```

Validate package install/uninstall lifecycle in distro containers:

```bash
make package-validate
```

Generated artifacts:
- `dist/packages/faircom-mcp_<version>_all.deb`
- `dist/packages/faircom-mcp-<version>-1.noarch.rpm`

## Release Integrity Artifacts
Generate CycloneDX SBOM + SHA256 checksums:

```bash
make release-integrity
```

Output:
- `dist/sbom.cdx.json`
- `dist/SHA256SUMS`

## Installing Ruby fpm
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
