"""Reference Execute server: a fault after dispatch never claims "no effect".

``failed_platform`` means the platform failed before any possible business
effect, so a caller may safely resubmit. Once the runner has been called, a
write may have landed, and the only honest terminal outcome for an
unclassifiable run is ``reconciliation_required`` with uncertain delivery.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("openadapt_types")

from openadapt_types.execute import ExecuteTerminalOutcomeV1  # noqa: E402

from openadapt_flow.execute import service as service_module  # noqa: E402
from openadapt_flow.execute.dispatch import (  # noqa: E402
    DispatchError,
    _map_outcome,
    project_run_report,
)
from openadapt_flow.execute.registry import (  # noqa: E402
    MOCKMED_EFFECT_STRENGTH,
    MOCKMED_ENVIRONMENT_OK,
    MOCKMED_QUALIFICATION_ID,
    MOCKMED_WORKFLOW_DIGEST,
    MOCKMED_WORKFLOW_VERSION,
)
from openadapt_flow.execute.service import ExecuteService  # noqa: E402


def _request() -> dict[str, object]:
    return {
        "schema_version": "openadapt.execute-request/v1",
        "qualification_id": MOCKMED_QUALIFICATION_ID,
        "workflow_version": MOCKMED_WORKFLOW_VERSION,
        "workflow_digest": MOCKMED_WORKFLOW_DIGEST,
        "environment_id": MOCKMED_ENVIRONMENT_OK,
        "parameters": {"record": {"id": "12345"}},
        "idempotency_key": "caller_key_12345678",
        "authorization_context": {
            "actor_id": "caller_agent_12345678",
            "authorization_reference": "authorization_12345678",
        },
        "effect_strength_schema_version": "1",
        "minimum_effect_strength": MOCKMED_EFFECT_STRENGTH,
    }


def _receipt(tmp_path: Path, runner: Any) -> Any:
    store = ExecuteService(tmp_path, token="t", runner=runner, seed_mockmed=True)
    accepted = store.create_execution(_request())
    return store.get_receipt(accepted.execution_id)


def test_runner_crash_after_write_requires_reconciliation(tmp_path: Path) -> None:
    writes: list[str] = []

    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        writes.append("record saved in system of record")
        raise DispatchError("local replay failed: browser closed")

    receipt = _receipt(tmp_path, runner)
    assert writes, "the runner was invoked"
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.delivery_uncertain is True
    assert receipt.contracts.effect_passed is False


def test_unexpected_runner_exception_requires_reconciliation(tmp_path: Path) -> None:
    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        raise TimeoutError("timed out after the submit click")

    receipt = _receipt(tmp_path, runner)
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.delivery_uncertain is True


def test_verified_claim_without_contracts_requires_reconciliation(
    tmp_path: Path,
) -> None:
    report = SimpleNamespace(
        transaction_outcome="VERIFIED",
        execution_outcome="VERIFIED",
        outcome_envelope=None,
        model_calls=0,
        bundle_content_digest="x",
    )

    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        return project_run_report(report, workflow_digest=request.workflow_digest)

    receipt = _receipt(tmp_path, runner)
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.delivery_uncertain is True

    direct = project_run_report(report, workflow_digest=MOCKMED_WORKFLOW_DIGEST)
    assert direct.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert direct.delivery_uncertain is True
    assert direct.effect_passed is False


def test_pre_effect_dispatch_error_stays_failed_platform(tmp_path: Path) -> None:
    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        raise DispatchError("admitted bundle_dir is missing", pre_effect=True)

    receipt = _receipt(tmp_path, runner)
    assert receipt.outcome is ExecuteTerminalOutcomeV1.FAILED_PLATFORM
    assert receipt.delivery_uncertain is False


def test_admission_lookup_failure_stays_failed_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        calls.append("ran")
        raise AssertionError("the runner must not be reached")

    store = ExecuteService(tmp_path, token="t", runner=runner, seed_mockmed=True)

    def broken_lookup(*args: Any, **kwargs: Any) -> Any:
        raise OSError("admission store unreadable")

    # create_execution checks the admission itself; break only the second
    # lookup that _run performs before dispatch.
    real_lookup = service_module.lookup_admission
    seen: list[int] = []

    def lookup_once(*args: Any, **kwargs: Any) -> Any:
        seen.append(1)
        if len(seen) == 1:
            return real_lookup(*args, **kwargs)
        return broken_lookup()

    monkeypatch.setattr(service_module, "lookup_admission", lookup_once)
    accepted = store.create_execution(_request())
    receipt = store.get_receipt(accepted.execution_id)
    assert calls == []
    assert receipt.outcome is ExecuteTerminalOutcomeV1.FAILED_PLATFORM
    assert receipt.delivery_uncertain is False


def test_live_replay_missing_bundle_is_pre_effect(tmp_path: Path) -> None:
    from openadapt_types.execute import ExecuteRequestV1

    from openadapt_flow.execute.dispatch import live_replay
    from openadapt_flow.execute.models import AdmittedBundle

    admission = AdmittedBundle(
        qualification_id=MOCKMED_QUALIFICATION_ID,
        workflow_version=MOCKMED_WORKFLOW_VERSION,
        workflow_digest=MOCKMED_WORKFLOW_DIGEST,
        environment_id=MOCKMED_ENVIRONMENT_OK,
        minimum_effect_strength=MOCKMED_EFFECT_STRENGTH,
        bundle_dir=str(tmp_path / "missing"),
    )
    request = ExecuteRequestV1.model_validate(_request())
    with pytest.raises(DispatchError) as raised:
        live_replay(admission, request, tmp_path / "run")
    assert raised.value.pre_effect is True


@pytest.mark.parametrize("coarse", ["FAILED", ""])
def test_unclassified_coarse_outcome_requires_reconciliation(coarse: str) -> None:
    assert _map_outcome(coarse, None) is (
        ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    )


def _fail_every_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_finalize(self: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ExecuteService, "_finalize", broken_finalize)


def test_unsealable_fault_never_leaves_execution_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_types.execute import ExecuteLifecycleStateV1

    from openadapt_flow.execute.service import ExecuteServiceError

    _fail_every_finalize(monkeypatch)
    store = ExecuteService(tmp_path, token="t", seed_mockmed=True)
    accepted = store.create_execution(_request())

    status = store.get_status(accepted.execution_id)
    assert status.state is ExecuteLifecycleStateV1.WAITING_FOR_RECONCILIATION
    assert status.terminal_outcome is None
    assert status.evidence_receipt_id is None
    with pytest.raises(ExecuteServiceError) as refused:
        store.get_receipt(accepted.execution_id)
    assert refused.value.status_code == 409

    # Resubmitting the same key finds the same execution, which a client
    # can now see is waiting for a person instead of still running.
    again = store.create_execution(_request())
    assert again.execution_id == accepted.execution_id
    assert store.get_status(again.execution_id).state is (
        ExecuteLifecycleStateV1.WAITING_FOR_RECONCILIATION
    )


def test_unsealable_pre_effect_fault_is_not_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_types.execute import ExecuteLifecycleStateV1

    def runner(admission: Any, request: Any, run_dir: Path) -> Any:
        raise DispatchError("admitted bundle_dir is missing", pre_effect=True)

    _fail_every_finalize(monkeypatch)
    store = ExecuteService(tmp_path, token="t", runner=runner, seed_mockmed=True)
    accepted = store.create_execution(_request())
    assert store.get_status(accepted.execution_id).state is (
        ExecuteLifecycleStateV1.WAITING_FOR_RECONCILIATION
    )


def test_finalize_failure_after_delivered_effect_requires_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_finalize = ExecuteService._finalize
    calls: list[int] = []

    def finalize_fails_once(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        real_finalize(self, *args, **kwargs)

    monkeypatch.setattr(ExecuteService, "_finalize", finalize_fails_once)
    store = ExecuteService(tmp_path, token="t", seed_mockmed=True)
    accepted = store.create_execution(_request())
    receipt = store.get_receipt(accepted.execution_id)
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.delivery_uncertain is True
