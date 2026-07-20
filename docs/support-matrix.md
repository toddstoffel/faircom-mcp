# Linux Support Matrix

This matrix defines the currently validated Linux targets for FairCom MCP package lifecycle checks.

## Scope
- Package install/uninstall validation
- systemd unit verification
- logrotate configuration verification
- Python runtime and entrypoint presence checks

## Current Validation Targets

| Distribution | Version | Package Type | Architecture | Validation Mode | Status |
| --- | --- | --- | --- | --- | --- |
| Ubuntu | 24.04 | DEB | amd64 | CI containerized lifecycle validation | Validated |
| Rocky Linux | 9 | RPM | amd64 | CI containerized lifecycle validation | Validated |

## Validation Command
Run after package build artifacts are present under `dist/packages`:

```bash
make package-validate
```

## Notes
- Validation currently executes in Docker containers and checks package lifecycle behavior.
- Native VM or bare-metal validation is recommended before production rollout in regulated environments.
- Additional distro/architecture combinations should be added here as they are validated.
