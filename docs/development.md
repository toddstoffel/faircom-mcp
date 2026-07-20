# Development

## Prerequisites
- Python 3.11+
- pip

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .[dev]
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

Install package build prerequisite (`fpm`):

macOS:

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

Build packages:

```bash
make package-build
```

## Boundary Rule
Keep transports, FairCom API adapters, tool handlers, and security policy as separate modules.
