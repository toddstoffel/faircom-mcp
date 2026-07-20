# Testing

## Test Layers
- Unit tests in `tests/unit`
- Integration tests in `tests/integration`

## Run tests
```bash
make test
```

## Integration tests only
```bash
make test-integration
```

## Transport smoke validation
HTTP transport:

```bash
faircom-mcp-server --transport http
curl -fsS http://127.0.0.1:8000/health
```

STDIO transport:

```bash
faircom-mcp-server --transport stdio
```

## FairCom Edge Docker-backed tests
Default backend image: https://hub.docker.com/r/faircomteam/edge

```bash
make test-edge
```

Optional overrides:
- `FAIRCOM_EDGE_IMAGE` (default: `faircomteam/edge`)
- `FAIRCOM_EDGE_SCHEME` (default: `http`)
- `FAIRCOM_EDGE_HOST_PORT` (default: `8080`)
- `FAIRCOM_EDGE_CONTAINER_PORT` (default: `8080`)
- `FAIRCOM_EDGE_STARTUP_TIMEOUT` seconds (default: `120`)
- `FAIRCOM_EDGE_RUN_ARGS` for extra `docker run` args

## Coverage
```bash
make test-cov
```

## Linux package lifecycle validation
Requires built package artifacts under `dist/packages`.

```bash
make package-validate
```

This validates install/uninstall checks for DEB and RPM artifacts in distro
containers, including systemd unit verification and logrotate config parsing.

## Current Baseline
- Import smoke for package modules
- Minimal integration smoke
- Docker-backed FairCom Edge smoke path for runtime validation
