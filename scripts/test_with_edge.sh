#!/usr/bin/env bash
set -euo pipefail

FAIRCOM_EDGE_IMAGE="${FAIRCOM_EDGE_IMAGE:-faircomteam/edge}"
FAIRCOM_EDGE_CONTAINER_NAME="${FAIRCOM_EDGE_CONTAINER_NAME:-faircom-edge-test}"
FAIRCOM_EDGE_SCHEME="${FAIRCOM_EDGE_SCHEME:-http}"
FAIRCOM_EDGE_HOST_PORT="${FAIRCOM_EDGE_HOST_PORT:-8080}"
FAIRCOM_EDGE_CONTAINER_PORT="${FAIRCOM_EDGE_CONTAINER_PORT:-8080}"
FAIRCOM_EDGE_STARTUP_TIMEOUT="${FAIRCOM_EDGE_STARTUP_TIMEOUT:-120}"
FAIRCOM_EDGE_RUN_ARGS="${FAIRCOM_EDGE_RUN_ARGS:-}"

export FAIRCOM_API_BASE_URL="${FAIRCOM_API_BASE_URL:-${FAIRCOM_EDGE_SCHEME}://127.0.0.1:${FAIRCOM_EDGE_HOST_PORT}}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for test-edge target" >&2
  exit 1
fi

cleanup() {
  docker rm -f "${FAIRCOM_EDGE_CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup

echo "Pulling ${FAIRCOM_EDGE_IMAGE} ..."
docker pull "${FAIRCOM_EDGE_IMAGE}" >/dev/null

echo "Starting container ${FAIRCOM_EDGE_CONTAINER_NAME} ..."
# shellcheck disable=SC2086
docker run -d \
  --name "${FAIRCOM_EDGE_CONTAINER_NAME}" \
  -p "${FAIRCOM_EDGE_HOST_PORT}:${FAIRCOM_EDGE_CONTAINER_PORT}" \
  ${FAIRCOM_EDGE_RUN_ARGS} \
  "${FAIRCOM_EDGE_IMAGE}" >/dev/null

echo "Waiting for FairCom Edge to respond at ${FAIRCOM_API_BASE_URL} ..."
start_epoch="$(date +%s)"
while true; do
  if curl -sk --max-time 2 "${FAIRCOM_API_BASE_URL}" >/dev/null 2>&1; then
    break
  fi

  now_epoch="$(date +%s)"
  if (( now_epoch - start_epoch >= FAIRCOM_EDGE_STARTUP_TIMEOUT )); then
    echo "Timed out waiting for FairCom Edge backend" >&2
    exit 1
  fi

  sleep 2
done

echo "Running edge integration tests ..."
python3 -m pytest -m edge_integration -q
