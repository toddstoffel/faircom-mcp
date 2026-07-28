# Development

## Prerequisites
- Python 3.11+
- pip
- Docker

## Setup
```bash
python3 -m pip install --user -e '.[dev]'
```

Ensure user-level scripts are on `PATH`:

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

## Daily Commands
```bash
make format
make lint
make typecheck
make test
```

## Container Workflow
```bash
make container-build
make container-run
```

Compose alternative:

```bash
make compose-up
make compose-down
```

## Packaging Workflow
Validate Linux packaging source artifacts:

```bash
make package-verify
```

Build packages:

```bash
make package-build
```

Default package builds run in a Linux builder container for reproducibility across machines.
If you need a native build path, set `PACKAGE_BUILD_MODE=native` and install Ruby `fpm`.

## Error taxonomy and remediation
Use the structured error model consistently so client code can react predictably to failures.

| Error code | Category | Typical cause | Remediation hint |
| --- | --- | --- | --- |
| `validation_error` | validation | Invalid input or malformed arguments | Review the input values and try again. |
| `policy_violation` | authorization | Policy denies the requested write or privileged action | Adjust the policy or request a role with the required access. |
| `upstream_api_error` | upstream_failure | FairCom API is unavailable, timed out, or rejected the request | Retry with backoff if appropriate and inspect upstream service health. |
| `transport_error` | transport | Connection or routing problem between the server and FairCom | Check the network connection and service endpoint configuration. |
| `configuration_error` | configuration | Missing or invalid server configuration | Review the server configuration and required environment variables. |
| `internal_error` | internal | Unexpected server-side failure | Retry the request and contact support if the issue persists. |

All errors should be surfaced through the structured payload returned by `to_payload()` so the client can read `code`, `category`, `retryable`, `hint`, and `details` without special-casing exceptions.

## Boundary Rule
Keep transports, FairCom API adapters, tool handlers, and security policy as separate modules.

## Reference docs
- [Error codes and remediation](error-codes.md)
- [Client patterns for FairCom MCP](client-patterns.md)

## Capability discovery and safety workflow
The server now exposes a capabilities_summary tool that returns versioned service metadata, transport availability, policy state, and per-tool risk metadata. Use it to discover supported workflows and to understand which operations are read-only versus write-gated.

For safe writes, prefer the following sequence:
1. Review the target data with a read-only query.
2. Preview the write with dry_run=True.
3. Apply the change only with confirm_write=True and dry_run=False.
4. Check audit and metrics endpoints after execution.

Client integrations should treat retryable errors as candidates for backoff, but non-retryable validation, authorization, and safety-gate errors should fail fast.
