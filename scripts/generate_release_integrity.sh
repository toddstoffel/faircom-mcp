#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v cyclonedx-py >/dev/null 2>&1; then
  echo "cyclonedx-py is required to generate SBOM artifacts" >&2
  exit 1
fi

DIST_DIR="${ROOT_DIR}/dist"
mkdir -p "${DIST_DIR}"

echo "Generating CycloneDX SBOM ..."
cyclonedx-py environment --output-format json --output-file "${DIST_DIR}/sbom.cdx.json"

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
