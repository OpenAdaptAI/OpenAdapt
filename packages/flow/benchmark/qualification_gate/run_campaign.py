#!/usr/bin/env python3
"""Run the deterministic local qualification gate campaign.

This harness rebuilds the bounded synthetic campaign pattern proven by the
real-RDP multi-window campaign (``benchmark/rdp_multiapp``) on a fully local,
in-process substrate: Pillow-rendered pixels, a SQLite system of record, and
the unmodified governed runtime (Recorder -> compiler -> run gate ->
qualification case authority -> Replayer -> independent effect verifier).

Gate standard (AGENTS.md §2): every task x condition runs >= 3 trials; the
counted summary exposes ``silent_incorrect_successes`` and ``over_halts``
explicitly and refuses to run without them; at least three expected
uncertain-delivery fault conditions must end in ``RECONCILIATION_REQUIRED``
or in a contract-proven ``VERIFIED`` with zero blind retries and zero replay
dispatches.

Honest label: this is a DETERMINISTIC LOCAL STAND-IN substrate. It does not
exercise FreeRDP/Citrix transports, real window managers, or hosted browsers.
Those remain bound to the hosted campaigns. What it does qualify is the
substrate-independent gate behavior of the unmodified runtime: verified
healthy effects, safe halts before consequential input on identity/ambiguity/
render faults, and exactly-once uncertain-delivery reconciliation.

Run::

    python3 benchmark/qualification_gate/run_campaign.py \\
        --output benchmark/qualification_gate/results.json

No Docker, no network, no browser, no model calls: fully deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_flow import __version__  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.deployment import DeploymentConfig, PolicySection  # noqa: E402
from openadapt_flow.execution_profiles import (  # noqa: E402
    execution_profile_contract,
)
from openadapt_flow.ir import Postcondition, PostconditionKind  # noqa: E402
from openadapt_flow.qualification import (  # noqa: E402
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationOutcome,
    add_case,
    init_project,
    set_action_classification,
    set_case_scope,
    set_effect_policy,
)
from openadapt_flow.recorder import Recorder  # noqa: E402
from openadapt_flow.run_gate import (  # noqa: E402
    build_qualification_case_authorization,
    evaluate_run_gate,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import (  # noqa: E402
    Effect,
    EffectKind,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from openadapt_flow.verification import VerificationTier  # noqa: E402

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixture.py"


def _load_fixture():
    import importlib

    spec = importlib.util.spec_from_file_location("gate_fixture", _FIXTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("qualification gate fixture module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fx = _load_fixture()

CAMPAIGN_ID = "qualification-gate-campaign-v1"
CAMPAIGN_CONTRACT = "benchmark/qualification_gate/campaign.json"
CAMPAIGN_CONTRACT_PATH_NAME = "campaign.json"
TRIALS_PER_CONDITION = 3
NOTE_PARAM = fx.NOTE_PARAM
PARAMS = {NOTE_PARAM: fx.NOTE_VALUE}

REQUIRED_METRICS = (
    "verified_outcomes",
    "safe_halts",
    "reconciliation_required_outcomes",
    "silent_incorrect_successes",
    "over_halts",
    "wrong_record_writes",
    "duplicate_effects",
    "model_calls",
    "blind_retries",
    "replay_dispatches",
)

# Uncertain-delivery fault conditions required by the production gate.
UNCERTAIN_CONDITIONS = (
    "uncertain_delivery_write_lost",
    "uncertain_delivery_write_kept_timeout",
    "uncertain_delivery_oracle_unreachable",
)


@dataclass(frozen=True)
class ConditionSpec:
    id: str
    expect: str
    plan: "fx.ConditionPlan"


def condition_specs() -> tuple[ConditionSpec, ...]:
    specs = (
        ConditionSpec("healthy", "verified", fx.ConditionPlan()),
        ConditionSpec(
            "row_reordered", "verified", fx.ConditionPlan(row_reordered=True)
        ),
        ConditionSpec(
            "moderate_display_drift",
            "verified_or_safe_halt",
            fx.ConditionPlan(drift="moderate_display_drift"),
        ),
        ConditionSpec(
            "severe_display_drift",
            "safe_halt",
            fx.ConditionPlan(drift="severe_display_drift"),
        ),
        ConditionSpec(
            "duplicate_save_control",
            "safe_halt",
            fx.ConditionPlan(duplicate_save_control=True),
        ),
        ConditionSpec(
            "partial_render", "safe_halt", fx.ConditionPlan(hide_save_control=True)
        ),
        ConditionSpec(
            "wrong_record_before_write",
            "safe_halt",
            fx.ConditionPlan(pre_write_mutation="wrong_record"),
        ),
        ConditionSpec(
            "stale_identity_before_write",
            "safe_halt",
            fx.ConditionPlan(pre_write_mutation="stale_identity"),
        ),
        ConditionSpec(
            UNCERTAIN_CONDITIONS[0],
            "reconciliation_required",
            fx.ConditionPlan(uncertainty="write_lost_reset"),
        ),
        ConditionSpec(
            UNCERTAIN_CONDITIONS[1],
            "verified_after_uncertainty_or_reconciliation_required",
            fx.ConditionPlan(uncertainty="write_kept_timeout"),
        ),
        ConditionSpec(
            UNCERTAIN_CONDITIONS[2],
            "reconciliation_required",
            fx.ConditionPlan(uncertainty="oracle_unreachable"),
        ),
    )
    _validate_against_contract(specs)
    return specs


def _validate_against_contract(
    specs: tuple[ConditionSpec, ...],
    *,
    contract_path: Optional[Path] = None,
) -> None:
    """Fail closed unless the implemented matrix matches campaign.json."""

    path = contract_path or (
        Path(__file__).resolve().parent / CAMPAIGN_CONTRACT_PATH_NAME
    )
    contract = json.loads(path.read_text())
    if contract.get("schema_version") != "openadapt.qualification-gate-campaign.v1":
        raise RuntimeError("campaign contract schema_version is unsupported")
    contract_trials = int(contract["trials_per_condition"])
    if contract_trials < 3:
        raise RuntimeError(
            "the production gate requires at least three trials per condition"
        )
    global TRIALS_PER_CONDITION
    TRIALS_PER_CONDITION = contract_trials
    required = list(contract["required_metrics"])
    if sorted(required) != sorted(REQUIRED_METRICS):
        raise RuntimeError(
            "campaign contract required metrics diverge from the harness"
        )
    uncertain_min = int(contract.get("minimum_uncertain_delivery_conditions", 0))
    if uncertain_min < 3 or len(UNCERTAIN_CONDITIONS) < uncertain_min:
        raise RuntimeError(
            "the gate requires at least three uncertain-delivery conditions"
        )
    declared = {str(item["id"]): str(item["expect"]) for item in contract["conditions"]}
    implemented = {spec.id: spec.expect for spec in specs}
    if declared != implemented:
        missing = sorted(set(implemented) - set(declared))
        extra = sorted(set(declared) - set(implemented))
        changed = sorted(
            item
            for item in set(declared) & set(implemented)
            if declared[item] != implemented[item]
        )
        raise RuntimeError(
            "implemented conditions diverge from campaign.json: "
            f"missing={missing} extra={extra} changed_expectation={changed}"
        )


def _record_demo(recording_dir: Path) -> Path:
    """Demonstrate the workflow once against the healthy fixture."""

    oracle = fx.GateOracle(recording_dir.parent / "recording-oracle")
    oracle.reset()
    backend = fx.GateFixtureBackend(oracle, plan=fx.ConditionPlan())
    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.0,
        settle_stable_frames=2,
        settle_timeout_s=3.0,
    )
    recorder.click(*fx.ROW_200_POINT)
    recorder.click(*fx.NOTE_FIELD_POINT)
    recorder.type_text(fx.NOTE_VALUE, param=NOTE_PARAM)
    recorder.click(*fx.SAVE_BUTTON_POINT)
    recorder.finish()
    return recording_dir


def _qualify(workflow: Any, bundle_dir: Path):
    """Bind the exact effect contracts, project scope, and sealed bundle."""

    save = workflow.steps[-1]
    if getattr(save.action, "value", save.action) != "click":
        raise RuntimeError(
            "compiled campaign workflow must end with the qualified Save click"
        )
    # Operator-reviewed identity binding for the anchored TYPE step: its
    # field is identified by the same stable label band as the focusing
    # click ("Triage note entry"), so keyboard delivery stays bound to the
    # verified focus through the runtime's guarded keyboard lease.
    type_steps = [
        step
        for step in workflow.steps
        if getattr(step.action, "value", step.action) == "type"
    ]
    if len(type_steps) != 1:
        raise RuntimeError(
            "compiled campaign workflow must contain exactly one typed field"
        )
    type_step = type_steps[0]
    type_index = workflow.steps.index(type_step)
    if type_index < 1:
        raise RuntimeError("typed field has no preceding focusing click")
    focusing_click = workflow.steps[type_index - 1]
    source_anchor = focusing_click.anchor
    has_identity_evidence = bool(
        source_anchor is not None
        and getattr(focusing_click.action, "value", focusing_click.action) == "click"
        and (
            source_anchor.context_text
            or source_anchor.structured_identity
            or source_anchor.identity_template
            or source_anchor.identifier_crop
        )
    )
    if not has_identity_evidence:
        raise RuntimeError(
            "typed field lacks an identity-bound focusing click to inherit"
        )
    type_step.anchor = source_anchor.model_copy(deep=True)
    type_step.identity_armed = True
    # Operator-reviewed evidence binding: this campaign's identity rests on
    # per-target content evidence (template crop, context band, identifier
    # crop), not on relational landmark geometry. The row_reordered fault
    # legitimately moves whole records, so relational landmarks derived at
    # record time would disagree under it; they are removed from every
    # anchor rather than weakened at runtime.
    for step in workflow.steps:
        if step.anchor is not None and step.anchor.landmarks:
            step.anchor.landmarks = []
    save.risk = "irreversible"
    save.risk_explanation = "qualified triage note write"
    save.risk_review_required = False
    save.expect = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")]
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "record_id": ValueExpr(literal=fx.TARGET_RECORD),
                "note": ValueExpr(param=NOTE_PARAM),
            },
            expected_count=1,
            count_new_only=True,
            key_field="record_id",
            idempotency_key=ValueExpr(literal=fx.TARGET_RECORD),
            risk="irreversible",
            probe="surface=records|read-only exact SQLite lookup",
        )
    ]

    # Every delivered input edge carries its own exact one-write effect
    # contract on the fixture's independent persisted event surface, so a
    # duplicate or phantom edge fails independent verification.
    edge_contracts = (
        (0, "select_record", "gate-select_record-once"),
        (1, "focus_note", "gate-focus_note-once"),
        (2, "type_note", "gate-type_note-once"),
    )
    for index, kind, run_key in edge_contracts:
        edge_step = workflow.steps[index]
        match_spec: dict[str, ValueExpr] = {"kind": ValueExpr(literal=kind)}
        if kind == "select_record":
            match_spec["detail"] = ValueExpr(literal=fx.TARGET_RECORD)
        elif kind == "type_note":
            match_spec["detail"] = ValueExpr(param=NOTE_PARAM)
        edge_step.effects = [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match=match_spec,
                expected_count=1,
                count_new_only=True,
                identity_field="event_id",
                key_field="run_key",
                idempotency_key=ValueExpr(literal=run_key),
                risk="reversible",
                probe="surface=input_events|read-only exact event lookup",
            )
        ]

    observer = fx.GateEnvironmentObserver()
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="linux",
            application="OpenAdapt qualification-gate synthetic clinic",
            application_identity="qualification-gate-fixture",
            application_version="v1",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=observer.contract_sha256,
            environment_digest=fx.GATE_ENVIRONMENT_DIGEST,
            runtime_version=__version__,
            required_capabilities=[
                "pixel_observation",
                "effect_verification",
            ],
        ),
    )
    # Operator-reviewed classifications: the row selection, field focus, and
    # typed entry change application or field state and feed the qualified
    # write, so each delivery stays bound to its verified target identity and
    # fresh frame through the runtime's one-shot guarded leases while their
    # executable risk remains reversible. Only the final click is an
    # irreversible persisted write.
    for step in workflow.steps:
        classification = (
            ActionRiskClass.IRREVERSIBLE
            if step.id == save.id
            else ActionRiskClass.STATE_CHANGING
        )
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=classification,
                explanation=(
                    "Qualified persisted business write"
                    if classification is ActionRiskClass.IRREVERSIBLE
                    else "Record selection, field focus, or parameter entry "
                    "feeding the qualified write; delivery bound to verified "
                    "identity"
                ),
                operator_confirmed=True,
            ),
        )
    set_effect_policy(
        workflow,
        step_id=save.id,
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    for index, _kind, _run_key in edge_contracts:
        set_effect_policy(
            workflow,
            step_id=workflow.steps[index].id,
            effect_index=0,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
        )

    case_id = "gate-representative"
    add_case(
        workflow,
        QualificationCase(
            id=case_id,
            kind=QualificationCaseKind.REPRESENTATIVE,
            description="Local gate-campaign representative execution",
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    set_case_scope(
        workflow,
        case_id=case_id,
        runtime_input_sha256=runtime_inputs_digest(workflow, PARAMS, None),
        action_targets=[
            QualificationActionTarget(step_id=step.id, actuation_path="gui")
            for step in workflow.steps
        ],
    )

    bundle_key = secrets.token_urlsafe(32)
    checkpoint_key = secrets.token_urlsafe(32)
    workflow.save(bundle_dir, encrypt=True, key=bundle_key)
    loaded = load_workflow(bundle_dir, bundle_key)
    return loaded, bundle_dir, checkpoint_key, case_id, save.id


def hashlib_file_sha256(path: Path) -> str:
    return sha256_hex(path.read_bytes())


def sha256_hex(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def load_workflow(bundle_dir: Path, key: str):
    from openadapt_flow.ir import Workflow

    return Workflow.load(bundle_dir, key=key)


class ProbeRoutedVerifier:
    """Route each typed effect to the independent verifier for its surface.

    Mirrors ``benchmark.multiapp_common.SurfaceRoutedVerifier``: an effect's
    ``probe`` prefix ``surface=<name>|...`` selects the sub-verifier, and the
    composite advertises that sub-verifier's evidence tier. Every route here
    is an independent read-only SQLite surface.
    """

    substrate = "composite-sql"

    def __init__(self, verifiers: dict[str, SqlRecordVerifier]) -> None:
        self._verifiers = verifiers

    def _surface_of(self, effect: Any) -> str:
        probe = getattr(effect, "probe", "") or ""
        if probe.startswith("surface="):
            return probe[len("surface=") :].split("|", 1)[0].strip()
        raise ValueError("campaign effect contracts must declare their surface")

    def verification_tier_for(self, effect: Any) -> VerificationTier:
        return VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self, context: Any = None) -> Any:
        from openadapt_flow.runtime.effects import EffectState

        detail: dict[str, Any] = {}
        reachable = True
        for name, verifier in self._verifiers.items():
            state = verifier.capture_pre_state(context)
            detail[name] = {"reachable": state.reachable, "records": state.records}
            reachable = reachable and state.reachable
        return EffectState(
            substrate=self.substrate, reachable=reachable, records=[], detail=detail
        )

    def verify(self, expected: Any, before: Any, context: Any = None) -> Any:
        from openadapt_flow.runtime.effects import EffectState

        name = self._surface_of(expected)
        verifier = self._verifiers[name]
        sub = before.detail.get(name, {})
        sub_before = EffectState(
            substrate=verifier.substrate,
            reachable=bool(sub.get("reachable", False)),
            records=list(sub.get("records", [])),
        )
        return verifier.verify(expected, sub_before, context)


def _build_verifier(records_path: Path, backend: Any = None) -> Any:
    records_verifier = SqlRecordVerifier(
        lambda path=records_path: sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ),
        "SELECT record_id, note FROM records",
    )
    events_verifier = SqlRecordVerifier(
        lambda path=records_path: sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ),
        "SELECT event_id, seq, kind, detail, run_key FROM input_events ORDER BY seq",
    )
    routed = ProbeRoutedVerifier(
        {"records": records_verifier, "input_events": events_verifier}
    )
    if backend is None:
        return routed
    return DegradableSqlVerifierAdapter(routed, backend=backend)


class DegradableSqlVerifierAdapter:
    """Fail oracle reads closed after the fixture flags degradation."""

    def __init__(self, inner: Any, *, backend: Any) -> None:
        self._inner = inner
        self._backend = backend
        self.substrate = inner.substrate

    def verification_tier_for(self, effect: Any) -> VerificationTier:
        return self._inner.verification_tier_for(effect)

    def capture_pre_state(self, context: Any = None) -> Any:
        if getattr(self._backend, "degraded_oracle", False):
            from openadapt_flow.runtime.effects import EffectState

            return EffectState(substrate=self.substrate, reachable=False)
        return self._inner.capture_pre_state(context)

    def verify(self, expected: Any, before: Any, context: Any = None) -> Any:
        if getattr(self._backend, "degraded_oracle", False):
            from openadapt_flow.runtime.effects import EffectVerdict

            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=getattr(expected, "kind", None),
                substrate=self.substrate,
                reason=(
                    "campaign fault: system-of-record unreachable; "
                    "the outcome cannot be proven either way"
                ),
            )
        return self._inner.verify(expected, before, context)


def _bootstrap_oracle_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "record_id TEXT PRIMARY KEY, note TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS input_events ("
            "event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, "
            "kind TEXT NOT NULL, detail TEXT NOT NULL, "
            "run_key TEXT NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()


POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"


def _build_gate(workflow: Any, bundle_dir: Path, root: Path):
    gate_db = root / "records.sqlite3"
    _bootstrap_oracle_db(gate_db)
    verifier = _build_verifier(gate_db)
    gate = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(policy=PolicySection(policy=str(POLICY_PATH))),
        effect_verifier=verifier,
        policy_source=str(POLICY_PATH),
        profile_contract=execution_profile_contract("standard"),
        effective_durable=True,
        effective_require_settled=True,
        qualification_evidence_only=True,
        strict_templates=True,
        require_encryption=True,
    )
    if not gate.passed:
        raise RuntimeError(gate.render())
    return gate


def _oracle_result(oracle: "fx.GateOracle") -> dict[str, Any]:
    rows = oracle.read_all()
    ledger = oracle.read_ledger()
    events = oracle.read_events()
    save_entries = [e for e in events if e.get("kind") == "save_note"]
    expected_row = {"record_id": fx.TARGET_RECORD, "note": fx.NOTE_VALUE}
    exact_single_write = bool(len(rows) == 1 and rows[0] == expected_row)
    wrong_record_write = bool(
        any(row.get("record_id") != fx.TARGET_RECORD for row in rows)
        or any(e.get("detail") != fx.TARGET_RECORD for e in save_entries)
    )
    duplicate_effect = bool(len(rows) > 1 or len(save_entries) > 1)
    return {
        "rows": rows,
        "ledger": ledger,
        "events": events,
        "save_entries": len(save_entries),
        "exact_single_write": exact_single_write,
        "wrong_record_write": wrong_record_write,
        "duplicate_effect": duplicate_effect,
        "no_consequential_input": len(save_entries) == 0,
    }


def _resolution_rungs(report: Any) -> list[str]:
    rungs: list[str] = []
    for result in getattr(report, "results", []) or []:
        resolution = getattr(result, "resolution", None)
        rung = getattr(resolution, "rung", None)
        if rung is not None:
            rungs.append(str(getattr(rung, "value", rung)))
    return rungs


_FORBIDDEN_RUNG_MARKERS = ("model", "grounder", "vlm", "llm")


def _run_trial(
    *,
    spec: ConditionSpec,
    trial: int,
    workflow: Any,
    bundle_dir: Path,
    checkpoint_key: str,
    gate: Any,
    case_id: str,
    output_root: Path,
) -> dict[str, Any]:
    run_id = f"{spec.id}-trial-{trial:02d}"
    trial_root = output_root / spec.id / f"trial-{trial:02d}"
    trial_root.mkdir(parents=True, exist_ok=True)
    oracle = fx.GateOracle(trial_root / "oracle")
    oracle.reset()
    backend = fx.GateFixtureBackend(oracle, plan=spec.plan)
    authorization = build_qualification_case_authorization(
        workflow,
        gate,
        case_id=case_id,
        params=dict(PARAMS),
        worklists=None,
        campaign_id=CAMPAIGN_ID,
        run_id=run_id,
    )
    verifier = _build_verifier(
        trial_root / "oracle" / "records.sqlite3", backend=backend
    )
    started = time.monotonic()
    replayer = Replayer(
        backend,
        poll_interval_s=0.0,
        effect_verifier=verifier,
        governed_authorization=authorization,
        qualification_environment_observer=fx.GateEnvironmentObserver(),
        durable=True,
        require_settled=True,
        checkpoint_key=checkpoint_key,
    )
    report = replayer.run(
        workflow.model_copy(deep=True),
        params=dict(PARAMS),
        bundle_dir=bundle_dir,
        run_dir=trial_root / "run",
        execution_target_kind="linux",
        run_id=run_id,
    )
    runtime_s = time.monotonic() - started

    uncertainty_rows = []
    for result in getattr(report, "results", []) or []:
        uncertainty = getattr(result, "delivery_uncertainty", None)
        if uncertainty is not None:
            uncertainty_rows.append(
                {
                    "step_id": result.step_id,
                    "cause_type": uncertainty.cause_type,
                    "retried": uncertainty.retried,
                    "postconditions_confirmed": uncertainty.postconditions_confirmed,
                    "effects_confirmed": uncertainty.effects_confirmed,
                    "resolved_by_contract": uncertainty.resolved_by_contract,
                }
            )
    refusal_rows = []
    for result in getattr(report, "results", []) or []:
        evidence = getattr(result, "safety_refusal_evidence", None)
        if evidence is not None:
            refusal_rows.append(
                {
                    "step_id": result.step_id,
                    "stage": evidence.stage,
                    "code": evidence.code,
                    "delivery_attempted": result.delivery_attempted,
                }
            )

    outcome = classify_trial(
        spec=spec,
        report=report,
        oracle=oracle,
        backend=backend,
        uncertainty_rows=uncertainty_rows,
    )
    transaction = str(getattr(report, "transaction_outcome", "") or "")
    trial_result: dict[str, Any] = {
        "condition": spec.id,
        "expectation": spec.expect,
        "trial": trial,
        "run_id": run_id,
        "passed": outcome["passed"],
        "runtime_success": bool(report.success),
        "transaction_outcome": transaction,
        "runtime_s": round(runtime_s, 3),
        "model_calls": count_model_calls(report, replayer),
        "save_delivery_attempts": backend.save_delivery_attempts,
        "uncertainty_events": list(backend.uncertainty_events),
        "delivery_uncertainty": uncertainty_rows,
        "safety_refusals": refusal_rows,
        "resolution_rungs": _resolution_rungs(report),
        "silent_incorrect_success": outcome["silent_incorrect_success"],
        "over_halt": outcome["over_halt"],
        "oracle": _oracle_result(oracle),
        "errors": [
            result.error
            for result in getattr(report, "results", []) or []
            if getattr(result, "error", None)
        ],
    }
    return trial_result


def count_model_calls(report: Any, replayer: Any) -> int:
    """The local campaign wires no grounder or VLM; assert that structurally."""

    grounder = getattr(replayer, "grounder", None) or getattr(
        replayer, "_grounder", None
    )
    if grouner_is_wired(grounder):
        raise RuntimeError("campaign requires a no-model runtime configuration")
    return 0


def grouner_is_wired(grounder: Any) -> bool:
    return grounder is not None and not isinstance(grounder, type(None))


def classify_trial(
    *,
    spec: ConditionSpec,
    report: Any,
    oracle: "fx.GateOracle",
    backend: "fx.GateFixtureBackend",
    uncertainty_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    oracle_view = _oracle_result(oracle)
    success = bool(report.success)
    transaction = str(getattr(report, "transaction_outcome", "") or "")
    attempts = backend.save_delivery_attempts
    injected = len(backend.uncertainty_events) > 0

    def halt_proves_no_effect() -> bool:
        return bool(
            not success
            and oracle_view["no_consequential_input"]
            and not oracle_view["rows"]
            and attempts == 0
        )

    silent = False
    over_halt = False
    passed = False

    if spec.expect == "verified":
        passed = bool(
            success and transaction == "VERIFIED" and oracle_view["exact_single_write"]
        )
        silent = bool(success and not oracle_view["exact_single_write"])
        over_halt = bool(not success)
    elif spec.expect == "verified_or_safe_halt":
        verified_path = bool(
            success and transaction == "VERIFIED" and oracle_view["exact_single_write"]
        )
        halted_path = halt_proves_no_effect()
        passed = bool(verified_path or halted_path)
        silent = bool(success and not oracle_view["exact_single_write"])
    elif spec.expect == "safe_halt":
        passed = halt_proves_no_effect()
        silent = bool(success or attempts > 0 or bool(oracle_view["rows"]))
    elif spec.expect == "reconciliation_required":
        passed = bool(
            injected
            and attempts == 1
            and not success
            and transaction == "RECONCILIATION_REQUIRED"
        )
        silent = bool(success)
    elif spec.expect == "verified_after_uncertainty_or_reconciliation_required":
        proved_verified = bool(
            success
            and transaction == "VERIFIED"
            and oracle_view["exact_single_write"]
            and uncertainty_rows
            and all(
                row["retried"] is False
                and row["effects_confirmed"] is True
                and row["resolved_by_contract"] is True
                for row in uncertainty_rows
            )
        )
        reconciliation = bool(
            injected
            and attempts == 1
            and not success
            and transaction == "RECONCILIATION_REQUIRED"
            and oracle_view["exact_single_write"]
        )
        passed = bool(proved_verified or reconciliation)
        silent = bool(success and not proved_verified)

    return {
        "passed": passed,
        "silent_incorrect_success": silent,
        "over_halt": over_halt,
    }


def build_summary(
    specs: tuple[ConditionSpec, ...],
    trials: list[dict[str, Any]],
    *,
    stopped_early: bool,
) -> dict[str, Any]:
    counts = {
        spec.id: sum(1 for t in trials if t["condition"] == spec.id) for spec in specs
    }
    pending = [spec.id for spec in specs if counts[spec.id] < TRIALS_PER_CONDITION]
    summary = {
        "verified_outcomes": sum(
            1
            for t in trials
            if t["runtime_success"] and t["transaction_outcome"] == "VERIFIED"
        ),
        "safe_halts": sum(
            1
            for t in trials
            if not t["runtime_success"]
            and t["oracle"]["no_consequential_input"]
            and t["save_delivery_attempts"] == 0
        ),
        "reconciliation_required_outcomes": sum(
            1 for t in trials if t["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        ),
        "silent_incorrect_successes": sum(
            1 for t in trials if t["silent_incorrect_success"]
        ),
        "over_halts": sum(1 for t in trials if t["over_halt"]),
        "wrong_record_writes": sum(
            1 for t in trials if t["oracle"]["wrong_record_write"]
        ),
        "duplicate_effects": sum(1 for t in trials if t["oracle"]["duplicate_effect"]),
        "model_calls": sum(int(t["model_calls"]) for t in trials),
        "blind_retries": sum(
            1
            for t in trials
            if t["save_delivery_attempts"] > 1
            or any(row["retried"] is not False for row in t["delivery_uncertainty"])
        ),
        "replay_dispatches": sum(1 for t in trials if t["save_delivery_attempts"] > 1),
    }
    missing = [metric for metric in REQUIRED_METRICS if metric not in summary]
    if missing:
        raise RuntimeError(
            "campaign summary is missing required gate metrics: " + ", ".join(missing)
        )
    runtimes = sorted(float(t["runtime_s"]) for t in trials)
    accepted = bool(
        not stopped_early
        and not pending
        and all(t["passed"] for t in trials)
        and summary["silent_incorrect_successes"] == 0
        and summary["over_halts"] == 0
        and summary["wrong_record_writes"] == 0
        and summary["duplicate_effects"] == 0
        and summary["model_calls"] == 0
        and summary["blind_retries"] == 0
        and summary["replay_dispatches"] == 0
        and all(
            counts.get(condition, 0) >= TRIALS_PER_CONDITION
            for condition in UNCERTAIN_CONDITIONS
        )
    )
    return {
        "schema_version": "openadapt.qualification-gate-results.v1",
        "campaign_contract": CAMPAIGN_CONTRACT,
        "campaign_id": CAMPAIGN_ID,
        "trials_per_condition": TRIALS_PER_CONDITION,
        "required_metrics": list(REQUIRED_METRICS),
        "configured_conditions": [spec.id for spec in specs],
        "condition_trial_counts": counts,
        "full_campaign_complete": not pending,
        "full_campaign_pending_conditions": pending,
        "uncertain_delivery_conditions": list(UNCERTAIN_CONDITIONS),
        "uncertain_delivery_trials": sum(
            counts.get(condition, 0) for condition in UNCERTAIN_CONDITIONS
        ),
        "run_count": len(trials),
        "stopped_early": stopped_early,
        "accepted_subset": accepted,
        **summary,
        "p50_runtime_s": round(statistics.median(runtimes), 3) if runtimes else None,
        "trials": trials,
    }


def assert_complete_summary(result: dict[str, Any]) -> None:
    """Refuse any campaign result whose counted summary is incomplete.

    The production gate fails closed when ``silent_incorrect_successes``,
    ``over_halts``, or any other required counter is absent: a summary that
    cannot prove it counted the failure classes is not evidence.
    """

    for metric in REQUIRED_METRICS:
        value = result.get(metric)
        if not isinstance(value, int) or isinstance(value, bool):
            raise RuntimeError(
                "campaign result is missing required gate metric "
                f"{metric!r} (found {value!r})"
            )


def write_results(out: Path, result: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "results.json",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="Directory for recordings, bundles, and per-trial artifacts.",
    )
    args = parser.parse_args()

    started = time.monotonic()
    work_root = (args.work_root or args.output.parent / "campaign-run").resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    try:
        specs = condition_specs()
        recording_dir = work_root / "recording"
        recording_dir.mkdir(parents=True, exist_ok=True)
        _record_demo(recording_dir)
        bundle_dir = work_root / "bundle"
        workflow = compile_recording(
            recording_dir, bundle_dir, name="qualification-gate-fixture"
        )
        workflow, bundle_dir, checkpoint_key, case_id, _save_step_id = _qualify(
            workflow, bundle_dir
        )
        gate = _build_gate(workflow, bundle_dir, work_root / "gate-oracle")

        trials: list[dict[str, Any]] = []
        for spec in specs:
            for trial in range(1, TRIALS_PER_CONDITION + 1):
                trials.append(
                    _run_trial(
                        spec=spec,
                        trial=trial,
                        workflow=workflow,
                        bundle_dir=bundle_dir,
                        checkpoint_key=checkpoint_key,
                        gate=gate,
                        case_id=case_id,
                        output_root=work_root / "trials",
                    )
                )
                print(
                    f"[gate-campaign] {spec.id} trial {trial}: "
                    f"passed={trials[-1]['passed']} "
                    f"outcome={trials[-1]['transaction_outcome']}",
                    flush=True,
                )
        result = build_summary(specs, trials, stopped_early=False)
        assert_complete_summary(result)
    except Exception as exc:  # noqa: BLE001 - fail closed with retained evidence
        result = {
            "schema_version": "openadapt.qualification-gate-results.v1",
            "campaign_contract": CAMPAIGN_CONTRACT,
            "campaign_id": CAMPAIGN_ID,
            "accepted_subset": False,
            "stopped_early": True,
            "harness_failure": {
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        }
        print(f"campaign FAILED before completion: {exc}", file=sys.stderr)
    write_results(args.output, result)
    compact = {
        key: result[key]
        for key in (
            "accepted_subset",
            "full_campaign_complete",
            "run_count",
            "verified_outcomes",
            "safe_halts",
            "reconciliation_required_outcomes",
            "silent_incorrect_successes",
            "over_halts",
            "wrong_record_writes",
            "duplicate_effects",
            "model_calls",
            "blind_retries",
            "replay_dispatches",
        )
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    print(f"campaign wall time: {time.monotonic() - started:.1f}s")
    return 0 if result["accepted_subset"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
