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
    def __init__(self, fail_at: tuple[str, int] | None = None) -> None:
        self.calls: list[tuple[str, int, dict[str, str]]] = []
        self.fail_at = fail_at

    def __call__(self, **kwargs: Any) -> Any:
        rule, trial = kwargs["rule"], kwargs["trial"]
        self.calls.append((rule.case_id, trial, kwargs["params"]))
        failed = self.fail_at == (rule.case_id, trial)
        row = {"case_id": rule.case_id, "trial": trial}
        entry = {"case_id": rule.case_id, "trial": trial, "passed": not failed}
        return row, entry, "observed VERIFIED/VERIFIED" if failed else None


def _run_rules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trial_fn: _Recorder,
    config: Any,
) -> dict[str, Any]:
    import openadapt_flow.qualification as qualification

    state: dict[str, Any] = {"saved": 0, "invalidated": 0}
    workflow = SimpleNamespace(name="certified")
    monkeypatch.setattr(campaign_mod, "business_rule_trial", trial_fn)
    monkeypatch.setattr(
        qualification,
        "save_qualified_workflow",
        lambda wf, _dir: state.__setitem__("saved", state["saved"] + 1),
    )
    monkeypatch.setattr(
        qualification,
        "_invalidate_certification",
        lambda wf: state.__setitem__("invalidated", state["invalidated"] + 1),
    )
    monkeypatch.setattr(
        campaign_mod, "Workflow", SimpleNamespace(load=lambda _dir: workflow)
    )
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    state["rows"], state["entries"] = rows, entries
    campaign_mod._run_business_rules(
        workflow=workflow,  # type: ignore[arg-type]
        bundle_dir=tmp_path / "bundle",
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
        rows=rows,
        case_entries=entries,
        write_campaign_files=False,
        log=lambda _line: None,
        fail_result=lambda: {"passed": False},
    )
    return state


def test_every_rule_runs_on_every_trial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = {**RULE, "id": "second-rule"}
    trial_fn = _Recorder()
    state = _run_rules(monkeypatch, tmp_path, trial_fn, _config(rules=[RULE, second]))
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
    # The certified bundle is saved once, before the runs, and kept.
    assert state["saved"] == 1 and state["invalidated"] == 0


def test_one_failed_trial_withdraws_certification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trial_fn = _Recorder(fail_at=("rule-existing-duplicate", 2))
    with pytest.raises(QualifyError, match="certification withdrawn"):
        _run_rules(monkeypatch, tmp_path, trial_fn, _config())
    assert len(trial_fn.calls) == 2
    result = (tmp_path / "qualification" / "result.json").read_text()
    assert '"passed": false' in result


def test_withdrawal_saves_an_uncertified_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import openadapt_flow.qualification as qualification

    events: list[str] = []
    monkeypatch.setattr(
        campaign_mod,
        "business_rule_trial",
        lambda **_kw: ({}, {"passed": False}, "read-back unexpected_mutation"),
    )
    monkeypatch.setattr(
        qualification,
        "save_qualified_workflow",
        lambda wf, _dir: events.append("save"),
    )
    monkeypatch.setattr(
        qualification, "_invalidate_certification", lambda wf: events.append("void")
    )
    monkeypatch.setattr(
        campaign_mod, "Workflow", SimpleNamespace(load=lambda _dir: object())
    )
    with pytest.raises(QualifyError, match="rule-existing-duplicate trial 1"):
        campaign_mod._run_business_rules(
            workflow=object(),  # type: ignore[arg-type]
            bundle_dir=tmp_path / "bundle",
            config=_config(),
            hooks=campaign_mod.QualifyHooks(),
            setup=SimpleNamespace(),  # type: ignore[arg-type]
            out=tmp_path,
            n_trials=3,
            params={"email": "a@example.test", "lname": "Doe"},
            save_id="s3",
            policy="clinical-write",
            run_id_prefix="camp",
            lookup={},
            verifier_tier=None,
            verifier_substrate=None,
            rows=[],
            case_entries=[],
            write_campaign_files=False,
            log=lambda _line: None,
            fail_result=lambda: {"passed": False},
        )
    # Certified copy saved for the runs, then voided and saved again.
    assert events == ["save", "void", "save"]
