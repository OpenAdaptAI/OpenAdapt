"""Business-rule qualification cases (#1399).

The six cases inject faults. A business-rule case injects nothing: a seed step
creates the record the application's own rule protects (an existing chart for
the same person), the run uses inputs that trigger the rule, and the bundle
must stop before the save with no new or changed record. Certification runs
it on every trial and is withdrawn by any failed trial.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.qualify import QualifyConfigError, parse_verify_config, qualify
from openadapt_flow.qualify import campaign as campaign_mod
from openadapt_flow.qualify.campaign import QualifyError, rule_params
from openadapt_flow.qualify.result import build_result, case_entry
from openadapt_flow.verification import VerificationTier

FLOW = Path(__file__).resolve().parents[1]
APP = {"name": "Demo", "version": "1", "url": "http://127.0.0.1:9301/"}
RULE = {
    "id": "existing-duplicate",
    "description": "An existing record for the same person.",
    "seed": {"python": "benchmark.openemr_local.qualify_hooks:seed_existing_chart"},
    "params": {"lname": "Delgado", "email": "rosa@example.invalid"},
    "expect": "HALTED_BEFORE_EFFECT",
}


def _config(rules: Any = None, **over: Any) -> Any:
    data = {
        "schema_version": "openadapt.verify/v1",
        "app": {**APP, "reset": {"command": "true"}},
        "verifier": {
            "kind": "rest",
            "base_url": "http://127.0.0.1:9300",
            "records_path": "/api/patient",
        },
        "record": {
            "name": "patient",
            "match": ["email"],
            "fields": {"email": "email", "lname": "lname"},
        },
        "business_rules": [RULE] if rules is None else rules,
    }
    data.update(over)
    return parse_verify_config(data, env={})


# -- verify.yaml ----------------------------------------------------------------
def test_a_business_rule_parses_into_a_rule_case() -> None:
    config = _config()
    (rule,) = config.business_rules
    assert rule.case_id == "rule-existing-duplicate"
    assert rule.seed.python == RULE["seed"]["python"]
    assert rule.expect == "HALTED_BEFORE_EFFECT"
    assert rule.readback == "no_mutation"
    assert config.all_case_ids()[-1] == "rule-existing-duplicate"
    assert len(config.all_case_ids()) == 7
    # A config without rules is the six cases, unchanged.
    assert _config(rules=[]).all_case_ids() == list(campaign_mod.CASE_IDS)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expect": "VERIFIED"}, "expect"),
        ({"readback": "exact_record"}, "readback"),
        ({"params": {}}, "params"),
        ({"seed": {"command": "true", "python": "m:f"}}, "exactly one"),
        ({"seed": {"python": "not a hook"}}, "module:function"),
        ({"id": "Has Spaces"}, "id"),
    ],
)
def test_rule_errors_name_the_problem(change: dict[str, Any], message: str) -> None:
    with pytest.raises(QualifyConfigError, match=message):
        _config(rules=[{**RULE, **change}])


def test_rule_ids_are_unique_and_cases_must_name_a_rule() -> None:
    with pytest.raises(QualifyConfigError, match="unique"):
        _config(rules=[RULE, RULE])
    with pytest.raises(QualifyConfigError, match="rule-typo"):
        _config(campaign={"cases": ["representative", "rule-typo"]})


def test_the_openemr_verify_yaml_declares_the_existing_duplicate_rule() -> None:
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec
    from openadapt_flow.qualify import load_verify_config

    config = load_verify_config(FLOW / "benchmark/openemr_local/verify.yaml", env={})
    (rule,) = config.business_rules
    assert rule.case_id == "rule-existing-duplicate"
    assert rule.expect == "HALTED_BEFORE_EFFECT"
    assert rule.readback == "no_mutation"
    assert rule.seed.python == (
        "benchmark.openemr_local.qualify_hooks:seed_existing_chart"
    )
    # Every input of the recorded referral, so no recorded value carries over.
    assert set(rule.params) == set(SyntheticPatientSpec().params())
    assert (rule.params["fname"], rule.params["lname"]) == ("Rosa", "Delgado")
    assert rule.params["DOB"] == "1958-03-22"
    assert rule.halt_at == "empty_result_guard"
    assert rule.halt_reason == "No matches were found."


def test_verify_run_ignores_business_rules() -> None:
    from openadapt_flow.verify_run import QUALIFY_ONLY_SECTIONS

    assert "business_rules" in QUALIFY_ONLY_SECTIONS


def test_rule_params_replace_the_campaign_inputs() -> None:
    (rule,) = _config().business_rules
    campaign_inputs = {"email": "a@example.test", "lname": "Doe", "fname": "Ann"}
    assert rule_params(rule, campaign_inputs) == {
        "email": "rosa@example.invalid",
        "lname": "Delgado",
        "fname": "Ann",
    }
    with pytest.raises(QualifyConfigError, match="does not take"):
        rule_params(rule, {"email": "a@example.test"})


def test_rule_cases_run_only_when_certifying(tmp_path: Path) -> None:
    with pytest.raises(QualifyConfigError, match="--certify"):
        qualify(
            tmp_path / "bundle",
            _config(),
            out=tmp_path / "out",
            cases=["rule-existing-duplicate"],
        )
    with pytest.raises(QualifyConfigError, match="unknown case"):
        qualify(tmp_path / "bundle", _config(), out=tmp_path / "out", cases=["rule-x"])


# -- one trial's verdict ----------------------------------------------------------
def _report(execution: str, transaction: str, success: bool = False) -> Any:
    return SimpleNamespace(
        execution_outcome=execution,
        transaction_outcome=transaction,
        success=success,
        model_calls=0,
    )


def _entry(tmp_path: Path, report: Any, oracle: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "report.json"
    path.write_text("{}")
    return case_entry(
        case_id="rule-existing-duplicate",
        trial=1,
        report=report,
        oracle=oracle,
        verifier_tier=VerificationTier.INDEPENDENT_SYSTEM,
        report_path=path,
    )


NOTHING = {"independent": True, "effect_verdict": "REFUTED", "landed": "none"}


def test_a_rule_trial_passes_only_halted_before_the_save(tmp_path: Path) -> None:
    entry = _entry(tmp_path, _report("HALTED", "HALTED_BEFORE_EFFECT"), NOTHING)
    assert entry["passed"] is True
    assert entry["fault_class"] == "business_rule"
    assert entry["expected_verdict"] == "REFUTED"
    assert entry["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
    assert entry["silent_incorrect_success"] is False


def test_a_rule_trial_that_halts_after_trying_the_save_fails(tmp_path: Path) -> None:
    entry = _entry(tmp_path, _report("HALTED", "RECONCILIATION_REQUIRED"), NOTHING)
    assert entry["passed"] is False


def test_a_rule_trial_that_saves_the_duplicate_fails(tmp_path: Path) -> None:
    # What happened on 2026-10-20: the run clicked through and saved.
    landed = {"independent": True, "effect_verdict": "CONFIRMED", "landed": "exact"}
    entry = _entry(tmp_path, _report("VERIFIED", "VERIFIED", success=True), landed)
    assert entry["passed"] is False
    assert entry["silent_incorrect_success"] is True
    result = build_result(
        report=SimpleNamespace(model_dump=lambda **_: {"passed": True}),
        verify_sha256="sha256:" + "a" * 64,
        verifier_identity="v",
        fault_driver_version="f",
        cases=[entry],
    )
    assert result["passed"] is False
    assert result["silent_incorrect_success_count"] == 1


# -- certification requires every trial ---------------------------------------------
class _Recorder:
    def __init__(
        self,
        fail_at: tuple[str, int] | None = None,
        raise_at: tuple[tuple[str, int], BaseException] | None = None,
    ) -> None:
        self.calls: list[tuple[str, int, dict[str, str]]] = []
        self.fail_at = fail_at
        self.raise_at = raise_at
        self.bundle_dirs: list[Path] = []

    def __call__(self, **kwargs: Any) -> Any:
        rule, trial = kwargs["rule"], kwargs["trial"]
        self.calls.append((rule.case_id, trial, kwargs["params"]))
        self.bundle_dirs.append(kwargs["bundle_dir"])
        if self.raise_at is not None and self.raise_at[0] == (rule.case_id, trial):
            raise self.raise_at[1]
        failed = self.fail_at == (rule.case_id, trial)
        row = {"case_id": rule.case_id, "trial": trial}
        entry = {"case_id": rule.case_id, "trial": trial, "passed": not failed}
        return row, entry, "observed VERIFIED/VERIFIED" if failed else None


def _run_rules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trial_fn: _Recorder,
    config: Any,
    state: dict[str, Any],
) -> None:
    import openadapt_flow.qualification as qualification

    state.update(saves=[], invalidated=0, rows=[], entries=[])
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "workflow.json").write_text("{}")
    workflow = SimpleNamespace(name="certified")
    monkeypatch.setattr(campaign_mod, "business_rule_trial", trial_fn)
    monkeypatch.setattr(
        qualification,
        "save_qualified_workflow",
        lambda wf, path: state["saves"].append(
            (Path(path).name, state["invalidated"] == 0)
        ),
    )
    monkeypatch.setattr(
        qualification,
        "_invalidate_certification",
        lambda wf: state.__setitem__("invalidated", state["invalidated"] + 1),
    )
    monkeypatch.setattr(
        campaign_mod, "Workflow", SimpleNamespace(load=lambda _dir: workflow)
    )
    campaign_mod._run_business_rules(
        workflow=workflow,  # type: ignore[arg-type]
        bundle_dir=bundle,
        config=config,
        hooks=campaign_mod.QualifyHooks(),
        setup=SimpleNamespace(),  # type: ignore[arg-type]
        out=tmp_path,
        n_trials=3,
        params={"email": "a@example.test", "lname": "Doe"},
        save_id="s3",
        policy="clinical-write",
        run_id_prefix="camp",
        lookup={},
        verifier_tier=VerificationTier.INDEPENDENT_SYSTEM,
        verifier_substrate=None,
        rows=state["rows"],
        case_entries=state["entries"],
        write_campaign_files=False,
        log=lambda _line: None,
        fail_result=lambda: {"passed": True},
    )


def test_every_rule_runs_on_every_trial_on_a_certified_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = {**RULE, "id": "second-rule"}
    trial_fn = _Recorder()
    state: dict[str, Any] = {}
    _run_rules(monkeypatch, tmp_path, trial_fn, _config(rules=[RULE, second]), state)
    assert [(case, trial) for case, trial, _ in trial_fn.calls] == [
        ("rule-existing-duplicate", 1),
        ("rule-existing-duplicate", 2),
        ("rule-existing-duplicate", 3),
        ("rule-second-rule", 1),
        ("rule-second-rule", 2),
        ("rule-second-rule", 3),
    ]
    assert trial_fn.calls[0][2]["lname"] == "Delgado"
    assert len(state["entries"]) == 6 and all(e["passed"] for e in state["entries"])
    # The trials ran a certified copy; the bundle itself was not written (the
    # caller saves the certification once every rule passed), and the copy
    # is gone.
    assert {d.name for d in trial_fn.bundle_dirs} == {campaign_mod.RULE_BUNDLE_DIR}
    assert state["saves"] == [(campaign_mod.RULE_BUNDLE_DIR, True)]
    assert state["invalidated"] == 0
    assert not (tmp_path / campaign_mod.RULE_BUNDLE_DIR).exists()


def _assert_withdrawn(tmp_path: Path, state: dict[str, Any]) -> None:
    # Certified only in the copy; the bundle itself saved after withdrawal.
    assert state["invalidated"] == 1
    assert state["saves"] == [
        (campaign_mod.RULE_BUNDLE_DIR, True),
        ("bundle", False),
    ]
    assert not (tmp_path / campaign_mod.RULE_BUNDLE_DIR).exists()
    result = (tmp_path / "qualification" / "result.json").read_text()
    assert '"passed": false' in result


def test_one_failed_trial_withdraws_certification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trial_fn = _Recorder(fail_at=("rule-existing-duplicate", 2))
    state: dict[str, Any] = {}
    with pytest.raises(QualifyError, match="certification withdrawn"):
        _run_rules(monkeypatch, tmp_path, trial_fn, _config(), state)
    assert len(trial_fn.calls) == 2
    _assert_withdrawn(tmp_path, state)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("business rule 'existing-duplicate' seed HTTP 500"),  # seed
        RuntimeError("app.reset command exited 1"),  # reset
        QualifyError("run gate refused: bundle not certified"),  # run gate
        QualifyError("rule-existing-duplicate trial 1: the seed created no record"),
        OSError("browser crashed"),  # a crash
        KeyboardInterrupt(),  # Ctrl-C
    ],
)
def test_anything_that_raises_withdraws_certification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    trial_fn = _Recorder(raise_at=(("rule-existing-duplicate", 2), error))
    state: dict[str, Any] = {}
    with pytest.raises(type(error)):
        _run_rules(monkeypatch, tmp_path, trial_fn, _config(), state)
    _assert_withdrawn(tmp_path, state)


def test_an_unknown_rule_input_raises_before_anything_is_certified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = {**RULE, "params": {"no_such_input": "x"}}
    trial_fn = _Recorder()
    state: dict[str, Any] = {}
    with pytest.raises(QualifyConfigError, match="does not take"):
        _run_rules(monkeypatch, tmp_path, trial_fn, _config(rules=[bad]), state)
    assert trial_fn.calls == []
    _assert_withdrawn(tmp_path, state)


# -- what qualify checks before any run ---------------------------------------------
def _workflow(*, guard: bool = True) -> Any:
    from openadapt_flow.ir import (
        ActionKind,
        Postcondition,
        PostconditionKind,
        Step,
        Workflow,
    )
    from openadapt_flow.runtime.effects import Effect, EffectKind

    opener = Step(id="s2", intent="click 'Create'", action=ActionKind.CLICK)
    if guard:
        opener.expect = [
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="No matches were found.",
                exact=True,
            )
        ]
    save = Step(
        id="s3",
        intent="click 'Confirm'",
        action=ActionKind.CLICK,
        risk="irreversible",
        effects=[
            Effect(kind=EffectKind.RECORD_WRITTEN, match={"email": "a@example.test"})
        ],
    )
    return Workflow(
        name="register",
        steps=[
            Step(id="s1", intent="email", action=ActionKind.TYPE, param="email"),
            opener,
            save,
        ],
        params={"email": "a@example.test", "lname": "Doe"},
    )


def test_rule_problems_are_found_before_any_run() -> None:
    from openadapt_flow.qualify.campaign import business_rule_problems

    workflow = _workflow()
    save = workflow.steps[-1]
    params = {"email": "a@example.test", "lname": "Doe"}
    assert business_rule_problems(workflow, _config(), save, params) == []
    bad = _config(rules=[{**RULE, "params": {"mrn": "1"}}])
    assert "does not take" in business_rule_problems(workflow, bad, save, params)[0]
    # No mined empty-result check to halt at.
    (problem,) = business_rule_problems(_workflow(guard=False), _config(), save, params)
    assert "halt_at 'empty_result_guard'" in problem
    missing = _config(rules=[{**RULE, "halt_at": "s9"}])
    assert "names no step" in business_rule_problems(workflow, missing, save, params)[0]


def test_halt_steps_follow_halt_at() -> None:
    from openadapt_flow.qualify.campaign import rule_halt_steps

    workflow = _workflow()
    (rule,) = _config().business_rules
    assert rule_halt_steps(workflow, rule, "s3") == {"s2"}
    (before,) = _config(
        rules=[{**RULE, "halt_at": "before_save", "halt_reason": "x"}]
    ).business_rules
    assert rule_halt_steps(workflow, before, "s3") == {"s1", "s2", "s3"}
    with pytest.raises(QualifyConfigError, match="halt_reason"):
        _config(rules=[{**RULE, "halt_at": "before_save"}])


def test_certify_refuses_a_partial_rule_list(tmp_path: Path) -> None:
    second = {**RULE, "id": "second-rule"}
    with pytest.raises(QualifyConfigError, match="every business rule"):
        qualify(
            tmp_path / "bundle",
            _config(rules=[RULE, second]),
            out=tmp_path / "out",
            cases=[*campaign_mod.CASE_IDS, "rule-existing-duplicate"],
            certify="clinical-write",
        )


# -- one rule trial ----------------------------------------------------------------
class _Verifier:
    def __init__(self, store: list[dict[str, Any]]) -> None:
        self.store = store

    def _fetch_records(self) -> list[dict[str, Any]]:
        return [dict(rec) for rec in self.store]


def _trial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    seed_writes: bool = True,
    halted_at: str = "s2",
    reason: str = "Postconditions failed for step 's2': text_present (exact) "
    "'No matches were found.'",
    rule: dict[str, Any] = RULE,
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    import openadapt_flow.qualify.session as session_mod

    store: list[dict[str, Any]] = [{"id": "1", "email": "other@example.test"}]

    def seed() -> None:
        if seed_writes:
            store.append({"id": "2", "email": "r.delgado.prior@example.invalid"})

    monkeypatch.setattr(session_mod, "step_runner", lambda _spec, _where: seed)
    results = [
        SimpleNamespace(step_id="s1", ok=True, error=None),
        SimpleNamespace(step_id=halted_at, ok=False, error=reason),
    ]
    report = SimpleNamespace(
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        success=False,
        model_calls=0,
        screenshots_may_leave_box=False,
        results=results,
        halt=SimpleNamespace(reason=reason),
        execution_profile="standard",
        production_eligible=False,
        total_ms=1.0,
    )

    def fake_run(**kwargs: Any) -> Any:
        run_dir = kwargs["ctx"].run_dir
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text("{}")
        return report, None

    monkeypatch.setattr(campaign_mod, "_run", fake_run)
    monkeypatch.setattr(
        campaign_mod,
        "_judge_trial",
        lambda **_kw: {
            "oracle": {
                "independent": True,
                "passed": True,
                "observed": "no_mutation",
                "effect_verdict": "REFUTED",
                "landed": "none",
                "silent_incorrect_success": False,
            },
            "extra": {},
        },
    )
    hooks = campaign_mod.QualifyHooks(
        reset=lambda: None, make_verifier=lambda _params: _Verifier(store)
    )
    (parsed,) = _config(rules=[rule]).business_rules
    row, entry, failure = campaign_mod.business_rule_trial(
        workflow=_workflow(),
        bundle_dir=tmp_path / "bundle",
        config=_config(rules=[rule]),
        hooks=hooks,
        setup=SimpleNamespace(),  # type: ignore[arg-type]
        rule=parsed,
        params={"email": "rosa@example.invalid", "lname": "Delgado"},
        trial=1,
        trial_dir=tmp_path / "trial",
        run_id="run-1",
        policy="clinical-write",
        verifier_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    return row, entry, failure


def test_a_rule_trial_that_halts_at_the_guard_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row, entry, failure = _trial(monkeypatch, tmp_path)
    assert failure is None and entry["passed"] is True
    assert row["halted_at"] == "s2"


def test_a_halt_anywhere_else_fails_the_trial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unrelated early halt (a typed field's check on the rule's inputs).
    _row, entry, failure = _trial(monkeypatch, tmp_path, halted_at="s1")
    assert failure is not None and "halted at s1" in failure
    assert entry["passed"] is False


def test_the_halt_must_name_the_rules_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rule = {**RULE, "halt_reason": "No matches were found."}
    _row, _entry, failure = _trial(monkeypatch, tmp_path, rule=rule)
    assert failure is None
    _row, entry, failure = _trial(
        monkeypatch, tmp_path / "b", rule=rule, reason="Identity check failed"
    )
    assert failure is not None and "does not name" in failure
    assert entry["passed"] is False


def test_a_seed_that_creates_nothing_fails_the_trial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(QualifyError, match="seed created no record"):
        _trial(monkeypatch, tmp_path, seed_writes=False)
