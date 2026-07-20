# DEB Packaging

This directory contains Debian package metadata and maintainer scripts used by
distribution-specific build tooling.

## Included Files
- `control`: package metadata and runtime dependencies
- `postinst`: creates service user/runtime directories and reloads systemd
- `prerm`: stops service before package removal
- `postrm`: reloads systemd daemon state after removal

## Operational Expectations
- Service unit installed to `/lib/systemd/system/faircom-mcp.service`
- Env file installed to `/etc/faircom-mcp/faircom-mcp.env`
- Log rotation policy installed to `/etc/logrotate.d/faircom-mcp`
- sysusers/tmpfiles entries installed under `/usr/lib/*`

Use `make package-verify` to validate that expected packaging source files are
present in the repository.
