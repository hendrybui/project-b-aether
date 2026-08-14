#!/usr/bin/env bash
# start-llama-gpu.sh
# Run Aether's local LLM on the RX 580 (Vulkan) via the llama.cpp server that
# Jan AI already ships on this machine — no GPU clash with demucs (which only
# uses the GPU during stem separation, and both fit in 7.75 GiB VRAM: the
# Qwen3-8B Q4 model is ~4.9 GB, htdemucs ~2-3 GB).
#
# Why: Ollama runs the 8B model on the 4-core CPU (~4 tok/s), which starves
# the browser-audio thread and makes Aether's sound stutter. llama.cpp's Vulkan
# backend on the same model does ~30 tok/s with the GPU idle otherwise.
#
# Usage: start-llama-gpu.sh {start|status|stop|restart}
#   start    boot the server (idempotent; refuses if the drive is misplaced)
#   status   health + pid + model + listen address
#   stop     graceful TERM (pid from /tmp/llama-server.pid)
#
# Overrides (env): LLAMA_BIN (path to a llama-server binary),
#                  LLAMA_MODEL (path to a .gguf), LLAMA_PORT (default 11435)
#
# Aether's bridge (src/ai/ollama.ts) calls this server's OpenAI-compatible
# endpoint first, then falls back to Ollama on :11434, then to the local
# generators. Model name in the request is ignored — the server serves
# whatever GGUF it was started with.

set -uo pipefail

GUARD="${GUARD:-$(cd "$(dirname "$0")" && pwd)/check-pandora-mount.sh}"
PORT="${LLAMA_PORT:-11435}"
HOST="127.0.0.1"
CTX=4096
PIDFILE=/tmp/llama-server.pid
LOG=/tmp/llama-server.log

# --- Resolve binary + model (overridable, else newest Jan AI vulkan build) ---
BACKENDS_DIR="/mnt/Pandora/Jan_ai/llamacpp/backends"
MODELS_DIR="/mnt/Pandora/Jan_ai/llamacpp/models"

find_bin() {
  # newest build dir that contains a linux-vulkan-x64 build with llama-server
  for b in $(ls -1d "$BACKENDS_DIR"/b* 2>/dev/null | sort -V -r); do
    local cand="$b/linux-vulkan-x64/build/bin/llama-server"
    [ -x "$cand" ] && { echo "$cand"; return 0; }
  done
  return 1
}

find_model() {
  local cand
  for cand in \
    "$MODELS_DIR/Qwen3-8B-Q4_K_M.gguf" \
    "$MODELS_DIR"/*.gguf; do
    [ -f "$cand" ] && { echo "$cand"; return 0; }
  done
  return 1
}

BIN="${LLAMA_BIN:-$(find_bin || true)}"
MODEL="${LLAMA_MODEL:-$(find_model || true)}"

if [ -z "$BIN" ] || [ -z "$MODEL" ]; then
  echo "[llama-gpu] ERROR: could not locate llama-server binary and/or GGUF model." >&2
  echo "  binary: $BIN" >&2
  echo "  model:  $MODEL" >&2
  echo "  Set LLAMA_BIN / LLAMA_MODEL to override." >&2
  exit 1
fi

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

status() {
  if is_running; then
    pid=$(cat "$PIDFILE")
    if curl -s --max-time 3 "http://$HOST:$PORT/health" 2>/dev/null | grep -q ok; then
      echo "[llama-gpu] RUNNING  pid=$pid  http://$HOST:$PORT  model=$(basename "$MODEL")"
      return 0
    fi
    echo "[llama-gpu] STALE pid file ($pid) — server not answering; use 'stop' then 'start'."
    return 1
  fi
  echo "[llama-gpu] stopped."
  return 1
}

start() {
  if is_running; then
    echo "[llama-gpu] already running (pid $(cat "$PIDFILE")) — nothing to do."
    status
    return 0
  fi
  # Drive guard first — everything lives on /mnt/Pandora.
  if [ -x "$GUARD" ] && ! "$GUARD" >/dev/null 2>&1; then
    echo "[llama-gpu] ABORT: $GUARD failed (drive misplaced?)." >&2
    "$GUARD" >&2
    return 1
  fi

  local bdir; bdir=$(dirname "$(dirname "$(dirname "$BIN")")")
  setsid nohup env LD_LIBRARY_PATH="$(dirname "$BIN")" \
    "$BIN" -m "$MODEL" \
      --host "$HOST" --port "$PORT" \
      -ngl 99 -c "$CTX" \
      --reasoning-budget 0 \
      --no-webui \
    > "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  disown

  echo "[llama-gpu] starting (pid $!): $(basename "$BIN") + $(basename "$MODEL") — waiting for model load..."
  local i
  for i in $(seq 1 30); do
    if curl -s --max-time 2 "http://$HOST:$PORT/health" 2>/dev/null | grep -q ok; then
      echo "[llama-gpu] READY after ~$((i * 5))s — Aether will use the GPU model."
      return 0
    fi
    sleep 5
  done
  echo "[llama-gpu] WARN: not answering after 150s — see $LOG" >&2
  return 1
}

stop() {
  if ! is_running; then
    echo "[llama-gpu] not running."
    rm -f "$PIDFILE"
    return 0
  fi
  local pid; pid=$(cat "$PIDFILE")
  kill "$pid" 2>/dev/null
  local i
  for i in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -0 "$pid" 2>/dev/null && { echo "[llama-gpu] forced kill"; kill -9 "$pid"; }
  rm -f "$PIDFILE"
  echo "[llama-gpu] stopped (was pid $pid)."
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  *) echo "usage: $0 {start|status|stop|restart}"; exit 2 ;;
esac
