#!/bin/zsh
# Run 3: steps 1, 2, 2b and 4 in sequence (same drivers as run 2).
R=/Users/abrichr/oa/worktrees/acp-demo-out/run3
source $R/scripts/env.sh >/dev/null
export PYTHONPATH=$PYTHONPATH:$M/packages/desktop
$PY $R/scripts/step1_shadow.py $R/1-shadow 20 > $R/1-shadow.log 2>&1; echo "step1 exit $?"
$PY $R/scripts/step2_gated.py $R/2-gated $R/scripts/card_shot.py > $R/2-gated.log 2>&1; echo "step2 exit $?"
timeout 400 $PY $R/scripts/step2b_watch.py $R/2b-watch > $R/2b-watch.log 2>&1; echo "step2b exit $?"
timeout 500 $PY $R/scripts/step4_fallback.py $R/4-fallback < /dev/null > $R/4-fallback.log 2>&1; echo "step4 exit $?"
