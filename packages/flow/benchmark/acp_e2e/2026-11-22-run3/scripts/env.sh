export M=/Users/abrichr/oa/worktrees/acp-demo-merge
export PY=/Users/abrichr/oa/worktrees/wrap-publish/packages/flow/.venv/bin/python
export PYTHONPATH=$M/packages/flow:$M/packages/types:$M/packages/privacy
export OPENADAPT_HOME=/Users/abrichr/oa/worktrees/acp-demo-out/run3/openadapt-home
export PATH=$(dirname $PY):$PATH
unset OPENADAPT_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY
mkdir -p $OPENADAPT_HOME
cd $M/packages/flow
