#!/bin/sh
# One command on Linux/macOS: ./run.sh <url>   or   ./run.sh replay captures/<dir>
# First run downloads uv, Python, dependencies and Chromium into this folder (needs internet once).
set -e
cd "$(dirname "$0")"
here=$(pwd)
export UV_INSTALL_DIR="$here/.uv" UV_NO_MODIFY_PATH=1 UV_PYTHON_INSTALL_DIR="$here/.uv/python" \
       UV_CACHE_DIR="$here/.uv/cache" UV_PYTHON_PREFERENCE=only-managed PLAYWRIGHT_BROWSERS_PATH="$here/.browsers"
export PATH="$UV_INSTALL_DIR:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  url=https://astral.sh/uv/install.sh
  { command -v curl >/dev/null 2>&1 && curl -LsSf "$url" || wget -qO- "$url"; } | sh
fi
uv run --frozen --quiet playwright install chromium --no-shell
exec uv run --frozen --quiet web-cloner "$@"
