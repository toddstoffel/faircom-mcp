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

## Boundary Rule
Keep transports, FairCom API adapters, tool handlers, and security policy as separate modules.
