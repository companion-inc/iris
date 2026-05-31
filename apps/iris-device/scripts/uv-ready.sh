#!/usr/bin/env bash
set -euo pipefail

UV_BIN="${UV_BIN:-/usr/local/bin/uv}"

if [ -x "$UV_BIN" ]; then
  exit 0
fi

if command -v uv >/dev/null 2>&1; then
  exit 0
fi

curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="/usr/local/bin" sh

if [ ! -x "$UV_BIN" ]; then
  echo "uv install did not create $UV_BIN" >&2
  exit 1
fi
