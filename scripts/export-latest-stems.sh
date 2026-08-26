#!/bin/bash
# export-latest-stems.sh — copy the newest COMPLETED separation job's stems
# into a DAW-ready folder (exports/stems-latest/), with title-case file names.
# In-progress jobs (manifest status != done) are skipped, so a running
# separation can never export half-written stems.
#
# Usage:
#   ./export-latest-stems.sh            # export latest completed job
#   ./export-latest-stems.sh <job_id>   # export a specific job (any status)
#
# One-liner (latest completed job):
#   rm -rf exports/stems-latest && mkdir -p exports/stems-latest && latest=$(for d in /mnt/Pandora/Music/Audiamass/*/; do [ -f "$d/manifest.json" ] || continue; s=$(python3 -c "import json;print(json.load(open('$d/manifest.json')).get('status',''))" 2>/dev/null); [ "$s" = done ] && echo "$d"; done | sort -r | head -1) && for f in "$latest"stems/*.wav; do n=$(basename "$f"); n="${n^}"; cp "$f" "exports/stems-latest/${n%.wav}.wav"; done && echo "Stems exported to exports/stems-latest/ ($(ls exports/stems-latest | wc -l) files)"

set -e
cd "$(dirname "$0")" || exit 1

JOBS_DIR="/mnt/Pandora/Music/Audiamass"
OUT_DIR="exports/stems-latest"

if [ -n "$1" ]; then
    JOB_DIR="$JOBS_DIR/$1"
    if [ ! -d "$JOB_DIR/stems" ]; then
        echo "No stems found for job $1." >&2
        exit 1
    fi
else
    # Newest job whose manifest says status=done (skips in-progress/failed).
    JOB_DIR=""
    while IFS= read -r d; do
        [ -f "$d/manifest.json" ] || continue
        s=$(python3 -c "import json;print(json.load(open('$d/manifest.json')).get('status',''))" 2>/dev/null)
        if [ "$s" = "done" ]; then
            JOB_DIR="$d"
            break
        fi
    done < <(ls -dt "$JOBS_DIR"/*/ 2>/dev/null | grep -vE "/(_incoming|_pool|projects)/")
    if [ -z "$JOB_DIR" ] || [ ! -d "$JOB_DIR/stems" ]; then
        echo "No completed separation job found (looked in $JOBS_DIR)." >&2
        exit 1
    fi
fi

JOB_ID=$(basename "$JOB_DIR")
echo "Exporting stems from job $JOB_ID ($JOB_DIR)"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

N=0
for f in "$JOB_DIR"/stems/*.wav; do
    [ -e "$f" ] || continue
    name=$(basename "$f")
    name="${name^}"                     # title-case: vocals.wav -> Vocals.wav
    cp "$f" "$OUT_DIR/${name%.wav}.wav" # keep .wav extension
    N=$((N + 1))
done

echo "Done: $N stems exported to $OUT_DIR"
echo "  -> $(cd "$OUT_DIR" && pwd)"
