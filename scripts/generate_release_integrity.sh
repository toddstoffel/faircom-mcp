#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

CYCLONEDX_PY_BIN="$(command -v cyclonedx-py || true)"
if [[ -z "${CYCLONEDX_PY_BIN}" ]]; then
  USER_CYCLONEDX_PY="$(python3 - <<'PY'
import site
from pathlib import Path

print((Path(site.getuserbase()) / 'bin' / 'cyclonedx-py').as_posix())
PY
)"
  if [[ -x "${USER_CYCLONEDX_PY}" ]]; then
    CYCLONEDX_PY_BIN="${USER_CYCLONEDX_PY}"
  fi
fi

if [[ -z "${CYCLONEDX_PY_BIN}" ]]; then
  TOOLS_VENV_DIR="${ROOT_DIR}/.tools/cyclonedx-venv"
  if [[ ! -x "${TOOLS_VENV_DIR}/bin/cyclonedx-py" ]]; then
    echo "Preparing local tools environment for cyclonedx-py ..."
    python3 -m venv "${TOOLS_VENV_DIR}"
    "${TOOLS_VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
    "${TOOLS_VENV_DIR}/bin/python" -m pip install cyclonedx-bom >/dev/null
  fi
  CYCLONEDX_PY_BIN="${TOOLS_VENV_DIR}/bin/cyclonedx-py"
fi

if [[ ! -x "${CYCLONEDX_PY_BIN}" ]]; then
  echo "cyclonedx-py could not be resolved or installed" >&2
  exit 1
fi

DIST_DIR="${ROOT_DIR}/dist"
mkdir -p "${DIST_DIR}"

echo "Generating CycloneDX SBOM ..."
"${CYCLONEDX_PY_BIN}" environment --output-format json --output-file "${DIST_DIR}/sbom.cdx.json"

echo "Generating SHA256 checksums ..."
python3 - <<'PY'
from __future__ import annotations

import hashlib
from pathlib import Path

root = Path("dist")
checksum_file = root / "SHA256SUMS"

files = sorted(
    p for p in root.rglob("*") if p.is_file() and p.resolve() != checksum_file.resolve()
)

with checksum_file.open("w", encoding="utf-8") as out:
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = path.as_posix()
        out.write(f"{digest}  {rel}\n")
PY

echo "Generated integrity artifacts:"
ls -1 "${DIST_DIR}/sbom.cdx.json" "${DIST_DIR}/SHA256SUMS"
