#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/pyinstaller ]]; then
  echo "PyInstaller is missing. Run: uv sync --group dev" >&2
  exit 1
fi

rm -rf desktop/build desktop/dist
.venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --distpath desktop/dist \
  --workpath desktop/build \
  desktop/omni-agent-server.spec

DEST="$ROOT/desktop/resources/backend"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$ROOT/desktop/dist/omni-agent-server" "$DEST/"

echo "Bundled backend: $DEST/omni-agent-server/omni-agent-server"
