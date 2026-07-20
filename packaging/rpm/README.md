# RPM Packaging

This directory contains the RPM spec used to install service and operational
artifacts for FairCom MCP.

## Included Files
- `faircom-mcp.spec`: package metadata and file layout

## Spec Responsibilities
- Installs systemd unit and default environment file
- Installs logrotate configuration
- Installs sysusers and tmpfiles definitions
- Executes post-install daemon reload and provisioning hooks

The spec intentionally keeps Python application build/install logic decoupled
from Linux service artifact wiring so transport/API code can evolve without
rewriting operational packaging.
