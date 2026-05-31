#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WAKE_TEXT="${IRIS_TEST_WAKE_TEXT:-Iris, what time is it?}"
WAIT_SECONDS="${IRIS_TEST_WAIT_SECONDS:-20}"

printf 'Iris local desktop voice loop test\n'
printf '  repo: %s\n' "$ROOT_DIR"
printf '  text: %s\n' "$WAKE_TEXT"
printf '  wait: %ss\n' "$WAIT_SECONDS"

cd "$ROOT_DIR"
exec pnpm voice:smoke --text "$WAKE_TEXT" --wait-seconds "$WAIT_SECONDS"
