"""Qualify a compiled workflow from ``verify.yaml``, and optionally certify it.

Qualifying runs the compiled workflow against test cases before it may run
unattended: the normal case plus five deliberate faults. It passes only when
the normal case completes and is verified by reading back the system of
record, and every fault halts before the save instead of acting wrongly.
Certifying signs that result under a policy (see
``docs/QUALIFICATION_TERMS.md``).

This module is the generic orchestration that used to be copied into one
script per workflow. Per workflow, only ``verify.yaml`` changes.

Flow of :func:`qualify`::

    copy bundle -> apply verify.yaml (save step, record checks, duplicate key)
    -> check everything needed is there (fail with every problem at once)
    -> open the qualification project (risk classification, identity and
       effect policies, the six cases, case scope, per-campaign local keys)
    -> for each case x trial: reset, run in the Standard profile, read the
       record back independently, sign the case result
    -> run the same cases again with structure lookups disabled (the
       pixels-only pass); a gate when the target surface is pixels only
    -> record results and the lookups they used, certify under the policy
       (optional)
    -> optionally run the certified bundle once on new inputs

The keys are per-campaign local test keys (:mod:`openadapt_flow.qualify.keys`).
No production trust root is created or used.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow import __version__ as FLOW_VERSION
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.lookup_capabilities import (
    DeploymentSurface,
    PixelsOnlyPass,
    QualifiedLookup,
    capabilities_used,
    resolve_deployment_surface,
    surface_offers_structure,
)
from openadapt_flow.qualify.config import CASE_IDS, QualifyConfigError, VerifyConfig
from openadapt_flow.qualify.environment import (
    WEB_OBSERVER_CONTRACT_SHA256,
    WEB_OBSERVER_ID,
    WebEnvironmentObserver,
    web_environment_digest,
    web_environment_payload,
)
from openadapt_flow.qualify.faults import WebFaultDriver
from openadapt_flow.qualify.keys import LocalTestKey, new_local_test_key
from openadapt_flow.verification import VerificationTier

#: (case id, expected outcome, oracle kind), in run order.
CASES: tuple[tuple[str, str, str], ...] = (
    ("representative", "VERIFIED", "exact_record"),
    ("fault-ambiguity", "HALTED", "no_mutation"),
    ("fault-wrong-identity", "HALTED", "no_mutation"),
    ("fault-stale-identity", "HALTED", "no_mutation"),
    ("fault-weak-effect", "HALTED", "no_mutation"),
    ("fault-missing-effect", "HALTED", "no_mutation"),
)
assert tuple(case for case, _e, _o in CASES) == CASE_IDS


class QualifyError(RuntimeError):
    """The campaign refused. Nothing it produced may be called certified."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _evidence_bytes(path: Path) -> bytes:
    """A file's bytes; a file inside a sealed run directory is decrypted.

    Runs of a sealed bundle write a sealed run directory, whose report is
    ``report.json`` only by its logical name. Its evidence digest is over the
    plaintext; the sealed set authenticates the ciphertext on disk.
    """

    if path.is_file():
        return path.read_bytes()
    from openadapt_flow import sealed_set

    return sealed_set.read_bytes(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(_evidence_bytes(path)).hexdigest()


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    return path


def report_summary(report: RunReport) -> dict[str, Any]:
    failed = next((item for item in report.results if not item.ok), None)
    return {
        "execution_outcome": report.execution_outcome,
        "transaction_outcome": report.transaction_outcome,
        "execution_profile": report.execution_profile,
        "production_eligible": report.production_eligible,
        "success": report.success,
        "model_calls": report.model_calls,
        "total_ms": report.total_ms,
        "failed_step_id": failed.step_id if failed is not None else None,
        "failed_error": failed.error if failed is not None else None,
    }


# ---------------------------------------------------------------------------
# applying verify.yaml to the bundle
# ---------------------------------------------------------------------------


def resolve_save_step(workflow: Workflow, config: VerifyConfig) -> Any:
    """The single irreversible step the campaign qualifies."""

    if config.save_step != "auto":
        found = [s for s in workflow.steps if s.id == config.save_step]
        if not found:
            raise QualifyConfigError(
                f"save_step {config.save_step!r} is not a step of this bundle"
            )
        return found[0]
    with_effects = [s for s in workflow.steps if s.risk == "irreversible" and s.effects]
    if len(with_effects) == 1:
        return with_effects[0]
    irreversible = [s for s in workflow.steps if s.risk == "irreversible"]
    if not with_effects and len(irreversible) == 1:
        return irreversible[0]
    found_ids = [s.id for s in (with_effects or irreversible)]
    raise QualifyConfigError(
        "save_step: auto found "
        + (
            f"{len(found_ids)} candidate saves {found_ids}"
            if found_ids
            else "no irreversible step"
        )
        + "; name the save in verify.yaml (save_step: step_NNN)"
    )


def record_effects(config: VerifyConfig) -> list[Any]:
    """The save's system-of-record checks, from ``record`` in verify.yaml."""

    from openadapt_flow.runtime.effects import Effect, EffectKind

    record = config.record
    selector = {name: record.expr(name) for name in record.match}
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=selector,
        expected_count=record.expected_count,
        forbid_collateral_loss=True,
        risk="reversible",
        probe=f"exactly {record.expected_count} {record.name} record(s) written",
        timeout_s=record.timeout_s,
    )
    if record.duplicate_key is not None:
        written.idempotency_key = record.expr(record.duplicate_key)
        written.key_field = record.duplicate_key
    effects = [written]
    if record.exact_new_set:
        # Exactly these records were added since the snapshot before the
        # save, and nothing else, read by every declared field.
        selector_all = {
            name: record.expr(name) for name in [*record.match, *record.fields]
        }
        effects.append(
            Effect(
                kind=EffectKind.EXACT_NEW_SET,
                new_records=[dict(selector_all)] * record.expected_count,
                expected_count=record.expected_count,
                identity_field=record.identity_field,
                risk="reversible",
                probe=(
                    f"exactly {record.expected_count} new {record.name} record(s) "
                    "added, and nothing else"
                ),
                timeout_s=record.timeout_s,
            )
        )
    for name in record.fields:
        if name in record.match:
            continue
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=selector,
                field=name,
                value=record.expr(name),
                risk="reversible",
                probe=f"{record.name} field {name!r} read-back",
                timeout_s=record.timeout_s,
            )
        )
    return effects


def apply_verify_config(
    workflow: Workflow, config: VerifyConfig
) -> tuple[Any, list[dict[str, Any]]]:
    """Bind the save step and its record checks; return (save step, notes)."""

    from openadapt_flow.runtime.effects import ValueExpr

    notes: list[dict[str, Any]] = []
    save = resolve_save_step(workflow, config)
    if save.risk != "irreversible":
        save.risk = "irreversible"
        save.risk_explanation = "named as the save in verify.yaml"
        save.risk_review_required = False
        notes.append({"change": "save_step_irreversible", "step": save.id})
    if config.effects is not None:
        save.effects = [effect.model_copy(deep=True) for effect in config.effects]
        notes.append(
            {
                "change": "effects_from_verify_yaml",
                "step": save.id,
                "effects": len(save.effects),
            }
        )
    elif config.record.fields:
        save.effects = record_effects(config)
        notes.append(
            {
                "change": "record_checks_from_verify_yaml",
                "step": save.id,
                "effects": len(save.effects),
            }
        )
    elif config.record.duplicate_key is not None:
        for effect in save.effects:
            if effect.kind.value == "record_written" and effect.idempotency_key is None:
                effect.idempotency_key = ValueExpr(param=config.record.duplicate_key)
                effect.key_field = config.record.duplicate_key
                notes.append({"change": "duplicate_key", "step": save.id})
    if (
        config.minimum_readback is not None
        and workflow.minimum_readback != config.minimum_readback
    ):
        workflow.minimum_readback = config.minimum_readback
        notes.append({"change": "minimum_readback", "minimum": config.minimum_readback})
    return save, notes


