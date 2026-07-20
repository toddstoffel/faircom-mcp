# Operations Runbook

This runbook covers Linux service installation, startup checks, and common troubleshooting for FairCom MCP.

## Prerequisites
- FairCom backend reachable from host.
- Environment file configured at `/etc/faircom-mcp/faircom-mcp.env`.
- Service installed as `faircom-mcp.service`.

## Service Lifecycle
Start and enable at boot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now faircom-mcp.service
```

Check status:

```bash
systemctl status faircom-mcp.service --no-pager
```

Restart after config changes:

```bash
sudo systemctl restart faircom-mcp.service
```

Stop service:

```bash
sudo systemctl stop faircom-mcp.service
```

## Startup Probe
Default probe endpoint:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Expected behavior:
- Exit code `0` with JSON payload when healthy.
- Non-zero exit code when service is unavailable.

## Runtime Logs
Journald stream:

```bash
journalctl -u faircom-mcp.service -f
```

Last 200 lines:

```bash
journalctl -u faircom-mcp.service -n 200 --no-pager
```

## Log Rotation
Application file logs (if enabled) rotate using:
- `/etc/logrotate.d/faircom-mcp`

Manual dry-run:

```bash
sudo logrotate -d /etc/logrotate.d/faircom-mcp
```

Force a rotation cycle:

```bash
sudo logrotate -f /etc/logrotate.d/faircom-mcp
```

## Common Failure Modes
1. Service fails immediately on start:
- Inspect `journalctl -u faircom-mcp.service -n 200 --no-pager`.
- Verify required env vars in `/etc/faircom-mcp/faircom-mcp.env`.

2. Health endpoint unreachable:
- Confirm bind settings (`FAIRCOM_HTTP_HOST`, `FAIRCOM_HTTP_PORT`).
- Confirm service is running and listening on expected port.

3. Upstream API errors:
- Verify `FAIRCOM_API_BASE_URL` and credentials/token values.
- Check network reachability from service host to FairCom API host.

4. SQL writes denied unexpectedly:
- Inspect SQL allowlist/denylist env settings.
- Confirm mutating operations include required explicit opt-in flags.

## Upgrade and Rollback
Upgrade:
1. Install updated package version.
2. Validate service restart and `/health` probe.
3. Verify tool calls with a read-only smoke query.

Rollback:
1. Reinstall prior known-good package version.
2. Restart service and re-run `/health` probe.
3. Confirm env file compatibility with prior version.
