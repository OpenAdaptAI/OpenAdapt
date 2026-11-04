#!/bin/sh
# One app end to end on the frozen engine: up, record, compile, the case
# matrix in both arms, timing where the API step ran, down.
#   APIGEN_FROZEN=<sha> APIGEN_PY=<python> ./run_app.sh <app> [reps]
# Results: results/frozen-<sha8>/<app>/. Timing waits for load under 35.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
APP=$1
REPS=${2:-10}
: "${APIGEN_FROZEN:?set APIGEN_FROZEN to the frozen engine commit}"
OUT="$HERE/results/frozen-$(echo "$APIGEN_FROZEN" | cut -c1-8)/$APP"
STATE=${APIGEN_STATE:-$HOME/oa/scratch/api-generalization}
REC="$STATE/$APP/rec-$(echo "$APIGEN_FROZEN" | cut -c1-8)"
mkdir -p "$OUT"
H="$HERE/run.sh"
load1() { sysctl -n vm.loadavg | awk '{print $2}'; }

"$H" up --app "$APP"
[ -d "$REC" ] || "$H" record --app "$APP" --out "$REC" | tee "$OUT/record.json"
rm -rf "${STATE:?}/${APP:?}/bundle" "${STATE:?}/${APP:?}/matrix" "${STATE:?}/${APP:?}/timing"
"$H" compile --app "$APP" --recording "$REC" --out "$STATE/$APP/bundle" > "$OUT/compile.log" 2>&1
cp "$STATE/$APP/bundle/apigen-compile.json" "$OUT/compile.json"
"$H" matrix --app "$APP" --bundle "$STATE/$APP/bundle" --out "$STATE/$APP/matrix" --tag "$(date +%H%M)" \
  > "$OUT/matrix.log" 2>&1 || true
cp "$STATE/$APP/matrix/runs.csv" "$OUT/matrix.csv" 2>/dev/null || true
if grep -q '"proposed": true' "$OUT/compile.json"; then
  until [ "$(load1 | cut -d. -f1)" -lt 35 ]; do sleep 30; done
  "$H" timing --app "$APP" --bundle "$STATE/$APP/bundle" --out "$STATE/$APP/timing" --reps "$REPS" \
    > "$OUT/timing.log" 2>&1 || true
  cp "$STATE/$APP/timing/runs.csv" "$OUT/timing.csv" 2>/dev/null || true
fi
"$H" down --app "$APP"
echo "done $APP"
