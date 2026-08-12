#!/usr/bin/env bash
# Music Tools — static server for the local melody/sheet tools.
# Uses the audiomass venv python (no special deps needed for static serving,
# but keeping one interpreter simplifies things).
set -euo pipefail

PORT="${MUSIC_TOOLS_PORT:-8091}"
VENV_PY="$(cd "$(dirname "$0")/.." && pwd)/audiomass/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then VENV_PY="python3"; fi

cd "$(dirname "$0")"
echo "Music Tools serving on http://0.0.0.0:${PORT}"
echo "  Melody Generator: http://localhost:${PORT}/melody-generator.html"
echo "  Audio → Sheet:     http://localhost:${PORT}/audio-to-sheet.html"
exec "$VENV_PY" -m http.server "$PORT"
