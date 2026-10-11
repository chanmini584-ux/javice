#!/usr/bin/env bash
# JARVIS portable launcher (macOS). Double-click it in Finder, or run: ./start-mac.command [--host 0.0.0.0] [--listen]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export JARVIS_HOME="$DIR/data"                       # memory, knowledge, voiceprint stay on the USB
export UV_CACHE_DIR="$DIR/.cache/uv"                  # downloaded packages are reused from the USB
export UV_LINK_MODE=copy                              # USB file systems often lack hard links
# Python and the virtualenv live on this computer: faster, and USB file systems can't hold symlinks.
export UV_PYTHON_INSTALL_DIR="$HOME/Library/Caches/jarvis/python"
export UV_PROJECT_ENVIRONMENT="$HOME/Library/Caches/jarvis/venv-$(uname -s)-$(uname -m)"
EXTRAS="${JARVIS_EXTRAS:-$(cat "$DIR/data/extras.txt" 2>/dev/null || echo web)}"

UV="$DIR/bin/macos/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"
if [ -n "$UV" ]; then
  args=(); IFS=',' read -ra ex <<< "$EXTRAS"; for e in "${ex[@]}"; do args+=(--extra "$e"); done
  exec "$UV" run --project "$DIR/app" "${args[@]}" --python 3.12 jarvis serve "$@"
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "Python 3.10+ 또는 uv가 필요합니다. bin/macos/uv를 넣거나 Python을 설치하세요."; exit 1; }
VENV="$UV_PROJECT_ENVIRONMENT"
[ -x "$VENV/bin/jarvis" ] || { "$PY" -m venv "$VENV" && "$VENV/bin/pip" install -q "$DIR/app[$EXTRAS]"; }
exec "$VENV/bin/jarvis" serve "$@"
