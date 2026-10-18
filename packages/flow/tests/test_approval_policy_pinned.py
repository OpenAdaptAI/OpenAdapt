"""A run's approver policy is fixed when the run starts.

The attack: a deployment passes no approver policy (person-only), so no
policy file was written. Anything that can write the run directory then
drops in a policy that lets an agent approve, and the gate checked answers
against that file. Made-up values only.
"""

from __future__ import annotations

from openadapt_flow.approval_mode.approver import (
    DEFAULT_APPROVER_POLICY,
    StepClass,
)
from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.item_approval import (
    POLICY_FILENAME,
    ItemApprovalStore,
    wait_for_answer,
)
from tests.test_approval_delegated import _agent_answer, _allow_agents, _open


def _plant(run_dir) -> None:
    """Another process replaces the run's policy with one that allows agents."""

    (run_dir / "item-approval" / POLICY_FILENAME).unlink(missing_ok=True)
    ItemApprovalStore(run_dir).write_policy(_allow_agents(StepClass.CONSEQUENTIAL))


def test_the_run_start_writes_the_person_only_policy(tmp_path) -> None:
    run = tmp_path / "run"
    ApprovalGate("each_item", approval_store=run)
    assert (run / "item-approval" / POLICY_FILENAME).is_file()
    assert ItemApprovalStore(run).policy() == DEFAULT_APPROVER_POLICY


def test_a_policy_planted_after_the_run_started_is_ignored(tmp_path) -> None:
    run = tmp_path / "run"
    gate = ApprovalGate("each_item", approval_store=run, approval_wait_s=0)
    store = gate.approval_store
    assert store is not None
    _plant(run)
    request = _open(store)
    # The agent's answer is valid under the planted policy...
    planted = ItemApprovalStore(run)
    store.write_answer(_agent_answer(request, planted))
    # ...but the run checks it against the policy it started with.
    assert store.policy() == DEFAULT_APPROVER_POLICY
    assert store.status(request) == "invalid_answer"
    answer, code = wait_for_answer(store, request, timeout_s=0, poll_s=0)
    assert answer is None and code == "approver_kind_not_allowed"
    assert (
        store.find_unused_approval(
            run_id=request.run_id, step_id=request.step_id, step_index=3
        )
        is None
    )


def test_a_policy_planted_before_the_run_refuses_a_narrower_run(tmp_path) -> None:
    run = tmp_path / "run"
    _plant(run)
    try:
        ApprovalGate("each_item", approval_store=run)
    except Exception as exc:  # noqa: BLE001 - the code is what matters
        assert getattr(exc, "code", None) == "policy_changed"
    else:
        raise AssertionError("a run started under a planted policy")