def resolve_inputs(
    workflow: Workflow, config: VerifyConfig, *, campaign_id: str
) -> dict[str, str]:
    inputs = config.inputs
    params = {k: str(v) for k, v in (workflow.params or {}).items()}
    if inputs.source == "file":
        path = config.resolve_path(inputs.file or "")
        if not path.is_file():
            raise QualifyConfigError(f"inputs.file not found: {path}")
        loaded = json.loads(path.read_text())
        if not isinstance(loaded, dict):
            raise QualifyConfigError(f"inputs.file must hold a JSON object: {path}")
        params.update({k: str(v) for k, v in loaded.items()})
    params.update(inputs.params)
    suffix = hashlib.sha256(campaign_id.encode()).hexdigest()[:6]
    for name in inputs.unique:
        value = params.get(name)
        if value is None:
            continue
        if "@" in value:
            local, _, domain = value.partition("@")
            params[name] = f"{local}+q{suffix}@{domain}"
        else:
            params[name] = f"{value}q{suffix}"
    return params


def check_ready(
    workflow: Workflow, config: VerifyConfig, save: Any, params: Mapping[str, str]
) -> list[str]:
    """Every problem that would stop the campaign, found before it runs."""

    problems: list[str] = []
    if config.app.target != "web":
        problems.append(
            f"app.target {config.app.target!r}: qualify runs web campaigns today; "
            "the pixel-surface fault driver for Windows, RDP and Citrix is not built"
        )
    if (
        config.app.reset is None
        and config.campaign.trials * len(config.campaign.cases) > 1
    ):
        problems.append(
            "app.reset is missing: every trial must start from the same baseline "
            "(command, http or python)"
        )
    if not params:
        problems.append(
            "no test inputs: the bundle recorded none and inputs gives none"
        )
    specs = getattr(workflow, "param_specs", None) or {}
    for name, spec in specs.items():
        if getattr(spec, "required", True) and name not in params:
            problems.append(f"inputs: no value for required parameter {name!r}")
    for name in config.inputs.unique:
        if name not in params:
            problems.append(f"inputs.unique names unknown parameter {name!r}")
    for name, value in config.record.fields.items():
        expr = config.record.expr(name)
        if expr.param is not None and expr.param not in params:
            problems.append(
                f"record.fields.{name} uses parameter {expr.param!r}, which has no input"
            )
    if save.anchor is None or not save.identity_armed:
        problems.append(f"save step {save.id} has no armed identity check")
    if not save.effects:
        problems.append(
            f"save step {save.id} has no record check: add effects (or "
            "record.fields and record.match) to verify.yaml"
        )
    if any(effect.needs_operator_confirmation for effect in save.effects):
        problems.append(
            f"save step {save.id} has an unconfirmed placeholder record check"
        )
    if save.anchor is not None and (
        save.anchor.structural is None or not save.anchor.structural.selector
    ):
        problems.append(
            f"save step {save.id} has no structural selector: the web fault "
            "driver needs it for the ambiguity and identity faults"
        )
    return problems


# ---------------------------------------------------------------------------
# the qualification project
# ---------------------------------------------------------------------------


@dataclass
class CampaignSetup:
    runner_key: LocalTestKey
    driver: WebFaultDriver
    observer: WebEnvironmentObserver
    environment: dict[str, Any]


def configure_project(
    workflow: Workflow,
    config: VerifyConfig,
    *,
    save: Any,
    params: Mapping[str, str],
    environment: dict[str, Any],
    campaign_id: str,
    minimum_effect_tier: Any,
) -> CampaignSetup:
    """Open the project: classify, set policies, add and scope the six cases."""

    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        IdentityEnforcement,
        IdentityPolicy,
        QualificationActionTarget,
        QualificationCase,
        QualificationCaseKind,
        QualificationOutcome,
        add_case,
        init_project,
        set_action_classification,
        set_case_scope,
        set_effect_policy,
        set_identity_policy,
        set_trusted_fault_driver_key,
        set_trusted_runner_key,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    app = config.app
    capabilities = sorted(
        {
            "headless_chromium" if app.headless else "headed_chromium",
            "independent_system_of_record",
            "playwright_dom",
        }
    )
    boundary = EnvironmentBoundary(
        target_kind="web",
        application=app.name,
        application_identity=app.origin,
        application_version=app.version,
        environment_observer_id=WEB_OBSERVER_ID,
        environment_observer_contract_sha256=WEB_OBSERVER_CONTRACT_SHA256,
        environment_digest=web_environment_digest(environment),
        runtime_version=FLOW_VERSION,
        required_capabilities=capabilities,
    )
    # Re-qualifying a bundle that already has a project (a sealed copy, a
    # repaired copy, or one certified before) replaces the project with a new
    # revision of it: the same project id, revision + 1, linked to the prior
    # revision's digest. Every case runs again; nothing carries over.
    prior = workflow.qualification
    project = init_project(
        workflow,
        environment=boundary,
        minimum_effect_tier=minimum_effect_tier,
        replace=prior is not None,
    )
    if prior is not None:
        project.project_id = prior.project_id
        project.revision = prior.revision + 1
        project.previous_revision_sha256 = prior.revision_digest()
    record = config.record.name
    for step in workflow.steps:
        irreversible = step.risk == "irreversible"
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=(
                    ActionRiskClass.IRREVERSIBLE
                    if irreversible
                    else ActionRiskClass.READ_ONLY
                ),
                explanation=(
                    f"writes the {record} in the system of record (verify.yaml save)"
                    if step.id == save.id
                    else "compiled as an irreversible write"
                    if irreversible
                    else f"fills or navigates; writes no {record}"
                ),
                operator_confirmed=True,
            ),
        )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id=save.id, enforcement=IdentityEnforcement.CANONICAL_LADDER
        ),
    )
    for index, _effect in enumerate(save.effects):
        set_effect_policy(
            workflow, step_id=save.id, effect_index=index, tier=minimum_effect_tier
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
            description=f"Recorded workflow on {app.name} under its qualified boundary",
        ),
    )
    runner_key = new_local_test_key(f"{campaign_id}-runner")
    set_trusted_runner_key(
        workflow,
        key_id=runner_key.key_id,
        public_key_base64=runner_key.public_key_base64,
    )
    driver = WebFaultDriver(workflow, key_id=f"{campaign_id}-fault-driver")
    set_trusted_fault_driver_key(
        workflow,
        key_id=driver.attestation_key_id,
        public_key_base64=driver.public_key_base64,
    )
    input_sha256 = runtime_inputs_digest(workflow, dict(params), None)
    target = QualificationActionTarget(step_id=save.id, actuation_path="gui")
    for case in list(project.cases):
        set_case_scope(
            workflow,
            case_id=case.id,
            runtime_input_sha256=input_sha256,
            action_targets=[target],
            fault_target=None
            if case.kind is QualificationCaseKind.REPRESENTATIVE
            else target,
        )
    return CampaignSetup(
        runner_key=runner_key,
        driver=driver,
        observer=WebEnvironmentObserver(
            application_version=app.version, headless=app.headless
        ),
        environment=environment,
    )


