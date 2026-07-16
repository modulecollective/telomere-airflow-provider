#!/usr/bin/env bash
# Bring up the compose stack, run the e2e tier against it, tear down.
#
# Usage: TELOMERE_API_KEY=... tests/e2e/run.sh [extra pytest args]
# Needs: docker compose, and pytest + requests on PATH (the dev group has
# both — `uv sync` or `pip install -e . --group dev`).
set -euo pipefail

cd "$(dirname "$0")/../.."

: "${TELOMERE_API_KEY:?TELOMERE_API_KEY must be set (the e2e tier talks to the real Telomere API)}"

docker compose up -d --build --wait

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    docker compose logs --tail 200
  fi
  docker compose down -v
}
trap cleanup EXIT

pytest -m e2e tests/e2e -v "$@"
