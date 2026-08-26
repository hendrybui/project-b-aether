#!/usr/bin/env bash
# run-aether-with-audiomass.sh
# Robust launcher for Aether (AI synth dev) + AudioMass (editor) + Caddy reverse proxy.
# All live together on the Pandora box for unified creative workflow.
# - Aether: npm run dev on 5173 (with --base /aether/ so /aether proxy works w/ hot reload + HMR)
# - AudioMass: its run.sh (stdlib server) on 5055 (relative assets -> /mass proxy works)
# - DJ Toolkit: dj_toolkit/app.py (flask) on 5001 — stems / BPM-key / vocal remover / MP3->MIDI
# - Music Tools: music-tools/run.sh (static) on 8091 — melody generator / audio-to-sheet
# - Open WebUI: ~/webui/start-webui.sh (venv) on 3000 — LLM hub / chat / tools (Caddy catch-all route)
# - Caddy: intelligently start/stop/restart using /mnt/Pandora/caddy/Caddyfile if not running or config hash changed.
#   If a system-managed Caddy (systemd caddy.service, /etc/caddy/Caddyfile) already owns :80 with an
#   identical config, the script adopts it instead of double-binding; on config drift it tries to
#   sync + restart the service via sudo -n, else prints the manual sync command.
#   Uses pidfiles + config hash in /tmp for reliable management across sessions.
#   Tries direct then sudo -n; continues even if Caddy bind fails (direct ports still work).
#
# Place / run from: /mnt/Pandora/Project-B/
#
# Commands: start (default), stop, restart, status, build-aether, caddy-restart
#
# One-time Caddy setup (as user who will run this):
#   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/setup.deb.sh' | sudo bash
#   sudo apt install -y caddy
#   sudo setcap 'cap_net_bind_service=+ep' "$(command -v caddy)"
#   # (then this script can manage caddy w/o sudo passwords for :80)
#
# The Caddyfile stays at /mnt/Pandora/caddy/Caddyfile (script uses --config explicitly; no need to cp to /etc).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AETHER_DIR="$PROJECT_ROOT"
AUDIOMASS_DIR="$PROJECT_ROOT/audiomass"
DJ_TOOLKIT_DIR="$PROJECT_ROOT/dj_toolkit"
MUSIC_TOOLS_DIR="$PROJECT_ROOT/music-tools"
MELODY_SUITE_DIR="$PROJECT_ROOT/melody-suite"
MIXER_DIR="$PROJECT_ROOT/mixer"
MIXER_SERVICE="mixer.service"
MIXER_PORT=5058   # NOT 5060 — browsers hard-block 5060 (SIP) with ERR_UNSAFE_PORT
LLAMA_GPU_SCRIPT="$PROJECT_ROOT/scripts/start-llama-gpu.sh"
SEED_LLM_SCRIPT="$PROJECT_ROOT/scripts/seed-llm-config.sh"
CADDY_CONFIG="/mnt/Pandora/caddy/Caddyfile"

AETHER_PORT=5173
AUDIOMASS_PORT=5055
DJ_TOOLKIT_PORT=5001
MUSIC_TOOLS_PORT=8091
MELODY_SUITE_PORT=5002
# ROCm demucs image for the AudioMass GPU warm pool (audiomass/backend/adapters/docker_runtime.py).
# The server degrades to the local CPU worker if docker/daemon/image is unavailable,
# so this is safe to always set; the pool idle-evicts after 600s of no jobs.
DEMUCS_IMAGE="rocm64_gfx803_demucs:2.4"

# Logs and runtime state under /tmp (shared "Pandora" namespace, survives script restarts)
LOG_DIR="/tmp"
AETHER_LOG="$LOG_DIR/aether-dev.log"
AUDIOMASS_LOG="$LOG_DIR/audiomass.log"
DJ_TOOLKIT_LOG="$LOG_DIR/dj-toolkit.log"
MUSIC_TOOLS_LOG="$LOG_DIR/music-tools.log"
MELODY_SUITE_LOG="$LOG_DIR/melody-suite.log"
CADDY_LOG="$LOG_DIR/pandora-caddy.log"
AETHER_PIDFILE="$LOG_DIR/aether-dev.pid"
AUDIOMASS_PIDFILE="$LOG_DIR/audiomass.pid"
DJ_TOOLKIT_PIDFILE="$LOG_DIR/dj-toolkit.pid"
MUSIC_TOOLS_PIDFILE="$LOG_DIR/music-tools.pid"
MELODY_SUITE_PIDFILE="$LOG_DIR/melody-suite.pid"
CADDY_PIDFILE="$LOG_DIR/pandora-caddy.pid"
CADDY_HASHFILE="$LOG_DIR/pandora-caddy-config.hash"

