#!/usr/bin/env bash
# Legacy-records proof of concept (PROTOTYPE, synthetic data only).
#
# Runs both reference workflows against the two mock desktop apps on an X
# display, then checks every exchange the runs wrote:
#   1. referral triage into Mock EMR (new chart, existing chart, ambiguous halt)
#   2. the same template against an EMR that drops saves (halts on read-back)
#   3. supplier invoices into Mock Ledger (system_of_record grade from the
#      scheduled export of saved data)
#
#   examples/legacy_records/demo.sh [OUT_DIR] [--record]
#
# OUT_DIR must be new, empty, or a folder an earlier run of this demo made
# (it holds a .legacy-records-demo marker). Anything else is refused, so the
# demo never deletes a folder it did not create.
#
# Needs: Xvfb (or an existing DISPLAY), xdotool, xclip, ImageMagick `import`,
# python3 with tkinter for the apps, and openadapt-flow for the workflows
# (FLOW_PYTHON, default `python`). --record also needs ffmpeg.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT=""
RECORD=0
for a in "$@"; do
  case "$a" in
    --record) RECORD=1 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) [[ -z "$OUT" ]] || { echo "one output folder only" >&2; exit 2; }; OUT="$a" ;;
  esac
done
OUT="${OUT:-$PWD/legacy-records-demo}"
FLOW_PYTHON="${FLOW_PYTHON:-python}"
APP_PYTHON="${APP_PYTHON:-python3}"
MARKER=".legacy-records-demo"

refuse() { echo "refusing to use $OUT as the output folder: $1" >&2; exit 2; }
case "$OUT" in
  "" | "." | ".." | "/" | "~" | "$HOME" | "$HOME/") refuse "not a dedicated folder" ;;
esac
if [[ -L "$OUT" ]]; then refuse "it is a symlink"; fi
if [[ -e "$OUT" && ! -d "$OUT" ]]; then refuse "it is not a folder"; fi
if [[ -d "$OUT" ]]; then
  REAL="$(cd "$OUT" && pwd -P)"
  for bad in / "$(cd "$HOME" && pwd -P)" "$(pwd -P)" "$HERE"; do
    [[ "$REAL" == "$bad" ]] && refuse "not a dedicated folder"
  done
  if [[ -f "$OUT/$MARKER" ]]; then
    rm -rf -- "$OUT"  # made by an earlier run of this demo
  elif [[ -n "$(ls -A -- "$OUT")" ]]; then
    refuse "it is not empty and was not made by this demo"
  fi
fi
mkdir -p -- "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
echo "legacy-records demo output; safe to delete" > "$OUT/$MARKER"
cd "$OUT"

PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

if [[ -z "${DISPLAY:-}" || "${DEMO_XVFB:-1}" == 1 ]]; then
  export DISPLAY=":${DEMO_DISPLAY:-77}"
  Xvfb "$DISPLAY" -screen 0 1000x800x24 -nolisten tcp >/dev/null 2>&1 & PIDS+=($!)
  sleep 1
fi

"$FLOW_PYTHON" "$HERE/make_inbox.py" .
"$APP_PYTHON" "$HERE/apps/mock_emr.py" --db emr.sqlite --seed
"$APP_PYTHON" "$HERE/apps/mock_emr.py" --db emr-faulty.sqlite --seed
"$APP_PYTHON" "$HERE/apps/mock_ledger.py" --db ledger.sqlite --seed
mkdir -p out/referrals out/referrals-faulty out/invoices
"$APP_PYTHON" "$HERE/caption.py" out/referrals/run.log out/referrals-faulty/run.log \
  out/invoices/run.log & PIDS+=($!)

if [[ "$RECORD" == 1 ]]; then
  export DEMO_PACE="${DEMO_PACE:-0.7}"
  ffmpeg -loglevel error -y -f x11grab -video_size 1000x800 -framerate 10 -i "$DISPLAY" \
    -pix_fmt yuv420p demo.mp4 & FFMPEG=$!
fi

run_app() {  # run_app NAME ARGS... ; leaves APP_PID set
  "$APP_PYTHON" "$@" >/dev/null 2>&1 & APP_PID=$!; PIDS+=($APP_PID); sleep 1.5
}
cd "$HERE"

echo "== 1. Referral triage into Mock EMR"
run_app apps/mock_emr.py --db "$OUT/emr.sqlite"
"$FLOW_PYTHON" referral_triage.py --inbox "$OUT/fax_inbox" --run-dir "$OUT/out/referrals" \
  --l1-dir "$OUT/out/extractions" --answers answers.demo.json
kill "$APP_PID"; sleep 0.5

echo "== 2. Same template, EMR that says Saved but keeps nothing"
mkdir -p "$OUT/fax_inbox_faulty" && cp "$OUT/fax_inbox/referral-0008.pdf" "$OUT/fax_inbox_faulty/"
run_app apps/mock_emr.py --db "$OUT/emr-faulty.sqlite" --drop-save
"$FLOW_PYTHON" referral_triage.py --inbox "$OUT/fax_inbox_faulty" \
  --run-dir "$OUT/out/referrals-faulty" --l1-dir "$OUT/out/extractions-faulty" \
  --answers answers.demo.json
kill "$APP_PID"; sleep 0.5

echo "== 3. Supplier invoices into Mock Ledger"
run_app apps/mock_ledger.py --db "$OUT/ledger.sqlite" --export-dir "$OUT/exports"
"$FLOW_PYTHON" ap_invoice.py --inbox "$OUT/invoice_inbox" --run-dir "$OUT/out/invoices" \
  --l1-dir "$OUT/out/extractions" --export-dir "$OUT/exports" \
  --ledger-db "$OUT/ledger.sqlite" --app-python "$APP_PYTHON" --answers answers.ledger.demo.json
sleep 2
kill "$APP_PID"

if [[ "$RECORD" == 1 ]]; then kill -INT "$FFMPEG"; wait "$FFMPEG" || true; fi

echo "== Conformance check of every exchange written"
"$FLOW_PYTHON" -m openadapt_flow.interchange check \
  "$OUT"/out/referrals/*.json "$OUT"/out/referrals-faulty/*.json "$OUT"/out/invoices/*.json
echo
echo "L1 manifest: $OUT/out/extractions/manifest.csv"
echo "Review queue: $(ls "$OUT"/out/*/review_queue 2>/dev/null | grep -c json) item(s)"
[[ "$RECORD" == 1 ]] && echo "Recording: $OUT/demo.mp4"
exit 0
