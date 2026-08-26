#!/usr/bin/env bash
# check-pandora-mount.sh
# Guard: is the Pandora drive mounted at /mnt/Pandora with Project-B reachable?
#
# Every script in this workspace (launcher, demucs GPU check, docker data-root
# at /mnt/Pandora/docker-lib, the Caddy config) hardcodes /mnt/Pandora/... — so
# a drive that mounts elsewhere (udisks auto-mount, fstab edit, drive crash)
# breaks everything SILENTLY. This guard makes the change loud and gives the
# exact fix.
#
# Installed copies (survive the drive being gone, run from the ROOT filesystem):
#   /usr/local/bin/check-pandora-mount.sh        (used by the systemd nightly)
#   ~/bin/check-pandora-mount.sh or /home/neocyan/ (manual use if drive moved)
# The repo copy at /mnt/Pandora/Project-B/ is the source of truth — sync any
# installed copy with it after edits (cp, then verify both behave identically).
#
# Exit 0 = healthy. Exit 1 = drive missing or misplaced (with fix hints).
# Read-only, idempotent, safe to run anytime.
#
# Place / run from: anywhere. Usage: check-pandora-mount.sh

set -uo pipefail

EXPECTED="/mnt/Pandora"
LABEL="Pandora"
PROJECT_DIR="$EXPECTED/Project-B"

say_ok()   { echo "[mount-check] OK: $*"; }
say_fail() { echo "[mount-check] FAIL: $*" >&2; }

# --- 1. Healthy: /mnt/Pandora is a real mountpoint AND Project-B is there ---
if findmnt -n "$EXPECTED" >/dev/null 2>&1 && [ -d "$PROJECT_DIR" ]; then
  src=$(findmnt -n -o SOURCE "$EXPECTED" 2>/dev/null || echo "?")
  say_ok "Pandora mounted at $EXPECTED ($src), $PROJECT_DIR reachable."
  exit 0
fi

# --- 2. Diagnose: where did the Pandora drive actually go? -------------------
actual=""
if command -v findmnt >/dev/null 2>&1; then
  actual=$(findmnt -rn -o TARGET -S "LABEL=$LABEL" 2>/dev/null | head -1)
fi
# Fallback for odd cases: scan udisks' usual spots for Project-B itself.
if [ -z "$actual" ]; then
  for m in /media/*/Pandora /run/media/*/Pandora; do
    [ -d "$m/Project-B" ] && actual="$m" && break
  done
fi

if [ -n "$actual" ] && [ "$actual" != "$EXPECTED" ]; then
  say_fail "Pandora drive is mounted at '$actual', not '$EXPECTED'."
  echo "  Every script expects $EXPECTED — nothing will work until this is fixed." >&2
  echo "  Fix (restore the fstab entry and remount):" >&2
  echo "    sudo sed -i 's|^#LABEL=Pandora|LABEL=Pandora|' /etc/fstab" >&2
  echo "    sudo umount \"$actual\"; sudo mount -a     # or simply reboot" >&2
  exit 1
fi

if findmnt -n "$EXPECTED" >/dev/null 2>&1; then
  say_fail "$EXPECTED is a mountpoint but $PROJECT_DIR is missing — is the WRONG drive mounted there?"
else
  say_fail "The Pandora drive is not mounted anywhere."
  echo "  Plug in the drive, then: sudo mount -a   (or reboot)" >&2
fi
exit 1
