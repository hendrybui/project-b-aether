#!/usr/bin/env bash
# seed-llm-config.sh
# One-command setup of Aether's cloud-LLM row for the local 9router gateway,
# so a fresh browser never needs manual key pasting.
#
# How it works: the browser's localStorage is per-origin, so a terminal script
# cannot write it directly. Instead this helper:
#   1. reads the 9router gateway client key from ~/.9router/db/data.sqlite
#      (the gateway itself already holds the real provider keys — nothing new
#      is configured upstream),
#   2. writes a tiny same-origin seed page to Vite's public/ dir
#      (served as <origin>/aether/llm-seed.html),
#   3. opens that URL in your default browser (or prints it). The page writes
#      the config into localStorage and redirects into Aether. Done.
#
# The seed file is git-ignored (public/llm-seed.html) and removed with the
# `clean` subcommand — run it after the browser has been seeded if you don't
# want the key sitting in public/.
#
# Usage: seed-llm-config.sh {seed|status|clean} [--no-open] [--origin URL]
#   seed     write the seed page and open it in the browser (default action)
#   status   show what a fresh browser would get (never prints the key)
#   clean    delete public/llm-seed.html
#
# Overrides (env): AM9_KEY (skip the DB read), AM9_BASE (default
# http://127.0.0.1:20128/v1), AM9_MODEL (default groq/llama-3.3-70b-versatile),
# plus --origin for the app URL (default http://localhost:5173, the Vite dev
# server; pass http://localhost if you reach Aether via the Caddy proxy).

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SEED="$ROOT/public/llm-seed.html"
BASE="${AM9_BASE:-http://127.0.0.1:20128/v1}"
MODEL="${AM9_MODEL:-groq/llama-3.3-70b-versatile}"
ORIGIN="http://localhost:5173"
OPEN=1

read_key() {
  # AM9_KEY env wins; otherwise read the 9router gateway key from its sqlite DB.
  if [ -n "${AM9_KEY:-}" ]; then
    echo "$AM9_KEY"
    return 0
  fi
  local db="$HOME/.9router/db/data.sqlite"
  if [ ! -f "$db" ]; then
    echo "" >&2
    echo "[seed] ERROR: 9router DB not found at $db" >&2
    echo "[seed]   Set AM9_KEY=<key> to pass the gateway key directly." >&2
    return 1
  fi
  python3 - "$db" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
try:
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    row = con.execute("SELECT key FROM apiKeys WHERE key != '' LIMIT 1").fetchone()
    con.close()
    print(row[0] if row else '')
except Exception as e:
    print(f'ERR: {e}', file=sys.stderr)
    sys.exit(1)
PYEOF
}

write_seed() {
  local key="$1"
  # Generate the page with python so every value is JSON-escaped (no shell
  # interpolation, no HTML-injection risk from a weird key).
  SEED_KEY="$key" SEED_BASE="$BASE" SEED_MODEL="$MODEL" SEED_ORIGIN="$ORIGIN" \
    python3 <<'PYEOF'
import json, os
key = os.environ['SEED_KEY']
base = os.environ['SEED_BASE']
model = os.environ['SEED_MODEL']
origin = os.environ['SEED_ORIGIN']
app = origin.rstrip('/') + '/aether/'
cfg = json.dumps({'baseUrl': base, 'model': model, 'apiKey': key})
html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aether — cloud LLM config seed</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;height:90vh}
.card{background:#1b1f27;border:1px solid #3a4150;border-radius:10px;padding:28px 36px;text-align:center}
.ok{color:#7ee787}code{color:#ffd479}</style></head>
<body><div class="card">
<h2>Aether · cloud LLM config</h2>
<p>Seeding 9router gateway into this browser…</p>
<p><code id="s">working</code></p>
</div>
<script>
var cfg = %s;
try {
  localStorage.setItem('aether-llm-cloud', JSON.stringify(cfg));
  document.getElementById('s').className = 'ok';
  document.getElementById('s').textContent = 'Saved \u2713 — redirecting to Aether…';
  setTimeout(function () { window.location.href = %s; }, 1200);
} catch (e) {
  document.getElementById('s').textContent = 'Could not save: ' + e;
}
</script></body></html>
""" % (cfg, json.dumps(app))
open(os.environ.get('SEED_OUT', ''), 'w').write(html) if os.environ.get('SEED_OUT') else print(html, end='')
PYEOF
}

cmd="${1:-seed}"
case "$cmd" in
  seed|status) ;;
  clean)
    rm -f "$SEED"
    echo "[seed] removed $SEED"
    exit 0
    ;;
  *) echo "usage: $0 {seed|status|clean} [--no-open] [--origin URL]"; exit 2 ;;
esac

# flags
for a in "$@"; do
  case "$a" in
    --no-open) OPEN=0 ;;
    --origin=*) ORIGIN="${a#--origin=}" ;;
    --origin) shift; ORIGIN="${1:-}" ;;
  esac
done
ORIGIN="${ORIGIN%/}"

if [ "$cmd" = "status" ]; then
  key="$(read_key)" || exit 1
  [ -z "$key" ] && { echo "[seed] ERROR: no gateway key in 9router DB (apiKeys table empty?)." >&2; exit 1; }
  echo "[seed] fresh browsers would receive:"
  echo "        base:  $BASE"
  echo "        model: $MODEL"
  echo "        key:   ${#key} chars (never printed)"
  echo "        via:   ${ORIGIN}/aether/llm-seed.html"
  if [ -f "$SEED" ]; then echo "        seed page: present ($(wc -c < "$SEED") bytes)"; else echo "        seed page: absent — run '$0 seed' to create it"; fi
  exit 0
fi

key="$(read_key)" || exit 1
[ -z "$key" ] && { echo "[seed] ERROR: no gateway key in 9router DB (apiKeys table empty?)." >&2; echo "[seed]   Set AM9_KEY=<key> to pass it explicitly." >&2; exit 1; }

# The gateway should be listening; warn (but still seed) if it isn't.
if ! curl -s --max-time 2 "http://127.0.0.1:20128/v1/models" >/dev/null 2>&1; then
  echo "[seed] WARN: 9router gateway not answering on :20128 — the seeded config will only work once it is up." >&2
fi

SEED_OUT="$SEED" write_seed "$key"
echo "[seed] wrote $SEED"

url="${ORIGIN}/aether/llm-seed.html"
if [ "$OPEN" = "1" ] && command -v xdg-open >/dev/null 2>&1; then
  echo "[seed] opening $url in your default browser…"
  xdg-open "$url" >/dev/null 2>&1 &
  disown
else
  echo "[seed] open this URL once in the browser you use for Aether:"
  echo "        $url"
  echo "  It saves the cloud-LLM config and redirects into Aether."
fi
echo "[seed] after it saved, run '$0 clean' to remove the key from public/."