# ---------------------------------------------------------------------------
# the independent read-back after each trial
# ---------------------------------------------------------------------------


def _records_reader(
    verifier: Any,
) -> Optional[Callable[[], Optional[list[dict[str, Any]]]]]:
    for candidate in (
        verifier,
        getattr(verifier, "inner", None),
        getattr(verifier, "_inner", None),
    ):
        fetch = getattr(candidate, "_fetch_records", None)
        if callable(fetch):
            return fetch
    return None


def _canon(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str)


class RecordReadback:
    """Read the system of record before and after a trial; judge the trial.

    ``exact_record``: exactly the expected number of records match the save's
    selector, every declared field equals its expected value, and no record
    that existed before changed. ``no_mutation``: nothing matches and no
    record changed. Uses the configured verifier's own read path (REST today);
    other kinds fall back to the run report and say so.
    """

    def __init__(self, verifier: Any, save: Any, params: Mapping[str, str]) -> None:
        self._read = _records_reader(verifier)
        self._effects = [effect.resolve(params) for effect in save.effects]
        self.before: Optional[list[dict[str, Any]]] = None

    @property
    def independent(self) -> bool:
        return self._read is not None

    def snapshot(self) -> None:
        if self._read is not None:
            self.before = self._read()

    def judge(self, oracle_kind: str, report: RunReport) -> dict[str, Any]:
        base = {
            "schema_version": "openadapt.qualify-readback/v1",
            "oracle_kind": oracle_kind,
        }
        if self._read is None or self.before is None:
            if oracle_kind == "exact_record":
                passed = report.execution_outcome == "VERIFIED"
            else:
                passed = report.execution_outcome == "HALTED" and not any(
                    r.delivery_attempted for r in report.results if r.step_id
                )
            return {
                **base,
                "read_path": "run report only (this verifier kind has no records read)",
                "independent": False,
                "passed": passed,
                "observed": oracle_kind if passed else f"not_{oracle_kind}",
                "silent_incorrect_success": bool(not passed and report.success),
                "effect_verdict": "INDETERMINATE",
            }
        after = self._read() or []
        written = next(
            (e for e in self._effects if e.kind.value == "record_written"), None
        )
        selector = {
            k: v.literal if hasattr(v, "literal") else v
            for k, v in ((written.match if written else {}) or {}).items()
        }

        def matches(rec: Mapping[str, Any], sel: Mapping[str, Any]) -> bool:
            return all(str(rec.get(k)) == str(v) for k, v in sel.items())

        matched = [rec for rec in after if selector and matches(rec, selector)]
        after_set = {_canon(rec) for rec in after}
        changed_existing = [rec for rec in self.before if _canon(rec) not in after_set]
        field_checks = []
        for effect in self._effects:
            if effect.kind.value != "field_equals" or effect.field is None:
                continue
            want = effect.value.literal if effect.value is not None else None
            ok = len(matched) == 1 and str(matched[0].get(effect.field)) == str(want)
            field_checks.append({"field": effect.field, "match": ok})
        expected = written.expected_count if written else 1
        exact = (
            len(matched) == expected
            and not changed_existing
            and all(item["match"] for item in field_checks)
        )
        nothing = (
            not matched and not changed_existing and len(after) == len(self.before)
        )
        if oracle_kind == "exact_record":
            passed = exact
            observed = "exact_record" if passed else "record_mismatch"
        elif oracle_kind == "no_mutation":
            passed = (
                not matched and not changed_existing and len(after) == len(self.before)
            )
            observed = "no_mutation" if passed else "unexpected_mutation"
        else:
            raise QualifyError(f"unknown oracle kind {oracle_kind!r}")
        return {
            **base,
            "read_path": "system_of_record verifier records read",
            "independent": True,
            "passed": passed,
            "observed": observed,
            # RFC-0002 verdict of the read itself, whatever the case expected.
            "effect_verdict": "CONFIRMED" if exact else "REFUTED",
            "landed": "exact" if exact else ("none" if nothing else "some"),
            "records_before": len(self.before),
            "records_after": len(after),
            "matching_records": len(matched),
            "existing_records_changed": len(changed_existing),
            "fields_matched": sum(item["match"] for item in field_checks),
            "fields_expected": len(field_checks),
            "field_check": field_checks,
            "silent_incorrect_success": bool(not passed and report.success),
        }


# ---------------------------------------------------------------------------
# hooks for callers that need more than verify.yaml (demos, packs)
# ---------------------------------------------------------------------------


@dataclass
class TrialContext:
    case_id: str
    expected: str
    oracle_kind: str
    trial: int
    trial_dir: Path
    run_dir: Path
    run_id: str
    params: dict[str, str]
    certified_run: bool = False
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualifyHooks:
    """Optional extension points. Each defaults to the verify.yaml behaviour.

    - ``open_session`` / ``reset`` / ``make_verifier`` replace the
      declarative ones;
    - ``before_trial(ctx)`` runs after reset, before the browser opens;
    - ``replayer_kwargs(ctx)`` adds Replayer options (presentation overlays);
    - ``after_run(ctx, backend, report)`` runs before the browser closes;
    - ``after_close(ctx, report)`` runs once the run report is rendered;
    - ``oracle(ctx, report)`` replaces the generic read-back judgement;
    - ``extra_checks`` each return ``(name, result)``; ``result["passed"]``
      must be true, and the result is written next to the trial as
      ``<name>.json`` (a demo's second oracle, for example).
    - ``open_session_for(ctx)`` replaces ``open_session`` when the session
      depends on the trial (video for trial one, for example).
    """

    open_session: Optional[Callable[[], tuple[Any, Callable[[], None]]]] = None
    open_session_for: Optional[
        Callable[[TrialContext], tuple[Any, Callable[[], None]]]
    ] = None
    reset: Optional[Callable[[], Any]] = None
    make_verifier: Optional[Callable[[Mapping[str, object]], Any]] = None
    before_trial: Optional[Callable[[TrialContext], None]] = None
    replayer_kwargs: Optional[Callable[[TrialContext], dict[str, Any]]] = None
    after_run: Optional[Callable[[TrialContext, Any, RunReport], None]] = None
    after_close: Optional[Callable[[TrialContext, RunReport], None]] = None
    oracle: Optional[Callable[[TrialContext, RunReport], dict[str, Any]]] = None
    extra_checks: list[
        Callable[[TrialContext, RunReport], tuple[str, dict[str, Any]]]
    ] = field(default_factory=list)
    settle_s: float = 1.0
    runner_id: Optional[str] = None


# ---------------------------------------------------------------------------
# one trial, one case result
# ---------------------------------------------------------------------------


