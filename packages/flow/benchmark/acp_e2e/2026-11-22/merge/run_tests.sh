#!/bin/zsh
# Unit tests of the ACP pieces on the integration tree, single process.
M=/Users/abrichr/oa/worktrees/acp-demo-merge
PY=/Users/abrichr/oa/worktrees/wrap-publish/packages/flow/.venv/bin/python
export PYTHONPATH=$M/packages/flow:$M/packages/types:$M/packages/privacy:$M/packages/desktop:$M
unset OPENADAPT_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY
export OPENADAPT_HOME=$(mktemp -d)
cd $M/packages/flow
echo "### flow unit"; $PY -m pytest -q -p no:cacheprovider -p no:xdist \
  tests/test_exception_detectors.py tests/test_exception_registry.py tests/test_exception_watch.py \
  tests/test_flight_cli_egress.py tests/test_flight_computer.py tests/test_flight_egress.py tests/test_flight_phi_payload.py \
  tests/test_flight_proxy.py tests/test_flight_scrub.py tests/test_flight_timeline.py \
  tests/test_guard_approval_fields.py tests/test_guard_ledger_row.py tests/test_guard_readback_provider.py \
  tests/test_readiness_claude_cu_agent.py tests/test_readiness_compare.py tests/test_readiness_egress.py \
  tests/test_readiness_hardening.py tests/test_readiness_reference.py \
  tests/test_run_ledger_cli.py tests/test_run_ledger_collect.py tests/test_run_ledger_joins.py tests/test_run_ledger_outcome.py \
  tests/test_shadow_hold.py tests/test_wrap_approval_gate.py tests/test_wrap_dispatch_cli.py tests/test_wrap_fallback.py \
  tests/test_cli_tasks.py tests/test_capabilities_map.py 2>&1 | tail -60
echo "### types"; cd $M/packages/types && $PY -m pytest -q -p no:cacheprovider -p no:xdist tests/test_exception_card.py tests/test_run_ledger.py 2>&1 | tail -15
echo "### desktop"; cd $M/packages/desktop && $PY -m pytest -q -p no:cacheprovider -p no:xdist tests/test_engine/test_stop_queue_exceptions.py tests/test_guard_request_pickup.py 2>&1 | tail -15
echo "### launcher"; cd $M && $PY -m pytest -q -p no:cacheprovider -p no:xdist tests/test_cli_flight.py 2>&1 | tail -15
