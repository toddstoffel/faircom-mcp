#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build DEB/RPM packages" >&2
  exit 1
fi

VERSION="${1:-$(python3 - <<'PY'
import tomllib
from pathlib import Path

pyproject = Path('pyproject.toml')
data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
print(data['project']['version'])
PY
)}"

OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/dist/packages}"
BUILD_DIR="${BUILD_DIR:-${ROOT_DIR}/build/packages}"
STAGE_DIR="${BUILD_DIR}/stage"

rm -rf "${BUILD_DIR}"
mkdir -p "${STAGE_DIR}" "${OUTPUT_DIR}"

python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install . --root "${STAGE_DIR}" --prefix /usr >/dev/null

install -D -m 0644 packaging/systemd/faircom-mcp.service \
  "${STAGE_DIR}/usr/lib/systemd/system/faircom-mcp.service"
install -D -m 0644 packaging/systemd/faircom-mcp.env.example \
  "${STAGE_DIR}/etc/faircom-mcp/faircom-mcp.env"
install -D -m 0644 packaging/logrotate/faircom-mcp \
  "${STAGE_DIR}/etc/logrotate.d/faircom-mcp"
install -D -m 0644 packaging/sysusers.d/faircom-mcp.conf \
  "${STAGE_DIR}/usr/lib/sysusers.d/faircom-mcp.conf"
install -D -m 0644 packaging/tmpfiles.d/faircom-mcp.conf \
  "${STAGE_DIR}/usr/lib/tmpfiles.d/faircom-mcp.conf"

COMMON_ARGS=(
  -s dir
  -n faircom-mcp
  -v "${VERSION}"
  --license "Proprietary"
  --url "https://faircom.com/"
  --description "Production-grade MCP server for the FairCom JSON API."
  --maintainer "FairCom <support@faircom.com>"
  --architecture all
  -C "${STAGE_DIR}"
  .
)

fpm "${COMMON_ARGS[@]}" \
  -t deb \
  --package "${OUTPUT_DIR}/faircom-mcp_${VERSION}_all.deb" \
  --depends python3 \
  --depends python3-pip \
  --depends ca-certificates \
  --depends systemd \
  --config-files /etc/faircom-mcp/faircom-mcp.env \
  --config-files /etc/logrotate.d/faircom-mcp \
  --after-install packaging/deb/postinst \
  --before-remove packaging/deb/prerm \
  --after-remove packaging/deb/postrm

fpm "${COMMON_ARGS[@]}" \
  -t rpm \
  --package "${OUTPUT_DIR}/faircom-mcp-${VERSION}-1.noarch.rpm" \
  --depends python3 \
  --depends python3-pip \
  --depends ca-certificates \
  --depends systemd \
  --config-files /etc/faircom-mcp/faircom-mcp.env \
  --config-files /etc/logrotate.d/faircom-mcp \
  --after-install packaging/deb/postinst \
  --before-remove packaging/deb/prerm \
  --after-remove packaging/deb/postrm

echo "Built packages:"
ls -1 "${OUTPUT_DIR}"/*.deb "${OUTPUT_DIR}"/*.rpm
