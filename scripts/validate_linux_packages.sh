#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for package lifecycle validation" >&2
  exit 1
fi

DEB_PACKAGE="$(ls -1 dist/packages/*.deb 2>/dev/null | head -n 1 || true)"
RPM_PACKAGE="$(ls -1 dist/packages/*.rpm 2>/dev/null | head -n 1 || true)"

if [[ -z "${DEB_PACKAGE}" ]]; then
  echo "No DEB package found under dist/packages" >&2
  exit 1
fi

if [[ -z "${RPM_PACKAGE}" ]]; then
  echo "No RPM package found under dist/packages" >&2
  exit 1
fi

echo "Validating DEB install/uninstall lifecycle in ubuntu:24.04 ..."
docker run --rm -v "${ROOT_DIR}/dist/packages:/packages:ro" ubuntu:24.04 bash -lc '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates python3 python3-pip adduser systemd logrotate
  apt-get install -y /packages/*.deb
  command -v faircom-mcp-server >/dev/null
  test -f /lib/systemd/system/faircom-mcp.service
  test -f /etc/faircom-mcp/faircom-mcp.env
  test -f /etc/logrotate.d/faircom-mcp
  systemd-analyze verify /lib/systemd/system/faircom-mcp.service
  logrotate -d /etc/logrotate.d/faircom-mcp >/dev/null
  dpkg -r faircom-mcp
  ! dpkg -s faircom-mcp 2>/dev/null | grep -q "^Status: install ok installed"
'

echo "Validating RPM install/uninstall lifecycle in fedora:41 ..."
docker run --rm -v "${ROOT_DIR}/dist/packages:/packages:ro" fedora:41 bash -lc '
  set -euo pipefail
  dnf -y install ca-certificates python3 python3-pip systemd shadow-utils logrotate
  dnf -y install /packages/*.rpm --nogpgcheck
  command -v faircom-mcp-server >/dev/null
  test -f /usr/lib/systemd/system/faircom-mcp.service
  test -f /etc/faircom-mcp/faircom-mcp.env
  test -f /etc/logrotate.d/faircom-mcp
  systemd-analyze verify /usr/lib/systemd/system/faircom-mcp.service
  logrotate -d /etc/logrotate.d/faircom-mcp >/dev/null
  dnf -y remove faircom-mcp
  ! rpm -q faircom-mcp >/dev/null 2>&1
'

echo "Package lifecycle validation succeeded for DEB and RPM artifacts."
