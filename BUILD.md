# Build And Release Guide

This document is for maintainers who build, test, and package FairCom MCP.
End-user install and configuration instructions are in `README.md`.

## Maintainer Prerequisites
- Python 3.11+
- Docker (required for reproducible package builds on any host)
- Ruby `fpm` only when using native package builds (optional, advanced)

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

## Container Image Build Targets
The Dockerfile has separate stages for package building and runtime images.

Build Debian runtime image (installs generated `.deb` package):

```bash
docker build --target runtime-deb -t faircom-mcp:deb .
```

Build RPM runtime image (installs generated `.rpm` package):

```bash
docker build --target runtime-rpm -t faircom-mcp:rpm .
```

Default target is Debian runtime:

```bash
docker build -t faircom-mcp:local .
```

Notes:
- `package-builder` stage builds Debian package artifacts
- `package-builder-rpm` stage builds RPM package artifacts
- runtime stages install those package artifacts to mirror customer installs

## Package Build And Validation
Validate packaging tree consistency:

```bash
make package-verify
```

Build installable Linux packages (`.deb`, `.rpm`):

```bash
make package-build
```

Default package build behavior is reproducible and host-agnostic:
- `make package-build` runs package creation in a Linux builder container by default
- Default builder image: `debian:stable-slim`
- Works the same across hosts as long as Docker is available

Build mode controls:
- `PACKAGE_BUILD_MODE=container` forces containerized build
- `PACKAGE_BUILD_MODE=native` uses host tooling (`python3`, `rpm`, Ruby `fpm`)
- `PACKAGE_BUILD_MODE=auto` (default) chooses container when Docker is available

Examples:

```bash
# Recommended reproducible path
PACKAGE_BUILD_MODE=container make package-build

# Native host build (advanced)
PACKAGE_BUILD_MODE=native make package-build
```

Override builder image (for controlled toolchain updates):

```bash
PACKAGE_BUILDER_IMAGE=debian:stable-slim make package-build
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

## CI/CD Workflow Behavior
This repository uses two separate GitHub Actions workflows with different purposes.

### CI Workflow
File: `.github/workflows/ci.yml`

Trigger:
- Every push
- Every pull request

Purpose:
- Run quality gates
- Smoke-test container runtime paths (`Dockerfile` and `docker-compose.yml`)
- Build and validate Linux packages in a clean runner
- Produce run artifacts for verification and handoff

Package build mode in CI:
- CI pins `PACKAGE_BUILD_MODE=native` in Linux runners for speed
- Reproducibility for local maintainer builds still defaults to containerized mode

Container/compose coverage in CI:
- Build container image with `docker build`
- Start container and verify `/health`
- Start compose stack and verify `/health`
- Always tear down containers/compose services after smoke tests

What CI uploads:
- `dist/packages/*.deb`
- `dist/packages/*.rpm`
- `dist/sbom.cdx.json`
- `dist/SHA256SUMS`

Where to find CI package outputs:
- GitHub Actions run page
- Artifacts section
- Artifact name format: `faircom-mcp-packages-<commit-sha>`

Important:
- CI artifacts are Actions run artifacts, not GitHub Release assets.

### Release Workflow
File: `.github/workflows/release.yml`

Trigger:
- Tag push matching `v*` (for example: `v0.1.3`)

Purpose:
- Re-run quality and packaging steps for the tagged commit
- Publish assets to the GitHub Release page

Package build mode in Release:
- Release pins `PACKAGE_BUILD_MODE=native` in Linux runners
- This avoids nested Docker while still producing Linux-native artifacts

What Release publishes:
- `dist/packages/*.deb`
- `dist/packages/*.rpm`
- `dist/*.whl`
- `dist/*.tar.gz`
- `dist/sbom.cdx.json`
- `dist/SHA256SUMS`

Important:
- A successful CI run on `main` does not publish new GitHub Release assets.
- Release assets are only published by the tag-triggered Release workflow.

## Maintainer Release Steps
After validating changes on `main`:

1. Ensure local branch is current.
2. Create a new semantic version tag.
3. Push the tag to origin.
4. Watch the Release workflow complete.
5. Verify assets on the GitHub Releases page.

Example:

```bash
git checkout main
git pull --ff-only origin main
git tag v0.1.3
git push origin v0.1.3
```

## Troubleshooting CI/CD Visibility
Symptom: CI is green but no new files appear under GitHub Releases.

Cause:
- Only CI workflow ran.
- No new `v*` tag was pushed.

Fix:
- Create and push a new release tag.
- Confirm the Release workflow (not only CI) completed successfully.

Symptom: Release run exists but assets are missing.

Checks:
- Confirm tag matches `v*` pattern.
- Confirm Release workflow succeeded through "Publish release assets" step.
- Confirm `dist/packages/*.deb` and `dist/packages/*.rpm` existed earlier in the run.

## Installing Ruby fpm
Only needed for `PACKAGE_BUILD_MODE=native`.

Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y rpm ruby ruby-dev build-essential
sudo gem install --no-document fpm
```
