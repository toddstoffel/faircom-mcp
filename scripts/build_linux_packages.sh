#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

IN_DOCKER_FPM_BUILD="${IN_DOCKER_FPM_BUILD:-0}"

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build DEB/RPM packages" >&2
  exit 1
fi

FPM_BIN="${FPM_BIN:-$(command -v fpm)}"
if "${FPM_BIN}" --version 2>&1 | grep -qi "Fortran package manager"; then
  RUBY_VERSION="$(ruby -e 'print RbConfig::CONFIG["ruby_version"]' 2>/dev/null || true)"
  RUBY_FPM="${HOME}/.gem/ruby/${RUBY_VERSION}/bin/fpm"
  if [[ -n "${RUBY_VERSION}" && -x "${RUBY_FPM}" ]]; then
    FPM_BIN="${RUBY_FPM}"
  else
    echo "Detected Fortran fpm at ${FPM_BIN}. Install Ruby fpm (gem install --user-install fpm) and set FPM_BIN if needed." >&2
    exit 1
  fi
fi

VERSION="${1:-$(python3 - <<'PY'
import tomllib
from pathlib import Path

pyproject = Path('pyproject.toml')
data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
print(data['project']['version'])
PY
)}"

if [[ "$(uname -s)" == "Darwin" && "${IN_DOCKER_FPM_BUILD}" != "1" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to build valid Linux DEB/RPM packages from macOS hosts" >&2
    exit 1
  fi

  echo "Detected macOS host; building Linux packages in ubuntu:24.04 container ..."
  docker run --rm \
    -e DEBIAN_FRONTEND=noninteractive \
    -e IN_DOCKER_FPM_BUILD=1 \
    -e VERSION="${VERSION}" \
    -e OUTPUT_DIR="/work/dist/packages" \
    -e BUILD_DIR="/work/build/packages" \
    -v "${ROOT_DIR}:/work" \
    -w /work \
    ubuntu:24.04 bash -lc '
      set -euo pipefail
      apt-get update >/dev/null
      apt-get install -y ca-certificates python3 python3-pip ruby ruby-dev build-essential rpm >/dev/null
      gem install --no-document fpm >/dev/null
      bash scripts/build_linux_packages.sh "${VERSION}"
    '
  exit 0
fi

OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/dist/packages}"
BUILD_DIR="${BUILD_DIR:-${ROOT_DIR}/build/packages}"
STAGE_DIR="${BUILD_DIR}/stage"

PIP_INSTALL_ARGS=()
if python3 -m pip --help | grep -q -- '--break-system-packages'; then
  PIP_INSTALL_ARGS+=(--break-system-packages)
fi
export PIP_BREAK_SYSTEM_PACKAGES=1

rm -rf "${BUILD_DIR}"
mkdir -p "${STAGE_DIR}" "${OUTPUT_DIR}"

python3 -m pip install "${PIP_INSTALL_ARGS[@]}" . --root "${STAGE_DIR}" --prefix /usr >/dev/null

install_file() {
  local src="$1"
  local dest="$2"

  mkdir -p "$(dirname "${dest}")"
  install -m 0644 "${src}" "${dest}"
}

install_file packaging/systemd/faircom-mcp.service \
  "${STAGE_DIR}/usr/lib/systemd/system/faircom-mcp.service"
install_file packaging/systemd/faircom-mcp.env.example \
  "${STAGE_DIR}/etc/faircom-mcp/faircom-mcp.env"
install_file packaging/logrotate/faircom-mcp \
  "${STAGE_DIR}/etc/logrotate.d/faircom-mcp"
install_file packaging/sysusers.d/faircom-mcp.conf \
  "${STAGE_DIR}/usr/lib/sysusers.d/faircom-mcp.conf"
install_file packaging/tmpfiles.d/faircom-mcp.conf \
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
)

"${FPM_BIN}" "${COMMON_ARGS[@]}" \
  -t deb \
  --force \
  --package "${OUTPUT_DIR}/faircom-mcp_${VERSION}_all.deb" \
  --depends python3 \
  --depends python3-pip \
  --depends ca-certificates \
  --depends systemd \
  --config-files /etc/faircom-mcp/faircom-mcp.env \
  --config-files /etc/logrotate.d/faircom-mcp \
  --after-install packaging/deb/postinst \
  --before-remove packaging/deb/prerm \
  --after-remove packaging/deb/postrm \
  .

"${FPM_BIN}" "${COMMON_ARGS[@]}" \
  -t rpm \
  --force \
  --package "${OUTPUT_DIR}/faircom-mcp-${VERSION}-1.noarch.rpm" \
  --depends python3 \
  --depends python3-pip \
  --depends ca-certificates \
  --depends systemd \
  --config-files /etc/faircom-mcp/faircom-mcp.env \
  --config-files /etc/logrotate.d/faircom-mcp \
  --after-install packaging/deb/postinst \
  --before-remove packaging/deb/prerm \
  --after-remove packaging/deb/postrm \
  .

echo "Built packages:"
ls -1 "${OUTPUT_DIR}"/*.deb "${OUTPUT_DIR}"/*.rpm