def _run(
    *,
    workflow: Workflow,
    bundle_dir: Path,
    config: VerifyConfig,
    hooks: QualifyHooks,
    ctx: TrialContext,
    setup: CampaignSetup,
    authorize: Callable[[Any, dict[str, Any]], Any],
    fault_driver: Any,
    marks: dict[str, float],
    lookup: Optional[Mapping[str, Any]] = None,
    pixels_only: bool = False,
) -> tuple[RunReport, Any]:
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.runtime import Replayer

    opener = (
        (lambda: hooks.open_session_for(ctx))  # type: ignore[misc]
        if hooks.open_session_for is not None
        else hooks.open_session
    )
    assert opener is not None and hooks.make_verifier is not None
    backend, close = opener()
    marks["browser_ready"] = time.time()
    try:
        verifier = hooks.make_verifier(ctx.params)
        gate_kwargs = dict(
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(
                policy=PolicySection(
                    policy=ctx.state.get("policy", config.campaign.policy)
                )
            ),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        authorization = authorize(verifier, gate_kwargs)
        marks["admitted"] = time.time()
        kwargs: dict[str, Any] = dict(
            effect_verifier=verifier,
            governed_authorization=authorization,
            qualification_fault_driver=fault_driver,
            qualification_environment_observer=setup.observer,
            durable=True,
            require_settled=True,
            pixel_verify_enabled=any(
                s.anchor is not None and s.anchor.identifier_crop
                for s in workflow.steps
            ),
        )
        if workflow.encrypted:
            # An encrypted bundle's runs keep their durable checkpoints under
            # the same key (Standard and Regulated require them encrypted).
            from openadapt_flow import crypto

            kwargs["checkpoint_key"] = crypto.resolve_key(None)
        if hooks.replayer_kwargs is not None:
            kwargs.update(hooks.replayer_kwargs(ctx))
        # The signed lookup settings win over any hook, so a hook can't turn
        # structure back on during a pass that must not use it.
        kwargs.update(lookup or {})
        if pixels_only:
            # The pixels-only pass: the structural rung never runs, whatever
            # the hooks asked for.
            kwargs["use_structural"] = False
        marks["task_start"] = time.time()
        report = Replayer(backend, **kwargs).run(
            workflow.model_copy(deep=True),
            params=dict(ctx.params),
            bundle_dir=bundle_dir,
            run_dir=ctx.run_dir,
            execution_target_kind="web",
            execution_origin=config.app.origin,
            execution_entry_url=backend.page.url,
            run_id=ctx.run_id,
        )
        marks["task_end"] = time.time()
        if hooks.after_run is not None:
            hooks.after_run(ctx, backend, report)
        return report, verifier
    finally:
        close()
        marks["browser_closed"] = time.time()


def _judge_trial(
    *,
    hooks: QualifyHooks,
    ctx: TrialContext,
    report: RunReport,
    readback: RecordReadback,
) -> dict[str, Any]:
    from openadapt_flow.report import render_run_report

    time.sleep(hooks.settle_s)
    render_run_report(ctx.run_dir)
    if hooks.after_close is not None:
        hooks.after_close(ctx, report)
    oracle = (
        hooks.oracle(ctx, report)
        if hooks.oracle is not None
        else readback.judge(ctx.oracle_kind, report)
    )
    write_json(ctx.trial_dir / "oracle.json", oracle)
    extra: dict[str, Any] = {}
    for check in hooks.extra_checks:
        name, result = check(ctx, report)
        write_json(ctx.trial_dir / f"{name}.json", result)
        extra[name] = result
    return {"oracle": oracle, "extra": extra}


def _evidence_ref(root: Path, path: Path, kind: Any) -> Any:
    from openadapt_flow.qualification import EvidenceRef

    return EvidenceRef(
        kind=kind, sha256=_sha256(path), relative_path=path.relative_to(root).as_posix()
    )


def case_result(
    *,
    root: Path,
    workflow: Workflow,
    case_id: str,
    observed_outcome: str,
    run_id: str,
    trial_dir: Path,
    runner_key: LocalTestKey,
    runner_id: str,
    case_params: Mapping[str, str],
    campaign_id: str,
) -> Any:
    """The signed result of one case trial, bound to every retained file."""

    from openadapt_flow.qualification import (
        QualificationCaseResult,
        QualificationOutcome,
        qualification_campaign_id_sha256,
        qualification_run_id_sha256,
        sign_case_result,
        workflow_contract_sha256,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    project = workflow.qualification
    if project is None:
        raise QualifyError("the workflow's qualification project disappeared")
    run_dir = trial_dir / "run"
    from openadapt_flow import sealed_set

    # A sealed bundle's run directory is a sealed set: its files are bound by
    # their logical names and plaintext digests, as the run report names them.
    run_files = (
        sealed_set.list_files(run_dir)
        if sealed_set.is_sealed_path(run_dir)
        else [path for path in run_dir.rglob("*") if path.is_file()]
    )
    run_artifacts = [
        _evidence_ref(root, path, "other")
        for path in sorted(run_files)
        if path.name != "report.json"
    ]
    faults = (
        [
            _evidence_ref(root, trial_dir / "fault-receipt.json", "fault_receipt"),
            _evidence_ref(root, trial_dir / "fault-mutation.bin", "fault_mutation"),
        ]
        if (trial_dir / "fault-receipt.json").is_file()
        else []
    )
    result = QualificationCaseResult(
        case_id=case_id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id=runner_id,
        runner_capabilities=list(project.environment.required_capabilities),
        status="passed",
        observed_outcome=QualificationOutcome(observed_outcome.lower()),
        campaign_id_sha256=qualification_campaign_id_sha256(campaign_id),
        case_input_sha256=runtime_inputs_digest(workflow, dict(case_params), None),
        run_id_sha256=qualification_run_id_sha256(run_id),
        evidence=[
            _evidence_ref(root, run_dir / "report.json", "run_report"),
            _evidence_ref(root, trial_dir / "case-input.json", "case_input"),
            _evidence_ref(root, trial_dir / "oracle.json", "effect"),
            *run_artifacts,
            *faults,
        ],
        detail_code=f"{case_id}.{run_id.rsplit('-', 1)[-1]}",
        attestation_key_id=runner_key.key_id,
    )
    return sign_case_result(result, private_key=runner_key.private_key)


# ---------------------------------------------------------------------------
# lookup capabilities and the pixels-only pass
# ---------------------------------------------------------------------------


def lookup_plan(config: VerifyConfig) -> tuple[DeploymentSurface, bool]:
    """Return the target surface and whether its lookups may use structure.

    ``app.surface`` in verify.yaml names the deployment surface; an RDP or
    Citrix target, or ``unknown``, resolves to ``remote_display`` (pixels
    only).
    """

    surface = resolve_deployment_surface(config.app.surface, config.app.target)
    return surface, surface_offers_structure(surface, config.app.target)


def pixels_only_trial_failure(
    report: RunReport,
    expected: str,
    oracle: Mapping[str, Any],
    extra: Mapping[str, Mapping[str, Any]],
) -> Optional[str]:
    """Why one pixels-only trial did not behave as required, or None."""

    if report.execution_outcome != expected:
        reason = f"observed {report.execution_outcome}, expected {expected}"
        if report.halt is not None:
            reason += f" ({report.halt.reason})"
        return reason
    if report.model_calls != 0:
        return "called a model"
    if not oracle.get("passed") or oracle.get("silent_incorrect_success"):
        return f"read-back {oracle.get('observed')}"
    bad = [name for name, result in extra.items() if not result.get("passed")]
    if bad:
        return f"extra check(s) failed: {bad}"
    return None


def summarize_pixels_only(
    outcomes: list[tuple[str, int, Optional[str]]],
    *,
    role: str,
) -> PixelsOnlyPass:
    """Fold ``(case, trial, failure-or-None)`` rows into the pass record."""

    cases: list[str] = []
    for case_id, _trial, _failure in outcomes:
        if case_id not in cases:
            cases.append(case_id)
    failures = [
        f"{case_id} trial {trial}: {failure}"[:300]
        for case_id, trial, failure in outcomes
        if failure is not None
    ]
    return PixelsOnlyPass(
        role=role,  # type: ignore[arg-type]
        cases=cases,
        trials_run=len(outcomes),
        trials_passed=sum(1 for _c, _t, failure in outcomes if failure is None),
        failures=failures[:200],
    )


def _pixels_only_pass(
    *,
    workflow: Workflow,
    bundle_dir: Path,
    config: VerifyConfig,
    hooks: QualifyHooks,
    setup: CampaignSetup,
    out: Path,
    selected: list[str],
    n_trials: int,
    params: Mapping[str, str],
    save: Any,
    project_cases: Mapping[str, Any],
    policy_name: str,
    campaign_id: str,
    run_id_prefix: Optional[str],
    lookup: Mapping[str, Any],
    log: Callable[[str], None],
) -> PixelsOnlyPass:
    """Run every selected case again with structure lookups disabled.

    Informational: these runs are not signed and never fail the campaign.
    Each trial resets the test system and is judged by the same independent
    read-back as the signed cases. Evidence goes to ``out/pixels-only``.
    """

    from openadapt_flow.qualification import QualificationCaseKind
    from openadapt_flow.run_gate import (
        build_qualification_case_authorization,
        evaluate_run_gate,
    )

    outcomes: list[tuple[str, int, Optional[str]]] = []
    for case_id, expected, oracle_kind in CASES:
        if case_id not in selected:
            continue
        qcase = project_cases[case_id]
        for trial in range(1, n_trials + 1):
            trial_dir = out / "pixels-only" / case_id / f"trial-{trial:02d}"
            ctx = TrialContext(
                case_id=case_id,
                expected=expected,
                oracle_kind=oracle_kind,
                trial=trial,
                trial_dir=trial_dir,
                run_dir=trial_dir / "run",
                run_id=(
                    f"{run_id_prefix or campaign_id}-pixels-only-{case_id}"
                    f"-trial-{trial:02d}"
                ),
                params=dict(params),
                state={"policy": policy_name, "pixels_only": True},
            )

            def authorize(
                _verifier: Any,
                gate_kwargs: dict[str, Any],
                _q: Any = qcase,
                _c: Any = ctx,
            ) -> Any:
                gate = evaluate_run_gate(
                    workflow, qualification_evidence_only=True, **gate_kwargs
                )
                if not gate.passed:
                    raise QualifyError(gate.render())
                return build_qualification_case_authorization(
                    workflow,
                    gate,
                    case_id=_q.id,
                    params=dict(params),
                    worklists=None,
                    campaign_id=campaign_id,
                    run_id=_c.run_id,
                    fault_driver=(
                        None
                        if _q.kind is QualificationCaseKind.REPRESENTATIVE
                        else setup.driver
                    ),
                )

            failure: Optional[str]
            try:
                trial_dir.mkdir(parents=True)
                if hooks.reset is not None:
                    hooks.reset()
                assert hooks.make_verifier is not None
                readback = RecordReadback(hooks.make_verifier(params), save, params)
                readback.snapshot()
                if hooks.before_trial is not None:
                    hooks.before_trial(ctx)
                report, _verifier = _run(
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    config=config,
                    hooks=hooks,
                    ctx=ctx,
                    setup=setup,
                    authorize=authorize,
                    fault_driver=setup.driver,
                    marks={"start": time.time()},
                    lookup=lookup,
                    pixels_only=True,
                )
                judged = _judge_trial(
                    hooks=hooks, ctx=ctx, report=report, readback=readback
                )
                failure = pixels_only_trial_failure(
                    report, expected, judged["oracle"], judged["extra"]
                )
            except Exception as exc:  # informational: record, never raise
                first = (str(exc).splitlines() or [""])[0]
                failure = f"{type(exc).__name__}: {first}"[:240]
            outcomes.append((case_id, trial, failure))
            log(
                json.dumps(
                    {
                        "pixels_only": True,
                        "case_id": case_id,
                        "trial": trial,
                        "passed": failure is None,
                        "failure": failure,
                    }
                )
            )
    return summarize_pixels_only(outcomes, role="informational")


def qualified_lookup(
    *,
    surface: DeploymentSurface,
    target_kind: str,
    structure_offered: bool,
    reports: Mapping[str, list[RunReport]],
    pixels_only: PixelsOnlyPass,
) -> QualifiedLookup:
    """The qualified-lookup record for the signed cases' run reports."""

    return QualifiedLookup(
        surface=surface,
        target_kind=target_kind,
        structure_offered=structure_offered,
        capabilities_used=capabilities_used(
            [report for runs in reports.values() for report in runs],
            structure_offered=structure_offered,
        ),
        pixels_only=pixels_only,
    )


# ---------------------------------------------------------------------------
# the campaign
# ---------------------------------------------------------------------------


@dataclass
class QualifyResult:
    passed: bool
    certified: bool
    policy: Optional[str]
    out: Path
    bundle_dir: Path
    report: Any
    rows: list[dict[str, Any]]
    timing: dict[str, Any]
    notes: list[dict[str, Any]]
    certified_run: Optional[dict[str, Any]] = None
    #: Every case's run reports, in trial order.
    reports: dict[str, list[RunReport]] = field(default_factory=dict)
    #: The environment observer, for runs after certification.
    observer: Any = None
    environment_digest: Optional[str] = None
    #: ``openadapt.qualification-result/v1``.
    result: Optional[dict[str, Any]] = None
    #: Why no admission envelope was written, when certify did not write one.
    admission_note: Optional[str] = None
    #: The lookups the signed cases used and the surface they ran for.
    lookup: Optional[QualifiedLookup] = None

    @property
    def pixels_only(self) -> Optional[PixelsOnlyPass]:
        """The pass run with structure lookups disabled."""

        return self.lookup.pixels_only if self.lookup is not None else None

    @property
    def partial(self) -> bool:
        """True when only some of the six cases ran (each behaved as required).

        A partial run is a development check, never a qualification.
        """

        return len({row["case_id"] for row in self.rows}) < len(CASES)


def _default_hooks(config: VerifyConfig, hooks: Optional[QualifyHooks]) -> QualifyHooks:
    from openadapt_flow.qualify.session import (
        reset_runner,
        session_opener,
        verifier_factory,
    )

    hooks = hooks or QualifyHooks()
    if hooks.open_session is None and hooks.open_session_for is None:
        hooks.open_session = session_opener(config)
    if hooks.reset is None:
        hooks.reset = reset_runner(config)
    if hooks.make_verifier is None:
        hooks.make_verifier = verifier_factory(config)
    return hooks


def qualify(
    bundle_src: Path | str,
    config: VerifyConfig,
    *,
    out: Path | str,
    cases: Optional[list[str]] = None,
    trials: Optional[int] = None,
    certify: Optional[str] = None,
    run_params: Optional[Mapping[str, str]] = None,
    hooks: Optional[QualifyHooks] = None,
    campaign_id: Optional[str] = None,
    run_id_prefix: Optional[str] = None,
    evidence_root: Optional[Path | str] = None,
    in_place: bool = False,
    write_campaign_files: bool = True,
    log: Callable[[str], None] = print,
) -> QualifyResult:
    """Run the campaign; certify under ``certify`` (a policy name) if given.

    Raises :class:`QualifyConfigError` before running anything when
    verify.yaml or the bundle is missing something, and :class:`QualifyError`
    when a case does not behave as required or certification refuses.

    Layout: ``out/bundle`` (a copy, or ``bundle_src`` itself with
    ``in_place``), ``out/cases/<case>/trial-NN`` and ``out/qualification``.
    ``evidence_root`` (default ``out``) is the root case evidence is relative
    to. ``write_campaign_files=False`` leaves out the campaign summary,
    timing, notes and ``QUALIFICATION.md`` (for packs that list every file).
    """

    from openadapt_flow.policy import load_policy
    from openadapt_flow.qualification import (
        QualificationCaseKind,
        certify_project,
        evaluate_qualification,
        record_case_results,
        save_qualified_workflow,
    )
    from openadapt_flow.run_gate import (
        build_qualification_case_authorization,
        evaluate_run_gate,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_bytes

    t0 = time.time()
    bundle_src = Path(bundle_src)
    out = Path(out)
    root = Path(evidence_root) if evidence_root is not None else out
    if (out / "cases").exists() or (out.exists() and not in_place):
        raise QualifyError(f"output already exists: {out}")
    selected = list(cases or config.campaign.cases)
    unknown = [c for c in selected if c not in CASE_IDS]
    if unknown:
        raise QualifyConfigError(f"unknown case(s) {unknown}; known: {list(CASE_IDS)}")
    n_trials = trials or config.campaign.trials
    if certify is not None:
        load_policy(certify)  # fail before any run on an unknown policy
        if set(selected) != set(CASE_IDS):
            raise QualifyConfigError(
                "--certify needs all six cases; certification refuses a partial campaign"
            )
    campaign_id = (
        campaign_id or config.campaign.id or f"qualify-{bundle_src.name}-{int(t0)}"
    )

    workflow = Workflow.load(bundle_src)
    save, notes = apply_verify_config(workflow, config)
    params = resolve_inputs(workflow, config, campaign_id=campaign_id)
    problems = check_ready(workflow, config, save, params)
    hooks = _default_hooks(config, hooks)
    if config.app.reset is None and hooks.reset is not None:
        problems = [p for p in problems if not p.startswith("app.reset")]
    if problems:
        raise QualifyConfigError(problems)
    surface, structure_offered = lookup_plan(config)
    lookup_kwargs: dict[str, Any] = {
        "use_structural": structure_offered,
        "deployment_surface": surface,
    }

    if in_place:
        bundle_dir = bundle_src
        out.mkdir(parents=True, exist_ok=True)
    else:
        bundle_dir = out / "bundle"
        out.mkdir(parents=True)
        shutil.copytree(bundle_src, bundle_dir)
    if notes:
        save_qualified_workflow(workflow, bundle_dir)
        workflow = Workflow.load(bundle_dir)
        save = next(s for s in workflow.steps if s.id == save.id)
    if write_campaign_files:
        write_json(out / "qualify-notes.json", notes)
        write_json(out / "verify.resolved.json", config.model_dump(mode="json"))

    # The environment boundary is read from a live session.
    assert hooks.make_verifier is not None
    probe_verifier = hooks.make_verifier(params)
    verifier_tier = getattr(probe_verifier, "verification_tier", None)
    if verifier_tier is None:
        raise QualifyConfigError("verifier declares no verification tier")
    verifier_tier = VerificationTier(verifier_tier)
    from openadapt_flow.readback_ladder import workflow_tier_floor

    tier = config.minimum_tier()
    wanted = (
        f"minimum_readback {config.minimum_readback}"
        if config.minimum_readback is not None
        else f"minimum_effect_strength {config.minimum_effect_strength}"
    )
    if int(verifier_tier) > int(tier):
        raise QualifyConfigError(
            f"verifier reads at {verifier_tier.name.lower()}, weaker than {wanted}"
        )
    if workflow.minimum_readback is not None:
        workflow_floor = workflow_tier_floor(workflow)
        if workflow_floor is not None and int(verifier_tier) > int(workflow_floor):
            raise QualifyConfigError(
                f"verifier reads at {verifier_tier.name.lower()}, weaker than the "
                f"workflow's minimum_readback {workflow.minimum_readback}"
            )
    from openadapt_flow.qualify.result import (
        build_result,
        case_entry,
        local_admission,
        result_sha256,
        sha256_hex,
        verifier_identity_of,
        verify_config_sha256,
    )

    verifier_id = verifier_identity_of(config.verifier.verifier, probe_verifier)
    verify_sha = verify_config_sha256(verifier_id, list(save.effects))
    if hooks.reset is not None:
        hooks.reset()
    opener = hooks.open_session or (
        lambda: hooks.open_session_for(  # type: ignore[misc]
            TrialContext(
                "environment", "", "", 0, out, out, "environment", dict(params)
            )
        )
    )
    backend, close = opener()
    try:
        environment = web_environment_payload(
            backend,
            application_version=config.app.version,
            headless=config.app.headless,
        )
    finally:
        close()
    setup = configure_project(
        workflow,
        config,
        save=save,
        params=params,
        environment=environment,
        campaign_id=campaign_id,
        minimum_effect_tier=tier,
    )
    save_qualified_workflow(workflow, bundle_dir)
    workflow = Workflow.load(bundle_dir)
    save = next(s for s in workflow.steps if s.id == save.id)
    runner_id = (
        hooks.runner_id
        or f"openadapt-flow/qualify-web-{'headless' if config.app.headless else 'headed'}"
    )
    policy_name = certify or config.campaign.policy

    rows: list[dict[str, Any]] = []
    case_entries: list[dict[str, Any]] = []
    reports: dict[str, list[RunReport]] = {}
    results: list[Any] = []
    trial_times: list[float] = []
    reset_times: list[float] = []
    project_cases = {
        c.id: c
        for c in (workflow.qualification.cases if workflow.qualification else [])
    }
    for case_id, expected, oracle_kind in CASES:
        if case_id not in selected:
            continue
        qcase = project_cases[case_id]
        for trial in range(1, n_trials + 1):
            trial_dir = out / "cases" / case_id / f"trial-{trial:02d}"
            ctx = TrialContext(
                case_id=case_id,
                expected=expected,
                oracle_kind=oracle_kind,
                trial=trial,
                trial_dir=trial_dir,
                run_dir=trial_dir / "run",
                run_id=f"{run_id_prefix or campaign_id}-{case_id}-trial-{trial:02d}",
                params=dict(params),
                state={"policy": policy_name},
            )
            trial_dir.mkdir(parents=True)
            t_trial = time.time()
            if hooks.reset is not None:
                hooks.reset()
            reset_times.append(time.time() - t_trial)
            readback = RecordReadback(hooks.make_verifier(params), save, params)
            readback.snapshot()
            if hooks.before_trial is not None:
                hooks.before_trial(ctx)

            def authorize(
                _verifier: Any,
                gate_kwargs: dict[str, Any],
                _q: Any = qcase,
                _c: Any = ctx,
            ) -> Any:
                gate = evaluate_run_gate(
                    workflow, qualification_evidence_only=True, **gate_kwargs
                )
                if not gate.passed:
                    raise QualifyError(gate.render())
                return build_qualification_case_authorization(
                    workflow,
                    gate,
                    case_id=_q.id,
                    params=dict(params),
                    worklists=None,
                    campaign_id=campaign_id,
                    run_id=_c.run_id,
                    fault_driver=(
                        None
                        if _q.kind is QualificationCaseKind.REPRESENTATIVE
                        else setup.driver
                    ),
                )

            marks: dict[str, float] = {"start": time.time()}
            report, _verifier = _run(
                workflow=workflow,
                bundle_dir=bundle_dir,
                config=config,
                hooks=hooks,
                ctx=ctx,
                setup=setup,
                authorize=authorize,
                fault_driver=setup.driver,
                marks=marks,
                lookup=lookup_kwargs,
            )
            reports.setdefault(case_id, []).append(report)
            judged = _judge_trial(
                hooks=hooks, ctx=ctx, report=report, readback=readback
            )
            oracle = judged["oracle"]
            (trial_dir / "case-input.json").write_bytes(
                runtime_inputs_bytes(workflow, dict(params), None)
            )
            if report.qualification_fault_mutations:
                if len(report.qualification_fault_mutations) != 1:
                    raise QualifyError("a fault case retained several fault receipts")
                receipt = report.qualification_fault_mutations[0]
                (trial_dir / "fault-receipt.json").write_bytes(receipt.artifact_bytes())
                (trial_dir / "fault-mutation.bin").write_bytes(
                    setup.driver.mutation_bytes(receipt.fault_kind)
                )
            trial_s = time.time() - t_trial
            trial_times.append(trial_s)
            row = {
                "case_id": case_id,
                "trial": trial,
                "expected": expected,
                **report_summary(report),
                "oracle": oracle.get("observed"),
                "trial_wall_s": round(trial_s, 1),
                "fault_declined": setup.driver.declined,
                **{
                    f"{name}_passed": bool(res.get("passed"))
                    for name, res in judged["extra"].items()
                },
            }
            case_entries.append(
                case_entry(
                    case_id=case_id,
                    trial=trial,
                    report=report,
                    oracle=oracle,
                    verifier_tier=verifier_tier,
                    report_path=ctx.run_dir / "report.json",
                    verifier_substrate=getattr(probe_verifier, "substrate", None),
                )
            )
            rows.append(row)
            if write_campaign_files:
                write_json(out / "campaign-summary.json", rows)
            log(json.dumps(row, default=str))
            failure = None
            if report.execution_outcome != expected:
                failure = f"observed {report.execution_outcome}, expected {expected}"
                if report.halt is not None:
                    failure += f" ({report.halt.reason})"
                if setup.driver.declined:
                    failure += f"; the fault driver declined: {setup.driver.declined}"
            elif report.model_calls != 0:
                failure = "a qualification run called a model"
            elif report.screenshots_may_leave_box:
                failure = "a qualification run let screenshots leave this computer"
            elif not oracle.get("passed") or oracle.get("silent_incorrect_success"):
                failure = f"read-back {oracle.get('observed')}"
            elif not case_entries[-1]["passed"]:
                entry = case_entries[-1]
                failure = (
                    f"read-back verdict {entry['observed_verdict']} at "
                    f"{entry['verification_tier']}, expected "
                    f"{entry['expected_verdict']} at independent_readback"
                )
            else:
                bad = [n for n, r in judged["extra"].items() if not r.get("passed")]
                if bad:
                    failure = f"extra check(s) failed: {bad}"
            if failure is not None:
                write_json(
                    out / "qualification" / "result.json",
                    build_result(
                        report=evaluate_qualification(workflow, evidence_root=root),
                        verify_sha256=verify_sha,
                        verifier_identity=verifier_id,
                        fault_driver_version=setup.driver.contract_sha256[:16],
                        cases=case_entries,
                    ),
                )
                raise QualifyError(f"{case_id} trial {trial}: {failure}")
            results.append(
                case_result(
                    root=root,
                    workflow=workflow,
                    case_id=case_id,
                    observed_outcome=expected,
                    run_id=ctx.run_id,
                    trial_dir=trial_dir,
                    runner_key=setup.runner_key,
                    runner_id=runner_id,
                    case_params=params,
                    campaign_id=campaign_id,
                )
            )
    if structure_offered:
        # The signed cases could use structure, so run them again on pixels
        # alone. Informational on this surface.
        pixels_only = _pixels_only_pass(
            workflow=workflow,
            bundle_dir=bundle_dir,
            config=config,
            hooks=hooks,
            setup=setup,
            out=out,
            selected=selected,
            n_trials=n_trials,
            params=params,
            save=save,
            project_cases=project_cases,
            policy_name=policy_name,
            campaign_id=campaign_id,
            run_id_prefix=run_id_prefix,
            lookup=lookup_kwargs,
            log=log,
        )
    else:
        # The signed cases already ran with structure disabled: they are the
        # pixels-only pass, and every one of them behaved as required (a
        # failure stops the campaign above). On a pixels-only surface this is
        # the gate.
        pixels_only = summarize_pixels_only(
            [(row["case_id"], row["trial"], None) for row in rows],
            role="gate" if surface == "remote_display" else "informational",
        )
    common = pixels_only.common_failure()
    log(
        f"pixels-only pass ({pixels_only.role}): {pixels_only.rate_text()} "
        "trials behaved as required with structure lookups disabled"
        + (f"; most common failure: {common}" if common else "")
    )
    for result in results:
        try:
            record_case_results(workflow, [result], evidence_root=root)
        except Exception as exc:
            raise QualifyError(
                f"case result refused for {result.case_id} ({result.detail_code}): {exc}"
            ) from exc
    from openadapt_flow.qualification import set_qualified_lookup

    lookup_record = qualified_lookup(
        surface=surface,
        target_kind=config.app.target,
        structure_offered=structure_offered,
        reports=reports,
        pixels_only=pixels_only,
    )
    set_qualified_lookup(workflow, lookup_record)
    write_json(
        out / "qualification" / "lookup.json",
        {
            **lookup_record.model_dump(mode="json"),
            "pixels_only_pass_rate": pixels_only.pass_rate,
        },
    )
    log(
        f"qualified lookups: {', '.join(lookup_record.capabilities_used)} on a "
        f"{surface} surface"
    )
    if certify is not None:
        qreport = certify_project(
            workflow, policy=load_policy(certify), evidence_root=root
        )
    else:
        qreport = evaluate_qualification(workflow, evidence_root=root)
    write_json(out / "qualification" / "report.json", qreport.model_dump(mode="json"))
    qresult = build_result(
        report=qreport,
        verify_sha256=verify_sha,
        verifier_identity=verifier_id,
        fault_driver_version=setup.driver.contract_sha256[:16],
        cases=case_entries,
    )
    result_path = write_json(out / "qualification" / "result.json", qresult)
    if write_campaign_files:
        (out / "qualification" / "verdict.txt").write_text(qreport.render() + "\n")
    log(qreport.render())
    log(
        f"qualification-result/v1 {result_sha256(qresult)[:16]}: passed="
        f"{qresult['passed']} silent_incorrect_success="
        f"{qresult['silent_incorrect_success_count']} over_halt="
        f"{qresult['over_halt_count']} executor_model_calls="
        f"{qresult['executor_model_calls']} verifier_model_calls="
        f"{qresult['verifier_model_calls']}"
    )
    admission_note: Optional[str] = None
    if certify is not None and qreport.passed and qresult["passed"]:
        from openadapt_flow.admitted_composition import live_bundle_content_digest

        save_qualified_workflow(workflow, bundle_dir)
        try:
            envelope = local_admission(
                result=qresult,
                result_bytes=result_path.read_bytes(),
                oracle_id=config.verifier.verifier.kind,
                campaign_id=campaign_id,
                trials_required=n_trials,
                bundle_content_digest=live_bundle_content_digest(
                    Workflow.load(bundle_dir), bundle_dir
                ),
                effect_contract_sha256=sha256_hex(
                    [effect.model_dump(mode="json") for effect in save.effects]
                ),
                identity_contract_sha256=None,
            )
        except ValueError as exc:
            first = str(exc).splitlines()
            admission_note = "no admission envelope: " + (
                first[0] if len(first) == 1 else f"{first[0]} {first[-2].strip()}"
            )
            log(admission_note)
        else:
            write_json(
                out / "qualification" / "admission.local.json",
                envelope.model_dump(mode="json"),
            )
    t_campaign = time.time()
    timing: dict[str, Any] = {
        "campaign_wall_s": round(t_campaign - t0, 1),
        "trials": len(trial_times),
        "trial_wall_s_mean": round(sum(trial_times) / len(trial_times), 1)
        if trial_times
        else None,
        "reset_s_total": round(sum(reset_times), 1),
        "engine_ms_by_case": {
            row["case_id"]: [
                r["total_ms"] for r in rows if r["case_id"] == row["case_id"]
            ]
            for row in rows
        },
    }
    if certify is not None and qreport.passed and not qresult["passed"]:
        raise QualifyError(
            "certification refused: the qualification result did not pass "
            f"(silent incorrect success {qresult['silent_incorrect_success_count']}, "
            "or a case verdict not from an independent read-back)"
        )
    if certify is not None and not qreport.passed:
        if write_campaign_files:
            write_json(out / "timing.json", timing)
        raise QualifyError(f"certification refused:\n{qreport.render()}")
    save_qualified_workflow(workflow, bundle_dir)
    write_json(
        out / "qualification" / "project.json",
        workflow.qualification.model_dump(mode="json")
        if workflow.qualification
        else {},
    )
    result = QualifyResult(
        passed=bool(qreport.passed) and bool(qresult["passed"]),
        certified=certify is not None and bool(qreport.passed) and qresult["passed"],
        policy=certify,
        out=out,
        bundle_dir=bundle_dir,
        report=qreport,
        rows=rows,
        timing=timing,
        notes=notes,
        reports=reports,
        observer=setup.observer,
        environment_digest=web_environment_digest(environment),
        result=qresult,
        admission_note=admission_note,
        lookup=lookup_record,
    )
    if run_params is not None:
        if not result.certified:
            raise QualifyError("--run-params needs a certified bundle (pass --certify)")
        result.certified_run = certified_run(
            Workflow.load(bundle_dir),
            bundle_dir,
            config,
            out=out / "certified-run",
            params={k: str(v) for k, v in run_params.items()},
            hooks=hooks,
            setup=setup,
            policy=certify or config.campaign.policy,
            run_id=f"{campaign_id}-certified-run",
            lookup=lookup_kwargs,
        )
        timing["certified_run"] = result.certified_run["timing"]
    if write_campaign_files:
        write_json(out / "timing.json", timing)
        write_summary_markdown(result)
    return result


def certified_run(
    workflow: Workflow,
    bundle_dir: Path,
    config: VerifyConfig,
    *,
    out: Path,
    params: dict[str, str],
    hooks: QualifyHooks,
    setup: CampaignSetup,
    policy: str,
    run_id: str,
    lookup: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """One ordinary Standard-profile run of the certified bundle."""

    from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate

    out.mkdir(parents=True)
    t0 = time.time()
    if hooks.reset is not None:
        hooks.reset()
    t_reset = time.time()
    save = next(s for s in workflow.steps if s.risk == "irreversible" and s.effects)
    readback = RecordReadback(hooks.make_verifier(params), save, params)  # type: ignore[misc]
    readback.snapshot()
    ctx = TrialContext(
        case_id="certified-run",
        expected="VERIFIED",
        oracle_kind="exact_record",
        trial=1,
        trial_dir=out,
        run_dir=out / "run",
        run_id=run_id,
        params=params,
        certified_run=True,
        state={"policy": policy},
    )
    if hooks.before_trial is not None:
        hooks.before_trial(ctx)

    def authorize(_verifier: Any, gate_kwargs: dict[str, Any]) -> Any:
        gate = evaluate_run_gate(workflow, **gate_kwargs)
        write_json(out / "run-gate.json", gate.model_dump(mode="json"))
        if not gate.passed:
            raise QualifyError(gate.render())
        return build_runtime_authorization(workflow, gate, params=params)

    marks: dict[str, float] = {"start": time.time()}
    report, _ = _run(
        workflow=workflow,
        bundle_dir=bundle_dir,
        config=config,
        hooks=hooks,
        ctx=ctx,
        setup=setup,
        authorize=authorize,
        fault_driver=None,
        marks=marks,
        lookup=lookup,
    )
    judged = _judge_trial(hooks=hooks, ctx=ctx, report=report, readback=readback)
    marks["checked"] = time.time()
    timing = {
        "reset_s": round(t_reset - t0, 1),
        "run_wall_s": round(marks["task_end"] - marks["start"], 3),
        "replay_task_s": round(marks["task_end"] - marks["task_start"], 3),
        "engine_total_ms": report.total_ms,
        "session_open_s": round(marks["browser_ready"] - marks["start"], 3),
    }
    summary = {
        **report_summary(report),
        "oracle": judged["oracle"].get("observed"),
        **{f"{n}_passed": bool(r.get("passed")) for n, r in judged["extra"].items()},
        "timing": timing,
    }
    write_json(out / "summary.json", summary)
    write_json(out / "timing.json", timing)
    return summary


def _lookup_summary_lines(lookup: Optional[QualifiedLookup]) -> list[str]:
    if lookup is None:
        return []
    pixels = lookup.pixels_only
    role = (
        "required on this pixels-only surface"
        if pixels.role == "gate"
        else "for information"
    )
    common = pixels.common_failure()
    return [
        f"- Surface: `{lookup.surface}`; lookups used: "
        + ", ".join(lookup.capabilities_used),
        f"- Pixels-only pass ({role}): {pixels.rate_text()} trials behaved as "
        "required with structure lookups disabled"
        + (f"; most common failure: {common}" if common else ""),
    ]


def write_summary_markdown(result: QualifyResult) -> Path:
    lines = [
        "# Qualification result",
        "",
        f"- Verdict: {'PASS' if result.passed else 'FAIL'}"
        + (f", certified under `{result.policy}`" if result.certified else ""),
        f"- Campaign time: {result.timing.get('campaign_wall_s')} s for "
        f"{result.timing.get('trials')} trials",
        f"- Certified bundle: `{result.bundle_dir.name}/`"
        if result.certified
        else "- Not certified",
        *_lookup_summary_lines(result.lookup),
        "",
        "| Case | Trial | Expected | Observed | Read-back | Model calls | Engine ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in result.rows:
        lines.append(
            f"| {row['case_id']} | {row['trial']} | {row['expected']} | "
            f"{row['execution_outcome']} | {row['oracle']} | {row['model_calls']} | "
            f"{round(row['total_ms'] or 0)} |"
        )
    if result.certified_run is not None:
        run = result.certified_run
        lines += [
            "",
            "## Certified run",
            "",
            f"- Outcome: {run['execution_outcome']}, read-back {run['oracle']}, "
            f"model calls {run['model_calls']}",
            f"- Replay: {run['timing']['replay_task_s']} s",
        ]
    lines += [
        "",
        "The case results are signed with per-campaign local test keys. They "
        "prove this campaign in this environment, nothing more.",
        "",
    ]
    path = result.out / "QUALIFICATION.md"
    path.write_text("\n".join(lines))
    return path


__all__ = [
    "CASES",
    "QualifyError",
    "QualifyHooks",
    "QualifyResult",
    "RecordReadback",
    "TrialContext",
    "apply_verify_config",
    "case_result",
    "certified_run",
    "check_ready",
    "configure_project",
    "lookup_plan",
    "pixels_only_trial_failure",
    "qualified_lookup",
    "qualify",
    "record_effects",
    "report_summary",
    "resolve_inputs",
    "resolve_save_step",
    "summarize_pixels_only",
    "write_json",
]