# Open WebUI (LLM hub) — managed via its own scripts in ~/webui; Caddy catch-all on :80
WEBUI_HOME="${HOME}/webui"
WEBUI_START_SCRIPT="$WEBUI_HOME/start-webui.sh"
WEBUI_STOP_SCRIPT="$WEBUI_HOME/stop-webui.sh"
WEBUI_APP_VENV="/mnt/Pandora/open-webui-local/venv/bin/open-webui"
WEBUI_PORT=3000
WEBUI_LOG="$LOG_DIR/webui.log"
WEBUI_PIDFILE="$LOG_DIR/webui.pid"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Guard: every path this script touches (and docker's data-root, the Caddy
# config, the systemd units) assumes the Pandora drive is mounted at
# /mnt/Pandora. If the mount moved (udisks auto-mount, fstab edit, drive
# crash), starting would silently half-break everything. Fail loudly instead.
guard_pandora_mount() {
  if ! "$PROJECT_ROOT/scripts/check-pandora-mount.sh" 2>&1; then
    log "WARNING: Pandora drive is not at /mnt/Pandora — some features may be unavailable."
    log "Run the fix printed above (or /usr/local/bin/check-pandora-mount.sh) when convenient."
    return 0
  fi
}

check_caddy_installed() {
  if ! command -v caddy >/dev/null 2>&1; then
    log "WARNING: caddy not found in PATH — proxy will be unavailable (direct ports still work)."
    return 1
  fi
}

get_caddy_pid() {
  if [ -f "$CADDY_PIDFILE" ]; then
    local pid
    pid=$(cat "$CADDY_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  fi
  # Fallback search by our exact config path in cmdline (works for start/run)
  pgrep -f -- "--config ${CADDY_CONFIG}" | head -1 || true
}

caddy_config_hash() {
  if [ -f "$CADDY_CONFIG" ]; then
    sha256sum "$CADDY_CONFIG" | awk '{print $1}'
  else
    echo "missing-config-file"
  fi
}

# Echo "pid config_path" for a running Caddy we did NOT start (e.g. the
# systemd caddy.service with /etc/caddy/Caddyfile), if any. Empty otherwise.
external_caddy_line() {
  local pid cfg
  for pid in $(pgrep -x caddy 2>/dev/null); do
    [ -r "/proc/$pid/cmdline" ] || continue
    cfg=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
      | awk '/^--config=/{sub(/^--config=/,""); print; exit} /^--config$/{getline; print; exit}')
    [ -n "$cfg" ] || continue
    if [ "$cfg" != "$CADDY_CONFIG" ]; then
      echo "$pid $cfg"
      return 0
    fi
  done
  return 0
}

stop_caddy_internal() {
  local pid
  pid=$(get_caddy_pid || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "   Stopping Caddy (pid $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 0.4
    kill -9 "$pid" 2>/dev/null || true
  fi
  # Broad cleanup for this config (in case pidfile stale or multiple)
  pkill -f "caddy.*--config ${CADDY_CONFIG}" 2>/dev/null || true
  pkill -f "caddy run --config ${CADDY_CONFIG}" 2>/dev/null || true
  rm -f "$CADDY_PIDFILE" 2>/dev/null || true
}

stop_caddy() {
  log "→ Stopping Caddy (if managed)..."
  stop_caddy_internal
  rm -f "$CADDY_HASHFILE" 2>/dev/null || true
  log "   Caddy stop complete."
}

ensure_caddy() {
  check_caddy_installed

  log "→ Ensuring Caddy reverse proxy (config: $CADDY_CONFIG)..."

  local current_hash
  current_hash=$(caddy_config_hash)
  local last_hash=""
  [ -f "$CADDY_HASHFILE" ] && last_hash=$(cat "$CADDY_HASHFILE" 2>/dev/null || true)

  local pid
  pid=$(get_caddy_pid || true)

  local need_restart=0
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    log "   No active Caddy for our pidfile/config"
    need_restart=1
  elif [ "$current_hash" != "$last_hash" ]; then
    log "   Caddyfile content changed (new hash $current_hash != $last_hash)"
    need_restart=1
  fi

  if [ $need_restart -eq 1 ]; then
    # Adopt an externally managed Caddy (e.g. systemd caddy.service) when it
    # already owns :80 and serves a config identical to ours — starting a
    # second Caddy would just fail to bind.
    local ext_line ext_pid ext_cfg ext_hash
    ext_line=$(external_caddy_line || true)
    if [ -n "$ext_line" ]; then
      ext_pid=${ext_line%% *}
      ext_cfg=${ext_line#* }
      ext_hash=$( [ -f "$ext_cfg" ] && sha256sum "$ext_cfg" | awk '{print $1}' || echo "no-file" )
      if [ "$ext_hash" = "$current_hash" ]; then
        log "   Using externally managed Caddy (pid $ext_pid, config $ext_cfg — identical to ours)."
        echo "$current_hash" > "$CADDY_HASHFILE"
        return 0
      fi
      log "   NOTE: external Caddy (pid $ext_pid) serves $ext_cfg, which differs from $CADDY_CONFIG."
      if sudo -n cp "$CADDY_CONFIG" "$ext_cfg" 2>/dev/null && sudo -n systemctl restart caddy 2>/dev/null; then
        sleep 1
        log "   Synced $ext_cfg from ours and restarted the caddy system service."
        echo "$current_hash" > "$CADDY_HASHFILE"
        return 0
      fi
      log "   Could not auto-sync (needs passwordless sudo). Sync it manually:"
      log "   sudo cp '$CADDY_CONFIG' '$ext_cfg' && sudo systemctl restart caddy"
    fi

    log "   (Re)starting Caddy..."
    stop_caddy_internal

    local caddy_bin
    caddy_bin=$(command -v caddy)

    # Preferred: caddy start (daemonizes, writes our pidfile, picks up --config)
    if "$caddy_bin" start --config "$CADDY_CONFIG" --pidfile "$CADDY_PIDFILE" >>"$CADDY_LOG" 2>&1; then
      sleep 0.6
      if [ -f "$CADDY_PIDFILE" ]; then
        pid=$(cat "$CADDY_PIDFILE")
        log "   Caddy started (pid $pid). Logs: $CADDY_LOG"
        echo "$current_hash" > "$CADDY_HASHFILE"
      else
        log "   WARNING: caddy start reported success but pidfile missing (check $CADDY_LOG)"
      fi
    else
      local start_rc=$?
      log "   'caddy start' failed (rc=$start_rc) — port 80 bind or permissions likely."
      log "   Retrying via sudo -n (non-interactive sudo)..."
      if sudo -n "$caddy_bin" start --config "$CADDY_CONFIG" --pidfile "$CADDY_PIDFILE" >>"$CADDY_LOG" 2>&1; then
        sleep 0.6
        pid=$(cat "$CADDY_PIDFILE" 2>/dev/null || echo "unknown")
        log "   Caddy started with sudo (pid $pid). Logs: $CADDY_LOG"
        echo "$current_hash" > "$CADDY_HASHFILE"
      else
        log "   WARNING: Could not auto-start Caddy (port may be in use by another process, or sudo rules)."
        log "   Check current listener: sudo ss -tlnp | grep :80"
        log "   Manual start example: sudo caddy start --config '$CADDY_CONFIG' --pidfile '$CADDY_PIDFILE'"
        log "   (Apps will still start on their direct ports; proxy URLs will be unavailable until Caddy is up.)"
        rm -f "$CADDY_PIDFILE" 2>/dev/null || true
      fi
    fi
  else
    log "   Caddy already running with up-to-date config (pid: $pid)"
  fi
}

dry_run() {
  echo "========================================"
  log "=== DRY RUN: Checking all dependencies ==="
  echo "Project root: $PROJECT_ROOT"
  echo "Timestamp: $(date)"
  echo ""

  local ok=0 warn=0 fail=0

  check() {
    local label="$1" status="$2" detail="${3:-}"
    case "$status" in
      ok)   echo "  ✓ $label"; ((ok++)) || true ;;
      warn) echo "  ⚠ $label — $detail"; ((warn++)) || true ;;
      fail) echo "  ✗ $label — $detail"; ((fail++)) || true ;;
    esac
  }

  # --- Core ---
  echo "[Core]"
  if "$PROJECT_ROOT/scripts/check-pandora-mount.sh" >/dev/null 2>&1; then
    check "Pandora drive mounted at /mnt/Pandora" ok
  else
    check "Pandora drive" fail "not at /mnt/Pandora — mount it or run: sudo mount -a"
  fi

  if [ -d "$AETHER_DIR/node_modules" ]; then
    check "node_modules/ exists" ok
  else
    check "node_modules/" warn "missing — npm install will run on first start"
  fi

  if [ -f "$AETHER_DIR/package.json" ] && command -v node >/dev/null 2>&1; then
    check "Node.js $(node -v)" ok
  else
    check "Node.js" fail "not found in PATH"
  fi

  if command -v npm >/dev/null 2>&1; then
    check "npm $(npm -v)" ok
  else
    check "npm" fail "not found in PATH"
  fi

  echo ""

  # --- AudioMass ---
  echo "[AudioMass]"
  if [ -d "$PROJECT_ROOT/backend" ]; then
    check "backend/ directory" ok
  else
    check "backend/ directory" fail "not found"
  fi

  local am_py="$PROJECT_ROOT/backend/.venv/bin/python"
  if [ -x "$am_py" ]; then
    check "backend venv python" ok
    if "$am_py" -c 'import fastapi, uvicorn' 2>/dev/null; then
      check "FastAPI + uvicorn in backend venv" ok
    else
      check "FastAPI + uvicorn" warn "not in venv — run: backend/.venv/bin/pip install -r backend/requirements.txt"
    fi
  else
    check "backend venv" warn "missing — copy or bootstrap one (see backend/README.md)"
  fi

  echo ""

  # --- Caddy ---
  echo "[Caddy]"
  if command -v caddy >/dev/null 2>&1; then
    check "caddy binary" ok
  else
    check "caddy" warn "not installed — proxy unavailable (direct ports still work)"
  fi

  if [ -f "$CADDY_CONFIG" ]; then
    check "Caddyfile at $CADDY_CONFIG" ok
  else
    check "Caddyfile" warn "not found at $CADDY_CONFIG"
  fi

  local cpid
  cpid=$(get_caddy_pid || true)
  if [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null; then
    check "Caddy running (pid $cpid)" ok
  else
    check "Caddy running" warn "not running — will start on launch"
  fi

  echo ""

  # --- GPU / LLM ---
  echo "[GPU & LLM]"
  if [ -x "$LLAMA_GPU_SCRIPT" ]; then
    check "start-llama-gpu.sh" ok
  else
    check "GPU LLM script" warn "not found — Aether will use Ollama CPU fallback"
  fi

  if command -v ollama >/dev/null 2>&1; then
    check "Ollama installed" ok
    if curl -sS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
      check "Ollama server running" ok
    else
      check "Ollama server" warn "not running on :11434"
    fi
  else
    check "Ollama" warn "not installed"
  fi

  if [ -x "$SEED_LLM_SCRIPT" ]; then
    check "seed-llm-config.sh" ok
  else
    check "Cloud-LLM seed script" warn "not found"
  fi

  echo ""

  # --- Companion apps ---
  echo "[Companion Apps]"
  if [ -d "$DJ_TOOLKIT_DIR" ]; then
    check "DJ Toolkit directory" ok
    if [ -x "$PROJECT_ROOT/backend/.venv/bin/python" ]; then
      check "DJ Toolkit uses AudioMass venv" ok
    fi
  else
    check "DJ Toolkit" warn "dj_toolkit/ not found — skipped"
  fi

  if [ -d "$MUSIC_TOOLS_DIR" ] && [ -f "$MUSIC_TOOLS_DIR/run.sh" ]; then
    check "Music Tools" ok
  else
    check "Music Tools" warn "music-tools/ or run.sh not found — skipped"
  fi

  if [ -d "$MELODY_SUITE_DIR" ]; then
    check "Melody Suite directory" ok
    if [ -x "$MELODY_SUITE_DIR/.venv/bin/python" ]; then
      check "Melody Suite venv" ok
    else
      check "Melody Suite venv" warn "will be created on first start"
    fi
  else
    check "Melody Suite" warn "melody-suite/ not found — skipped"
  fi

  if [ -x "$WEBUI_START_SCRIPT" ]; then
    check "Open WebUI start script" ok
  else
    check "Open WebUI" warn "~/webui/start-webui.sh not found — skipped"
  fi

  echo ""

  # --- Ports ---
  echo "[Port Availability]"
  for port_info in "$AETHER_PORT:Aether" "$AUDIOMASS_PORT:AudioMass" "$DJ_TOOLKIT_PORT:DJ Toolkit" "$MUSIC_TOOLS_PORT:Music Tools" "$MELODY_SUITE_PORT:Melody Suite" "$WEBUI_PORT:Open WebUI"; do
    local port="${port_info%%:*}"
    local name="${port_info#*:}"
    if ss -tlnp 2>/dev/null | grep -q ":$port[ ]"; then
      check ":$port ($name)" warn "already in use — may conflict"
    else
      check ":$port ($name)" ok "available"
    fi
  done

  echo ""
  echo "========================================"
  echo "Summary: $ok ok, $warn warnings, $fail failures"
  if [ $fail -gt 0 ]; then
    echo "Some critical dependencies are missing. Fix the ✗ items above before starting."
  elif [ $warn -gt 0 ]; then
    echo "Non-critical warnings — the core stack (Aether + AudioMass) should still start."
  else
    echo "All checks passed — ready to start!"
  fi
  echo "========================================"
  exit $fail
}

start_aether_dev() {
  log "→ Starting Aether (dev + hot reload + HMR) on :$AETHER_PORT with base=/aether/ ..."

  cd "$AETHER_DIR"

  if [ ! -d node_modules ]; then
    log "   node_modules missing — running npm install..."
    npm install
  fi

  # Clean prior instance
  if [ -f "$AETHER_PIDFILE" ]; then
    kill "$(cat "$AETHER_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$AETHER_PIDFILE"
  fi
  pkill -f "vite.*$AETHER_PORT" 2>/dev/null || true
  pkill -f -- "--port $AETHER_PORT" 2>/dev/null || true

  # Key: --base /aether/ makes Vite emit asset + HMR + import URLs under /aether/...
  # Combined with Caddy "handle /aether* { uri strip_prefix /aether; reverse_proxy localhost:5173 }"
  # this lets everything load correctly when accessed via the unified proxy URL.
  nohup npm run dev -- --port "$AETHER_PORT" --host 0.0.0.0 --base /aether/ > "$AETHER_LOG" 2>&1 &

  local pid=$!
  echo "$pid" > "$AETHER_PIDFILE"

  log "   Aether dev server PID: $pid (logs: $AETHER_LOG)"
  log "   Direct: http://localhost:$AETHER_PORT"
  log "   Via proxy: http://localhost/aether/"

  # Ensure shared folders for seamless Aether <-> AudioMass handoff (exports for WAVs, samples for roundtrips)
  mkdir -p "$PROJECT_ROOT/exports" "$PROJECT_ROOT/samples"
  log "   Shared handoff folders ready: $PROJECT_ROOT/exports and $PROJECT_ROOT/samples"
}

start_audiomass() {
  log "→ Starting AudioMass API (rebuilt backend) on :$AUDIOMASS_PORT ..."

  # The rebuilt backend (backend/, FastAPI) — served by mass-backend.service.
  # The old audiomass.service + audiomass/backend/ were scrapped at cutover
  # (2026-08-27). See API-CONTRACT.md for what this API serves.
  local SVC="$HOME/.config/systemd/user/mass-backend.service"
  if command -v systemctl &>/dev/null && [ -f "$SVC" ]; then
    systemctl --user start mass-backend.service 2>/dev/null && {
      log "   AudioMass API started via systemd (mass-backend.service)"
      log "   Direct: http://localhost:$AUDIOMASS_PORT   (consumers: mixer :5058, Aether bounce)"
      return 0
    }
    log "   systemd start failed, falling back to nohup"
  else
    log "   systemd unit missing ($SVC) — copy systemd/mass-backend.service from the repo"
  fi

  # Fallback: nohup (same env as the unit)
  if [ ! -x "$PROJECT_ROOT/backend/.venv/bin/python" ]; then
    log "ERROR: backend/.venv missing — bootstrap it (see backend/README.md)"
    return 1
  fi

  if [ -f "$AUDIOMASS_PIDFILE" ]; then
    kill "$(cat "$AUDIOMASS_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$AUDIOMASS_PIDFILE"
  fi
  pkill -f "uvicorn.*$AUDIOMASS_PORT" 2>/dev/null || true

  nohup env AUDIOMASS_PORT="$AUDIOMASS_PORT" AUDIOMASS_DEMUCS_DOCKER_IMAGE="$DEMUCS_IMAGE" \
    "$PROJECT_ROOT/backend/.venv/bin/python" -m uvicorn app:app \
    --host 0.0.0.0 --port "$AUDIOMASS_PORT" --app-dir backend > "$AUDIOMASS_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$AUDIOMASS_PIDFILE"

  log "   AudioMass API PID: $pid (logs: $AUDIOMASS_LOG)"
  log "   http://localhost:$AUDIOMASS_PORT"
}

start_dj_toolkit() {
  log "→ Starting DJ Toolkit (Stems / BPM-Key / Vocal Remover / MP3→MIDI) on :$DJ_TOOLKIT_PORT ..."

  if [ ! -d "$DJ_TOOLKIT_DIR" ]; then
    log "ERROR: dj_toolkit/ not found at $DJ_TOOLKIT_DIR — skipping (keep it locally next to audiomass/)."
    return 1
  fi
  local venv_py="$PROJECT_ROOT/backend/.venv/bin/python"
  if [ ! -x "$venv_py" ]; then
    log "ERROR: $venv_py missing — DJ Toolkit needs the backend venv (flask + basic-pitch). Skipping."
    return 1
  fi

  # Clean prior instance
  if [ -f "$DJ_TOOLKIT_PIDFILE" ]; then
    kill "$(cat "$DJ_TOOLKIT_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DJ_TOOLKIT_PIDFILE"
  fi
  pkill -f "backend/.venv/bin/python app.py" 2>/dev/null || true

  cd "$DJ_TOOLKIT_DIR"
  nohup env DJ_TOOLKIT_PORT="$DJ_TOOLKIT_PORT" "$venv_py" app.py > "$DJ_TOOLKIT_LOG" 2>&1 &

  local pid=$!
  echo "$pid" > "$DJ_TOOLKIT_PIDFILE"

  log "   DJ Toolkit PID: $pid (logs: $DJ_TOOLKIT_LOG)"
  log "   Direct: http://localhost:$DJ_TOOLKIT_PORT"
}

start_music_tools() {
  log "→ Starting Music Tools (Melody Generator + Audio→Sheet) on :$MUSIC_TOOLS_PORT ..."

  if [ ! -f "$MUSIC_TOOLS_DIR/run.sh" ]; then
    log "ERROR: music-tools/run.sh not found at $MUSIC_TOOLS_DIR — skipping."
    return 1
  fi

  # Clean prior instance
  if [ -f "$MUSIC_TOOLS_PIDFILE" ]; then
    kill "$(cat "$MUSIC_TOOLS_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$MUSIC_TOOLS_PIDFILE"
  fi
  pkill -f "http.server $MUSIC_TOOLS_PORT" 2>/dev/null || true

  nohup env MUSIC_TOOLS_PORT="$MUSIC_TOOLS_PORT" "$MUSIC_TOOLS_DIR/run.sh" > "$MUSIC_TOOLS_LOG" 2>&1 &

  local pid=$!
  echo "$pid" > "$MUSIC_TOOLS_PIDFILE"

  log "   Music Tools PID: $pid (logs: $MUSIC_TOOLS_LOG)"
  log "   Direct: http://localhost:$MUSIC_TOOLS_PORT/melody-generator.html  (and /audio-to-sheet.html)"
}

start_melody_suite() {
  log "→ Starting Melody Suite (interactive sheet editor / BPM-key / SATB / MP3→MIDI) on :$MELODY_SUITE_PORT ..."

  if [ ! -d "$MELODY_SUITE_DIR" ]; then
    log "ERROR: melody-suite/ not found at $MELODY_SUITE_DIR — skipping."
    return 1
  fi

  local venv_py="$MELODY_SUITE_DIR/.venv/bin/python"
  if [ ! -x "$venv_py" ]; then
    log "   melody-suite venv missing — creating it (one-time bootstrap, ~1-2 min)..."
    python3 -m venv "$MELODY_SUITE_DIR/.venv"
    "$MELODY_SUITE_DIR/.venv/bin/pip" install --quiet -r "$MELODY_SUITE_DIR/requirements.txt"
  fi

  # Clean prior instance
  if [ -f "$MELODY_SUITE_PIDFILE" ]; then
    kill "$(cat "$MELODY_SUITE_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$MELODY_SUITE_PIDFILE"
  fi
  pkill -f "melody-suite/.venv/bin/python app.py" 2>/dev/null || true

  cd "$MELODY_SUITE_DIR"
  nohup env MELODY_SUITE_PORT="$MELODY_SUITE_PORT" "$venv_py" app.py > "$MELODY_SUITE_LOG" 2>&1 &

  local pid=$!
  echo "$pid" > "$MELODY_SUITE_PIDFILE"

  log "   Melody Suite PID: $pid (logs: $MELODY_SUITE_LOG)"
  log "   Direct: http://localhost:$MELODY_SUITE_PORT  (editor: /tools/melody-sheet/interactive-sheet-music-editor-playback)"
}

start_mixer() {
  log "→ Starting Stem Mixer (static, :$MIXER_PORT) via systemd user unit..."

  if [ ! -d "$MIXER_DIR" ]; then
    log "ERROR: mixer/ not found at $MIXER_DIR — skipping."
    return 1
  fi

  if ! command -v systemctl &>/dev/null; then
    log "   systemctl unavailable — start manually: cd $MIXER_DIR && python3 -m http.server $MIXER_PORT"
    return 1
  fi

  systemctl --user daemon-reload 2>/dev/null || true
  if ! systemctl --user start "$MIXER_SERVICE" 2>/dev/null; then
    log "   WARNING: could not start $MIXER_SERVICE (is ~/.config/systemd/user/mixer.service present?)."
    log "   Manual: cd $MIXER_DIR && python3 -m http.server $MIXER_PORT --bind 0.0.0.0"
    return 1
  fi
  log "   Stem Mixer: http://localhost:$MIXER_PORT  (proxy: http://localhost/mixer)"
  log "   API: talks to AudioMass REST on :5055 directly (stems/jobs)."
}

stop_mixer() {
  log "Stopping Stem Mixer..."
  systemctl --user stop "$MIXER_SERVICE" 2>/dev/null || true
  pkill -f "http.server $MIXER_PORT" 2>/dev/null || true
  log "  Stem Mixer stopped."
}

start_webui() {
  # Open WebUI (LLM hub / chat / tools) — venv app on :3000, served as the Caddy
  # catch-all route (http://localhost/). Uses the existing ~/webui scripts.
  # Soft: if the scripts/venv are missing we log and continue.
  if [ ! -x "$WEBUI_START_SCRIPT" ]; then
    log "   ~/webui/start-webui.sh not found — Open WebUI skipped."
    return 1
  fi
  if [ ! -x "$WEBUI_APP_VENV" ]; then
    log "   open-webui venv missing at $WEBUI_APP_VENV — Open WebUI skipped."
    return 1
  fi
  log "→ Starting Open WebUI (LLM hub) on :$WEBUI_PORT ..."

  # Clean prior instance (also catches a manually-started open-webui on :3000)
  if [ -f "$WEBUI_PIDFILE" ]; then
    kill "$(cat "$WEBUI_PIDFILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$WEBUI_PIDFILE"
  fi
  pkill -f "open-webui.*serve --port $WEBUI_PORT" 2>/dev/null || true

  # start-webui.sh runs `open-webui serve` as its foreground process, so the script's
  # PID is what we track; its SIGTERM trap cleans up the tool server (:8000) on stop.
  nohup bash "$WEBUI_START_SCRIPT" > "$WEBUI_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$WEBUI_PIDFILE"

  log "   Open WebUI PID: $pid (logs: $WEBUI_LOG)"
  log "   Direct: http://localhost:$WEBUI_PORT"
  log "   Via proxy: http://localhost/  (Caddy catch-all route)"
  log "   First boot: pip install + DB migrations (~30-60s) + Open Terminal docker image pull (first time only, can take minutes). Ready when :3000 answers; also starts tool server :8000 + Open Terminal :8001."
}

start_llama_gpu() {
  # Aether's local LLM on the GPU (Vulkan llama.cpp — see start-llama-gpu.sh).
  # Soft: if the binary/model/drive are missing we log and continue — Aether
  # falls back to Ollama (CPU) then the local generators.
  if [ ! -x "$LLAMA_GPU_SCRIPT" ]; then
    log "   start-llama-gpu.sh not found — GPU model server skipped (Aether will use Ollama fallback)."
    return 1
  fi
  log "→ Starting GPU LLM server (llama.cpp Vulkan, Qwen3-8B) ..."
  "$LLAMA_GPU_SCRIPT" start 2>&1 | while read -r l; do log "   $l"; done
}

stop_llama_gpu() {
  if [ -x "$LLAMA_GPU_SCRIPT" ]; then
    log "Stopping GPU LLM server..."
    "$LLAMA_GPU_SCRIPT" stop 2>&1 | while read -r l; do log "   $l"; done
  fi
}

start_seed_llm() {
  # Cloud-LLM seed for fresh browsers (see seed-llm-config.sh). Writes
  # public/llm-seed.html + public/llm-seed.json; Aether auto-applies the JSON
  # on load when the browser has no saved config (manual config always wins).
  # Soft: if the 9router DB or the script is missing we log and continue.
  if [ ! -x "$SEED_LLM_SCRIPT" ]; then
    log "   seed-llm-config.sh not found — cloud-LLM seed skipped."
    return 1
  fi
  log "→ Writing cloud-LLM seed (9router) for fresh browsers ..."
  "$SEED_LLM_SCRIPT" seed --no-open 2>&1 | while read -r l; do log "   $l"; done
}

stop_seed_llm() {
  # Remove the key-bearing seed files from public/ on shutdown.
  if [ -x "$SEED_LLM_SCRIPT" ]; then
    log "Removing cloud-LLM seed files (gateway key) ..."
    "$SEED_LLM_SCRIPT" clean 2>&1 | while read -r l; do log "   $l"; done
  fi
}

stop_aether() {
  log "Stopping Aether..."
  if [ -f "$AETHER_PIDFILE" ]; then
    local pid
    pid=$(cat "$AETHER_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$AETHER_PIDFILE"
  fi
  pkill -f "vite.*$AETHER_PORT" 2>/dev/null || true
  pkill -f -- "--port $AETHER_PORT" 2>/dev/null || true
  log "  Aether stopped."
}

stop_audiomass() {
  log "Stopping AudioMass API (mass-backend)..."
  # Stop systemd service if present
  if command -v systemctl &>/dev/null && systemctl --user is-active mass-backend.service &>/dev/null; then
    systemctl --user stop mass-backend.service 2>/dev/null || true
  fi
  # Also kill any orphaned processes
  if [ -f "$AUDIOMASS_PIDFILE" ]; then
    local pid
    pid=$(cat "$AUDIOMASS_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$AUDIOMASS_PIDFILE"
  fi
  pkill -f "uvicorn.*$AUDIOMASS_PORT" 2>/dev/null || true
  log "  AudioMass API stopped."
}

stop_dj_toolkit() {
  log "Stopping DJ Toolkit..."
  if [ -f "$DJ_TOOLKIT_PIDFILE" ]; then
    local pid
    pid=$(cat "$DJ_TOOLKIT_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$DJ_TOOLKIT_PIDFILE"
  fi
  pkill -f "backend/.venv/bin/python app.py" 2>/dev/null || true
  log "  DJ Toolkit stopped."
}

stop_music_tools() {
  log "Stopping Music Tools..."
  if [ -f "$MUSIC_TOOLS_PIDFILE" ]; then
    local pid
    pid=$(cat "$MUSIC_TOOLS_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$MUSIC_TOOLS_PIDFILE"
  fi
  pkill -f "http.server $MUSIC_TOOLS_PORT" 2>/dev/null || true
  log "  Music Tools stopped."
}

stop_melody_suite() {
  log "Stopping Melody Suite..."
  if [ -f "$MELODY_SUITE_PIDFILE" ]; then
    local pid
    pid=$(cat "$MELODY_SUITE_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$MELODY_SUITE_PIDFILE"
  fi
  pkill -f "melody-suite/.venv/bin/python app.py" 2>/dev/null || true
  log "  Melody Suite stopped."
}

stop_webui() {
  log "Stopping Open WebUI... (via ~/webui/stop-webui.sh)"
  # Kill the tracked start-webui.sh wrapper first so its trap stops the tool server
  if [ -f "$WEBUI_PIDFILE" ]; then
    local pid
    pid=$(cat "$WEBUI_PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$WEBUI_PIDFILE"
  fi
  # The existing script pkills open-webui serve (catches orphans + manual starts) and the tool server
  if [ -x "$WEBUI_STOP_SCRIPT" ]; then
    bash "$WEBUI_STOP_SCRIPT" 2>&1 | while read -r l; do log "   $l"; done
  else
    pkill -f "open-webui.*serve" 2>/dev/null || true
    pkill -f "python main.py" 2>/dev/null || true
  fi
  log "  Open WebUI stopped."
}

stop_all() {
  stop_aether
  stop_audiomass
  stop_dj_toolkit
  stop_music_tools
  stop_melody_suite
  stop_mixer
  stop_webui
  stop_llama_gpu
  stop_seed_llm
  stop_caddy
  log "All (Aether + AudioMass + DJ Toolkit + Music Tools + Melody Suite + Open WebUI + GPU LLM + Caddy) stopped."
}

status() {
  echo "========================================"
  log "=== STATUS: Aether + AudioMass + Caddy (Pandora unified) ==="
  echo "Project root: $PROJECT_ROOT"
  echo "Caddy config: $CADDY_CONFIG"
  echo "Timestamp: $(date)"
  echo ""

  # --- Caddy ---
  echo "Caddy reverse proxy:"
  local cpid
  cpid=$(get_caddy_pid || true)
  if [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null; then
    echo "  RUNNING   pid=$cpid"
    echo "  pidfile : $CADDY_PIDFILE"
    echo "  logfile : $CADDY_LOG"
    if [ -f "$CADDY_LOG" ]; then
      echo "  recent log:"
      tail -n 4 "$CADDY_LOG" 2>/dev/null | sed 's/^/    /'
    fi
  else
    echo "  NOT RUNNING (via our pidfile/config)"
    if pgrep -f caddy >/dev/null 2>&1; then
      echo "  (Other caddy proc(s) exist:)"
      pgrep -af caddy 2>/dev/null | head -3 | sed 's/^/    /'
    fi
  fi
  if ss -tlnp 2>/dev/null | grep -q '[: ]80[ ]'; then
    echo "  :80 listening: YES"
  else
    echo "  :80 listening: NO"
  fi
  echo ""

  # --- Aether ---
  echo "Aether (npm run dev :$AETHER_PORT , --base /aether/):"
  local apid=""
  [ -f "$AETHER_PIDFILE" ] && apid=$(cat "$AETHER_PIDFILE" 2>/dev/null || true)
  if [ -n "$apid" ] && kill -0 "$apid" 2>/dev/null; then
    echo "  RUNNING   pid=$apid (pidfile)"
  elif pgrep -f "vite.*$AETHER_PORT" >/dev/null 2>&1 || pgrep -f -- "--port $AETHER_PORT" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match, pidfile stale?)"
    pgrep -af 'vite|npm run dev' 2>/dev/null | grep -E "(5173|$AETHER_PORT)" | head -2 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$AETHER_PORT[ ]"; then
    echo "  :$AETHER_PORT listening: YES"
  fi
  echo "  logfile : $AETHER_LOG"
  echo ""

  # --- AudioMass ---
  echo "AudioMass (backend on :$AUDIOMASS_PORT):"
  local mpid=""
  [ -f "$AUDIOMASS_PIDFILE" ] && mpid=$(cat "$AUDIOMASS_PIDFILE" 2>/dev/null || true)
  if [ -n "$mpid" ] && kill -0 "$mpid" 2>/dev/null; then
    echo "  RUNNING   pid=$mpid (pidfile)"
  elif pgrep -f "audiomass-server.py" >/dev/null 2>&1 || pgrep -f "uvicorn.*$AUDIOMASS_PORT" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match)"
    pgrep -af "audiomass-server.py\|uvicorn" 2>/dev/null | grep "$AUDIOMASS_PORT" | head -1 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$AUDIOMASS_PORT[ ]"; then
    echo "  :$AUDIOMASS_PORT listening: YES"
  fi
  echo "  logfile : $AUDIOMASS_LOG"
  echo ""

  # --- DJ Toolkit ---
  echo "DJ Toolkit (flask :$DJ_TOOLKIT_PORT — stems / BPM-key / vocal remover / MP3→MIDI):"
  local dpid=""
  [ -f "$DJ_TOOLKIT_PIDFILE" ] && dpid=$(cat "$DJ_TOOLKIT_PIDFILE" 2>/dev/null || true)
  if [ -n "$dpid" ] && kill -0 "$dpid" 2>/dev/null; then
    echo "  RUNNING   pid=$dpid (pidfile)"
  elif pgrep -f "backend/.venv/bin/python app.py" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match)"
    pgrep -af "app.py" 2>/dev/null | grep -i dj_toolkit | head -1 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$DJ_TOOLKIT_PORT[ ]"; then
    echo "  :$DJ_TOOLKIT_PORT listening: YES"
  fi
  echo "  logfile : $DJ_TOOLKIT_LOG"
  echo ""

  # --- Music Tools ---
  echo "Music Tools (static :$MUSIC_TOOLS_PORT — melody generator / audio→sheet):"
  local tpid=""
  [ -f "$MUSIC_TOOLS_PIDFILE" ] && tpid=$(cat "$MUSIC_TOOLS_PIDFILE" 2>/dev/null || true)
  if [ -n "$tpid" ] && kill -0 "$tpid" 2>/dev/null; then
    echo "  RUNNING   pid=$tpid (pidfile)"
  elif pgrep -f "http.server $MUSIC_TOOLS_PORT" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match)"
    pgrep -af "http.server $MUSIC_TOOLS_PORT" 2>/dev/null | head -1 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$MUSIC_TOOLS_PORT[ ]"; then
    echo "  :$MUSIC_TOOLS_PORT listening: YES"
  fi
  echo "  logfile : $MUSIC_TOOLS_LOG"
  echo ""

  # --- Melody Suite ---
  echo "Melody Suite (flask :$MELODY_SUITE_PORT — interactive sheet editor / BPM-key / SATB / MP3→MIDI):"
  local mp2pid=""
  [ -f "$MELODY_SUITE_PIDFILE" ] && mp2pid=$(cat "$MELODY_SUITE_PIDFILE" 2>/dev/null || true)
  if [ -n "$mp2pid" ] && kill -0 "$mp2pid" 2>/dev/null; then
    echo "  RUNNING   pid=$mp2pid (pidfile)"
  elif pgrep -f "melody-suite/.venv/bin/python app.py" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match)"
    pgrep -af "melody-suite/.venv/bin/python app.py" 2>/dev/null | head -1 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$MELODY_SUITE_PORT[ ]"; then
    echo "  :$MELODY_SUITE_PORT listening: YES"
  fi
  echo "  logfile : $MELODY_SUITE_LOG"
  echo ""

  # --- Open WebUI ---
  echo "Open WebUI (venv :$WEBUI_PORT — LLM hub / chat / tools; Caddy catch-all):"
  local wpid=""
  [ -f "$WEBUI_PIDFILE" ] && wpid=$(cat "$WEBUI_PIDFILE" 2>/dev/null || true)
  if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
    echo "  RUNNING   pid=$wpid (pidfile — start-webui.sh wrapper)"
  elif pgrep -f "open-webui.*serve" >/dev/null 2>&1; then
    echo "  RUNNING   (pgrep match)"
    pgrep -af "open-webui.*serve" 2>/dev/null | head -1 | sed 's/^/    /'
  else
    echo "  NOT RUNNING"
  fi
  if ss -tlnp 2>/dev/null | grep -q ":$WEBUI_PORT[ ]"; then
    echo "  :$WEBUI_PORT listening: YES"
  fi
  echo "  logfile : $WEBUI_LOG"
  echo ""

  # --- Proxy tests ---
  echo "Quick proxy tests (http://localhost via Caddy, 2s timeout):"
  for path in / /aether/ /mass /audiomass; do
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "http://localhost${path}" 2>/dev/null || echo "000/ERR")
    echo "  http://localhost${path}  -> HTTP $code"
  done
  echo ""
  echo "Tip: Use direct ports if proxy is down. Workflow: generate in /aether -> export .wav -> edit in /mass"
  echo "========================================"
}

# --- main dispatch ---

case "${1:-start}" in
  start)
    guard_pandora_mount || true
    ensure_caddy || log "   Caddy not available — apps still work on direct ports."
    start_llama_gpu || log "   GPU LLM server skipped — Aether will use Ollama/CPU fallback."
    start_seed_llm || log "   Cloud-LLM seed skipped — no 9router gateway key available."
    start_aether_dev
    start_audiomass
    start_dj_toolkit || log "   DJ Toolkit skipped."
    start_music_tools || log "   Music Tools skipped."
    start_melody_suite || log "   Melody Suite skipped."
    start_mixer || log "   Stem Mixer skipped."
    start_webui || log "   Open WebUI skipped."
    echo ""
    log "Aether + AudioMass + DJ Toolkit + Music Tools + Melody Suite + Stem Mixer + Open WebUI + GPU LLM + Caddy ready — the full creative stack."
    log "Recommended unified access (Caddy on :80):"
    log "  http://localhost/          → Open WebUI (LLM hub / chat / tools)"
    log "  http://localhost/aether/   → Aether (AI music generation + sequencer, hot reload)"
    log "    NOTE: the /aether* proxy route passes the path through UNTOUCHED — Vite runs --base /aether/, so a uri strip_prefix would make Vite receive / and 302-loop. Use the trailing slash (http://localhost/aether/); a bare /aether 404s from Vite's base check. Edit /mnt/Pandora/caddy/Caddyfile (then $0 caddy-restart)."
    log "    NOTE: Open WebUI is the Caddy catch-all — the Caddyfile ends with a matcher-less handle (localhost:3000). handle / matches ONLY the root and would break OWUI's /static + /api paths; add new prefix routes ABOVE the catch-all."
    log "  http://localhost/mass     → AudioMass (multitrack waveform editor)"
    log "  http://localhost/melody   → Melody Suite (sheet editor / SATB harmony / MP3→MIDI)"
    log "  http://localhost/mixer    → Stem Mixer (load AudioMass jobs / mix stems / export WAV)"
    log "Direct ports (no proxy needed):"
    log "  http://localhost:5001     → DJ Toolkit (stems / BPM-key / vocal remover / MP3→MIDI)"
    log "  http://localhost:8091     → Music Tools (melody generator / audio-to-sheet)"
    log "  http://localhost:$MELODY_SUITE_PORT     → Melody Suite (interactive sheet editor / SATB harmony / MP3→MIDI)"
    log "  http://localhost:$MIXER_PORT     → Stem Mixer direct (5060 is browser-blocked: ERR_UNSAFE_PORT)"
    log "  http://localhost:11435    → GPU LLM server (llama.cpp Vulkan — Aether AI on the RX 580)"
    log "  /aether/llm-seed.json     → cloud-LLM seed for fresh browsers (auto-applied on load; cleaned on stop)"
    log ""
    log "Handoff (no gaps): In Aether use BOUNCE buttons for stems/full WAV (named aether-*-to-audiomass.wav)."
    log "  Drop to $PROJECT_ROOT/exports/ or /mnt/Pandora/Music/Audiamass/_incoming/ for instant import."
    log "  Load back in Aether via file input in the bounce section for preview/noise boost."
    log "  Exports and samples folders created automatically for roundtrips."
    log ""
    log "Other (if running): http://localhost/comfy → ComfyUI; http://localhost/ollama → Ollama (see Caddyfile)"
    log "Stop:   $0 stop"
    log "Status: $0 status"
    log "Restart whole: $0 restart"
    ;;
  stop)
    stop_all
    ;;
  restart)
    log "Restarting everything..."
    guard_pandora_mount || true
    stop_all
    sleep 1
    ensure_caddy || log "   Caddy not available — apps still work on direct ports."
    start_llama_gpu || log "   GPU LLM server skipped."
    start_seed_llm || log "   Cloud-LLM seed skipped."
    start_aether_dev
    start_audiomass
    start_dj_toolkit || log "   DJ Toolkit skipped."
    start_music_tools || log "   Music Tools skipped."
    start_melody_suite || log "   Melody Suite skipped."
    start_mixer || log "   Stem Mixer skipped."
    start_webui || log "   Open WebUI skipped."
    log "Restart complete."
    ;;
  status)
    status
    ;;
  build-aether)
    log "→ Building Aether (static) with --base /aether/ for subpath proxy compatibility..."
    cd "$AETHER_DIR"
    rm -rf dist
    npm run build -- --base /aether/
    log "Build complete → dist/"
    log "To use via proxy as static: edit Caddyfile /aether block to file_server (uncomment static, comment reverse_proxy), then $0 caddy-restart or restart caddy manually."
    ;;
  caddy-restart|caddy-reload)
    log "→ Forcing Caddy stop + start (picks up any Caddyfile edits)..."
    stop_caddy_internal
    sleep 1
    ensure_caddy
    log "Caddy restarted."
    ;;
  dry-run|check)
    dry_run
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|dry-run|build-aether|caddy-restart}"
    echo ""
    echo "  start          Start Caddy (smart) + GPU LLM + Aether dev + AudioMass + DJ Toolkit + Music Tools + Melody Suite + Open WebUI"
    echo "  stop           Stop everything + our Caddy instance"
    echo "  restart        stop + start"
    echo "  status         Show pids, logs tails, listeners, quick curl tests against proxy"
    echo "  dry-run        Check all dependencies without starting anything"
    echo "  build-aether   Build with correct base for /aether serving"
    echo "  caddy-restart  Stop/start only the Caddy (e.g. after you edited Caddyfile)"
    exit 1
    ;;
esac
