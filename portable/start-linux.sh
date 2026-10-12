#!/usr/bin/env bash
# JARVIS portable launcher (Linux). Run from the USB stick: ./start-linux.sh [--host 0.0.0.0] [--listen]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export JARVIS_HOME="$DIR/data"                       # memory, knowledge, voiceprint stay on the USB
export UV_CACHE_DIR="$DIR/.cache/uv"                  # downloaded packages are reused from the USB
export UV_LINK_MODE=copy                              # USB file systems often lack hard links
# Python and the virtualenv live on this computer: faster, and USB file systems can't hold symlinks.
export UV_PYTHON_INSTALL_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/jarvis/python"
export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/jarvis/venv-$(uname -s)-$(uname -m)"
EXTRAS="${JARVIS_EXTRAS:-$(cat "$DIR/data/extras.txt" 2>/dev/null || echo web)}"

UV="$DIR/bin/linux/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"
if [ -n "$UV" ]; then
  args=(); IFS=',' read -ra ex <<< "$EXTRAS"; for e in "${ex[@]}"; do args+=(--extra "$e"); done
  exec "$UV" run --project "$DIR/app" "${args[@]}" --python 3.12 jarvis serve --open "$@"
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "Python 3.10+ 또는 uv가 필요합니다. bin/linux/uv를 넣거나 Python을 설치하세요."; exit 1; }
VENV="$UV_PROJECT_ENVIRONMENT"
[ -x "$VENV/bin/jarvis" ] || { "$PY" -m venv "$VENV" && "$VENV/bin/pip" install -q "$DIR/app[$EXTRAS]"; }
exec "$VENV/bin/jarvis" serve --open "$@"
