#!/bin/zsh
# Step 3 (run 2): ledger add every run dir from steps 1, 2, 2b, 4 and 5 (one
# directory or envelope per run, never a parent), then summary, export, a
# dry-run of the push payloads, and a real `ledger push` to the local Cloud
# dev server (mock mode, synthetic org ingest token oai_ingest_mock).
# Usage: step3_ledger.sh R2 CLOUD_ORIGIN
R2=$1; HOST=$2
source $R2/scripts/env.sh >/dev/null
export OPENADAPT_HOME=$R2/openadapt-home
L=$R2/3-ledger; mkdir -p $L; : > $L/add.log
add() { echo "\$ ledger add $*" >> $L/add.log; $PY -m openadapt_flow ledger add "$@" >> $L/add.log 2>&1; echo "[exit $?]" >> $L/add.log; }
S1=$R2/1-shadow
for d in $S1/runs/readiness-mockmed-*(/); do t=${d:t}; add $d --task-type intake --config $S1/episodes/$t/guard_task.json; done
for d in $S1/adversarial/runs/readiness-mockmed-*(/); do t=${d:t}; add $d --task-type intake --config $S1/adversarial/episodes/$t/guard_task.json; done
for d in $R2/2-gated/runs/*(/); do t=${d:t}; add $d --task-type intake --config $R2/2-gated/setup/$t/task.json; done
add $R2/2b-watch/run --task-type intake --run-ref watch-2fa-run2 --config $R2/2b-watch/setup/task.json
for l in happy drift-approved drift-declined; do add $R2/4-fallback/result-$l.json --task-type intake --config $R2/4-fallback/verify.yaml; done
if [ -d $R2/5-claude/runs ]; then for d in $R2/5-claude/runs/readiness-mockmed-*(/); do t=${d:t}; add $d --task-type intake --config $R2/5-claude/episodes/$t/guard_task.json; done; fi
$PY -m openadapt_flow ledger summary > $L/summary.txt 2>&1; echo "[exit $?]" >> $L/summary.txt
$PY -m openadapt_flow ledger summary --json > $L/summary.json 2> $L/summary.json.err
$PY -m openadapt_flow ledger list > $L/list.txt 2>&1
$PY -m openadapt_flow ledger export --out $L/export.jsonl --require-scrubber > $L/export.log 2>&1; echo "[exit $?]" >> $L/export.log
$PY -m openadapt_flow ledger push --host $HOST --dry-run > $L/payloads.jsonl 2> $L/push-dry-run.err; echo "[exit $?]" >> $L/push-dry-run.err
OPENADAPT_API_KEY=oai_ingest_mock OPENADAPT_FLOW_DESTINATION_KIND=local $PY -m openadapt_flow ledger push --host $HOST > $L/push.log 2>&1; echo "[exit $?]" >> $L/push.log
cat $L/push.log
