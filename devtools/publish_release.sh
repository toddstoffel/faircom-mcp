#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

usage() {
  cat <<'EOF'
Publish a release by creating/pushing a git tag and monitoring release workflows.

Usage:
  devtools/publish_release.sh <tag> [--no-wait] [--allow-dirty]

Arguments:
  <tag>           Release tag to publish (must start with 'v', e.g. vX.Y.Z)

Options:
  --no-wait       Do not wait for GitHub Actions workflows to finish
  --allow-dirty   Allow running with local uncommitted changes
  -h, --help      Show this help message

What this does:
  1) Runs local lint checks before any tag is pushed
  2) Creates and pushes the tag to origin
  3) Triggers:
     - Release workflow (publishes GitHub release assets)
     - Docker Hub workflow (publishes multi-arch image)
  4) Optionally waits for both workflow runs to complete
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

run_lint_preflight() {
  if command -v python >/dev/null 2>&1; then
    python -m ruff check src tests
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 -m ruff check src tests
    return
  fi

  echo "Missing Python interpreter for lint preflight (python/python3)." >&2
  exit 1
}

TAG=""
WAIT_FOR_RUNS=1
ALLOW_DIRTY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --no-wait)
      WAIT_FOR_RUNS=0
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "${TAG}" ]]; then
        echo "Only one tag argument is allowed." >&2
        usage >&2
        exit 1
      fi
      TAG="$1"
      shift
      ;;
  esac
done

if [[ -z "${TAG}" ]]; then
  echo "A release tag is required." >&2
  usage >&2
  exit 1
fi

if [[ "${TAG}" != v* ]]; then
  echo "Tag must start with 'v' (example: vX.Y.Z)." >&2
  exit 1
fi

require_cmd git
require_cmd gh

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated. Run: gh auth login" >&2
  exit 1
fi

if [[ "${ALLOW_DIRTY}" -ne 1 ]] && [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree has uncommitted changes. Commit/stash first or use --allow-dirty." >&2
  exit 1
fi

echo "Running local lint preflight"
run_lint_preflight

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag already exists locally: ${TAG}" >&2
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
  echo "Tag already exists on origin: ${TAG}" >&2
  exit 1
fi

HEAD_SHA="$(git rev-parse HEAD)"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "Preparing release publish"
echo "- Branch: ${CURRENT_BRANCH}"
echo "- Commit: ${HEAD_SHA}"
echo "- Tag:    ${TAG}"

git tag "${TAG}"
git push origin "refs/tags/${TAG}"

echo "Tag pushed. Workflows triggered: Release, Docker Hub"

if [[ "${WAIT_FOR_RUNS}" -eq 0 ]]; then
  echo "Not waiting for workflow completion (--no-wait)."
  exit 0
fi

export TERM=dumb
export NO_COLOR=1
export CLICOLOR=0
export GH_PAGER=cat

# Give Actions a moment to register runs for the new tag.
sleep 3

RELEASE_RUN_ID="$(gh run list --workflow "Release" --limit 1 --json databaseId --jq '.[0].databaseId')"
DOCKER_RUN_ID="$(gh run list --workflow "Docker Hub" --limit 1 --json databaseId --jq '.[0].databaseId')"

echo "Watching workflow runs"
echo "- Release run:    ${RELEASE_RUN_ID}"
echo "- Docker Hub run: ${DOCKER_RUN_ID}"

RELEASE_EXIT=0
DOCKER_EXIT=0

gh run watch "${RELEASE_RUN_ID}" --exit-status || RELEASE_EXIT=$?
gh run watch "${DOCKER_RUN_ID}" --exit-status || DOCKER_EXIT=$?

echo "Final workflow status"
gh run view "${RELEASE_RUN_ID}" --json url,status,conclusion --jq '{workflow:"Release", url, status, conclusion}'
gh run view "${DOCKER_RUN_ID}" --json url,status,conclusion --jq '{workflow:"Docker Hub", url, status, conclusion}'

if [[ "${RELEASE_EXIT}" -ne 0 || "${DOCKER_EXIT}" -ne 0 ]]; then
  echo "One or more workflows failed." >&2
  exit 1
fi

echo "Publish complete."
