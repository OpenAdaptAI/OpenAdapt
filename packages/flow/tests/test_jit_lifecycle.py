"""openadapt_flow.jit: DRAFT to ADMITTED, BYOK, the recording warning, the
generic dialect, and the jit CLI. No browser and no model.

The draft here is a synthetic bundle with one write step. The canary's run is
a stub report; tests/e2e/test_jit_cua_e2e.py runs the real thing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.jit import (
    RECORDING_WARNING,
    AdmissionError,
    BundleState,
    CustomerModelKey,
    MissingModelKey,
    TaskSignature,
    UnsupportedAction,
    WebApp,
    compile_agent,
    dialects,
    lifecycle,
    load_customer_model_key,
    register_dialect,
    translate,
)
from openadapt_flow.jit import cli as jit_cli
from openadapt_flow.jit.adapters import Op
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ReadbackSpec,
)
from openadapt_flow.verification import VerificationTier

TASK = TaskSignature("t.add_note", "Add a note", {})
REVIEWER = "Dana Reviewer"


class IndependentVerifier:
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM


class SeparateSessionVerifier:
    verification_tier = VerificationTier.INDEPENDENT_SESSION


class ScreenVerifier:
    verification_tier = VerificationTier.IMMEDIATE_SCREEN


class NeverCalled:
    dialect = "anthropic"
    requires_model_key = False

    def run(self, request: Any, computer: Any) -> None:
        raise AssertionError("the agent must not be called")


class ModelDriven:
    dialect = "anthropic"

    def run(self, request: Any, computer: Any) -> None:
        raise AssertionError("the agent must not be called")


def _report(outcome: str = "VERIFIED", model_calls: int = 0) -> Any:
    return SimpleNamespace(
        execution_outcome=outcome, model_calls=model_calls, halt=None
    )


@pytest.fixture(autouse=True)
def _policy_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthetic bundle carries no risk certification; the policy itself
    is exercised end to end in tests/e2e/test_jit_cua_e2e.py."""

    import openadapt_flow.policy as policy

    monkeypatch.setattr(policy, "load_policy", lambda name: name)
    monkeypatch.setattr(
        policy,
        "evaluate_policy",
        lambda *a, **k: SimpleNamespace(passed=True, render=lambda: ""),
    )


def _effect(**kw: Any) -> Effect:
    return Effect(kind=EffectKind.RECORD_WRITTEN, match={"type": "note"}, **kw)


def _jit(
    tmp_path: Path,
    verifier: Any = None,
    *,
    effect: Effect | None = None,
    agent: Any = None,
    task: TaskSignature = TASK,
    require_distinct_reviewer_and_admitter: bool = False,
) -> Any:
    jit = compile_agent(
        agent or NeverCalled(),
        task=task,
        app=WebApp(url="http://127.0.0.1:9/"),
        verifier=verifier if verifier is not None else IndependentVerifier(),
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
        require_distinct_reviewer_and_admitter=require_distinct_reviewer_and_admitter,
    )
    program = jit.program_dir
    steps = [
        Step(id="step_000", intent="type the note", action=ActionKind.TYPE, text="x"),
        Step(
            id="step_001",
            intent="click Save",
            action=ActionKind.CLICK,
            effects=[effect or _effect()],
        ),
    ]
    Workflow(name="t", steps=steps).save(program / "bundle")
    lifecycle.mark_draft(program / "bundle")
    (program / "candidate.json").write_text(
        json.dumps(
            {
                "state": "DRAFT",
                "status": "needs_human_admit",
                "task": task.to_dict(),
                "require_distinct_reviewer_and_admitter": (
                    require_distinct_reviewer_and_admitter
                ),
                "workflow_id": "wf",
                "recorded_by": "agent:anthropic",
                "agent_principal_id": "cua-bot-7",
                "dialect": "anthropic",
                "policy": "clinical-write",
                "mine_effects": True,
                "compiled_recording": str(program / "recording"),
            }
        )
    )
    return jit


def _review(jit: Any, who: str = REVIEWER) -> dict:
    return jit.review(
        principal_id=who, reviewed_steps=["step_000", "step_001"], confirm="ok"
    )


def _stub_runs(jit: Any, monkeypatch: pytest.MonkeyPatch, report: Any) -> list:
    calls: list = []

    def fake(workflow, digest, values, run_dir, *, attended_by=None):
        calls.append((attended_by, digest))
        return None, report

    monkeypatch.setattr(jit, "_run_governed", fake)
    return calls


@pytest.fixture
def at_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests of mode (a): a person admitting at an interactive terminal."""

    monkeypatch.setattr(lifecycle, "_has_terminal", lambda: True)


CI_PRINCIPAL = "ci:test"


def _ci(jit: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A registered admitter key (mode b), for tests that admit headlessly."""

    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "test passphrase")
    pem = jit.key_dir / "ci.pem"
    if not pem.is_file():
        pub = authority.generate_encrypted_key(pem)
        authority.register_principal(
            jit.key_dir,
            role="admitter_key",
            principal_id=CI_PRINCIPAL,
            public_key_hex=pub,
        )
    return authority.principal_from_key(authority.load_encrypted_key(pem), jit.key_dir)


def _admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    jit = _jit(tmp_path, **kw)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    decision = jit.admit_with_key(_ci(jit, monkeypatch))
    assert decision["outcome"] == "admitted"
    return jit


def _rerun(jit: Any, **changes: Any) -> Any:
    """The same cache, opened by another caller with ``changes``."""

    import dataclasses

    return dataclasses.replace(jit, **changes)


# -- the state machine ---------------------------------------------------------


def test_transitions_go_one_way_from_draft(tmp_path: Path, monkeypatch) -> None:
    assert lifecycle.TRANSITIONS[BundleState.DRAFT] == {
        BundleState.ADMITTED,
        BundleState.REJECTED,
    }
    jit = _admitted(tmp_path, monkeypatch)
    with pytest.raises(AdmissionError):
        jit.reject(principal_id=REVIEWER)
    with pytest.raises(AdmissionError):
        jit.admit(principal_id=REVIEWER, confirm="ok")


def test_a_draft_never_runs_unattended(tmp_path: Path, monkeypatch) -> None:
    jit = _jit(tmp_path)
    assert jit.state() is BundleState.DRAFT
    calls = _stub_runs(jit, monkeypatch, _report())
    result = jit.run()
    assert result.status == "needs_human_admit" and not result.agent_called
    assert calls == []
    # Below run(): the governed runner refuses a draft nobody attends.
    monkeypatch.undo()
    jit2 = _jit(tmp_path / "again")
    detail, report = jit2._run_governed(
        Workflow.load(jit2.bundle_dir), "d", {}, tmp_path / "run"
    )
    assert report is None and "never runs unattended" in detail


def test_replay_and_run_refuse_a_draft_bundle(tmp_path: Path, monkeypatch) -> None:
    from openadapt_flow.__main__ import main

    jit = _jit(tmp_path)
    assert (jit.bundle_dir / lifecycle.DRAFT_MARKER).is_file()
    with pytest.raises(SystemExit, match="is a DRAFT"):
        main(["replay", str(jit.bundle_dir)])
    # A copy of the bundle is still a draft.
    import shutil

    copy = tmp_path / "copied"
    shutil.copytree(jit.bundle_dir, copy)
    with pytest.raises(SystemExit, match="is a DRAFT"):
        lifecycle.refuse_unadmitted_draft(copy)
    # Admitted, it passes this check (the run gate still applies).
    admitted = _admitted(tmp_path / "ok", monkeypatch)
    lifecycle.refuse_unadmitted_draft(admitted.bundle_dir, key_dir=admitted.key_dir)


@pytest.mark.usefixtures("at_terminal")
def test_full_path_review_canary_admit_then_run(tmp_path: Path, monkeypatch) -> None:
    jit = _jit(tmp_path)
    with pytest.raises(AdmissionError, match="no human review"):
        jit.admit(principal_id=REVIEWER, confirm="ok")
    with pytest.raises(AdmissionError, match="review the draft"):
        jit.canary(principal_id=REVIEWER)
    _review(jit)
    with pytest.raises(AdmissionError, match="no passing canary"):
        jit.admit(principal_id=REVIEWER, confirm="ok")
    calls = _stub_runs(jit, monkeypatch, _report())
    canary = jit.canary(principal_id="Sam Operator")
    assert canary.status == "VERIFIED" and canary.cache == "canary"
    assert calls[0][0] == "Sam Operator"  # attended by a named person
    assert all(r["met"] for r in jit.status()["requirements"].values())

    manifest = jit.admit(principal_id="Pat Admin", confirm="ok")
    assert manifest["admitted_by_principal_id"] == "Pat Admin"
    assert manifest["reviewed_by_principal_id"] == REVIEWER
    assert manifest["canary"]["principal_id"] == "Sam Operator"
    assert manifest["canary"]["target_origin"] == "http://127.0.0.1:9"
    assert manifest["admitted_at"].endswith("Z")
    assert manifest["readback"][0]["effects"][0]["evidence"] == "independent_readback"
    assert manifest["signature"].startswith("ed25519:")
    assert jit.state() is BundleState.ADMITTED

    hit = jit.run()
    assert hit.status == "VERIFIED" and hit.cache == "hit"
    assert not hit.agent_called and hit.model_calls == 0
    assert calls[-1] == (None, manifest["digest"])  # unattended; admitted digest


def test_a_changed_manifest_admits_nothing(tmp_path: Path, monkeypatch) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    path = jit.bundle_dir / lifecycle.ADMISSION_FILE
    good = json.loads(path.read_text())
    path.write_text(json.dumps({**good, "admitted_by_principal_id": "Mallory"}))
    assert jit.run().status == "refused"
    with pytest.raises(AdmissionError, match="signature"):
        jit.state()
    path.write_text(json.dumps(good))
    with pytest.raises(AdmissionError, match="no trusted admission key"):
        lifecycle.verify_admission(jit.bundle_dir, key_dir=tmp_path / "elsewhere")
    # Signed by a different key that the trusted directory does not hold.
    other = _admitted(tmp_path / "other", monkeypatch)
    assert other.key_dir != jit.key_dir
    with pytest.raises(AdmissionError, match="signature"):
        lifecycle.verify_admission(jit.bundle_dir, key_dir=other.key_dir)


def test_an_admission_does_not_carry_over_to_another_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    admitted = _admitted(tmp_path / "a", monkeypatch)
    other = _jit(tmp_path / "b")
    wf = Workflow.load(other.bundle_dir)
    wf.steps[0].text = "a different note"
    wf.save(other.bundle_dir)
    (other.bundle_dir / lifecycle.ADMISSION_FILE).write_bytes(
        (admitted.bundle_dir / lifecycle.ADMISSION_FILE).read_bytes()
    )
    other.key_dir = admitted.key_dir  # same trusted key: the signature verifies
    with pytest.raises(AdmissionError, match="not the one that was admitted"):
        lifecycle.verify_admission(other.bundle_dir, key_dir=other.key_dir)
    assert other.run().status == "refused"


def test_a_bundle_edited_after_admission_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    wf_file = jit.bundle_dir / "workflow.json"
    wf_file.write_text(wf_file.read_text().replace("type the note", "type evil"))
    result = jit.run()
    assert result.status == "refused" and "admission does not hold" in result.detail


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"policy": "permissive"}, "admitted under policy"),
        ({"app": WebApp(url="https://prod.example/")}, "the canary ran on"),
        ({"verifier": ScreenVerifier()}, "no independent read-back"),
    ],
)
def test_an_admitted_program_runs_only_as_admitted(
    tmp_path: Path, monkeypatch, change: dict, reason: str
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    other = _rerun(jit, **change)
    calls = _stub_runs(other, monkeypatch, _report())
    result = other.run()
    assert result.status == "refused" and reason in (result.detail or "")
    assert calls == []


@pytest.mark.usefixtures("at_terminal")
def test_review_and_canary_pin_the_digest(tmp_path: Path, monkeypatch) -> None:
    jit = _jit(tmp_path)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    wf = Workflow.load(jit.bundle_dir)
    wf.steps[0].intent = "type something else"
    wf.save(jit.bundle_dir)
    with pytest.raises(AdmissionError, match="no human review"):
        jit.admit(principal_id=REVIEWER, confirm="ok")


def test_review_must_cover_every_step(tmp_path: Path) -> None:
    jit = _jit(tmp_path)
    with pytest.raises(AdmissionError, match="every step"):
        jit.review(principal_id=REVIEWER, reviewed_steps=["step_001"], confirm="ok")
    with pytest.raises(AdmissionError, match="confirm"):
        jit.review(
            principal_id=REVIEWER,
            reviewed_steps=["step_000", "step_001"],
            confirm="maybe",
        )


def test_parameters_are_confirmed_before_the_review(tmp_path: Path) -> None:
    with_params = _jit(
        tmp_path / "p", task=TaskSignature("t.p", "Add {note}", {"note": str})
    )
    with pytest.raises(AdmissionError, match="confirm the parameters"):
        _review(with_params)
    with pytest.raises(AdmissionError, match="task's parameters"):
        with_params.confirm_params(
            principal_id=REVIEWER, accept_params=[], confirm="ok"
        )
    # No parameters: confirm_params is optional, and can't follow a review.
    jit = _jit(tmp_path / "n")
    _review(jit)
    with pytest.raises(AdmissionError, match="already reviewed"):
        jit.confirm_params(principal_id=REVIEWER, accept_params=[], confirm="ok")


@pytest.mark.parametrize(
    "who",
    [
        "",
        "  ",
        "agent",
        "agent:anthropic",
        "Model:claude",
        "llm:x",
        "bot:ci",
        "service:runner",
        "cua-bot-7",
        "anthropic",
        None,
    ],
)
def test_no_agent_or_model_can_act_on_a_draft(tmp_path: Path, who: Any) -> None:
    jit = _jit(tmp_path)
    for act in (
        lambda: jit.review(
            principal_id=who, reviewed_steps=["step_000", "step_001"], confirm="ok"
        ),
        lambda: jit.confirm_params(principal_id=who, accept_params=[], confirm="ok"),
        lambda: jit.canary(principal_id=who),
        lambda: jit.admit(principal_id=who, confirm="ok"),
        lambda: jit.reject(principal_id=who),
    ):
        with pytest.raises(AdmissionError, match="principal|recording"):
            act()
    assert jit.state() is BundleState.DRAFT


@pytest.mark.usefixtures("at_terminal")
def test_distinct_reviewer_and_admitter_is_optional(
    tmp_path: Path, monkeypatch
) -> None:
    assert _admitted(tmp_path / "off", monkeypatch).state() is BundleState.ADMITTED
    jit = _jit(tmp_path / "on", require_distinct_reviewer_and_admitter=True)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    with pytest.raises(AdmissionError, match="different principal"):
        jit.admit(principal_id=f" {REVIEWER.upper()} ", confirm="ok")
    manifest = jit.admit(principal_id="Pat Admin", confirm="ok")
    assert manifest["require_distinct_reviewer_and_admitter"] is True


@pytest.mark.usefixtures("at_terminal")
def test_a_failing_policy_refuses_admission(tmp_path: Path, monkeypatch) -> None:
    import openadapt_flow.policy as policy

    jit = _jit(tmp_path)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    monkeypatch.setattr(
        policy,
        "evaluate_policy",
        lambda *a, **k: SimpleNamespace(passed=False, render=lambda: "risk: FAIL"),
    )
    with pytest.raises(AdmissionError, match="not certified"):
        jit.admit(principal_id=REVIEWER, confirm="ok")


# -- independent read-back ------------------------------------------------------


@pytest.mark.parametrize(
    ("verifier", "effect", "evidence"),
    [
        (IndependentVerifier(), None, "independent_readback"),
        (SeparateSessionVerifier(), None, "screen_readback"),
        (ScreenVerifier(), None, "screen_readback"),
        (IndependentVerifier(), _effect(readback=ReadbackSpec()), "screen_readback"),
        (
            IndependentVerifier(),
            _effect(needs_operator_confirmation=True),
            "placeholder",
        ),
        (object(), None, "unverified"),
    ],
)
@pytest.mark.usefixtures("at_terminal")
def test_only_a_system_of_record_read_lets_a_canary_run(
    tmp_path: Path, monkeypatch, verifier: Any, effect: Any, evidence: str
) -> None:
    jit = _jit(tmp_path, verifier, effect=effect)
    rows = lifecycle.readback_coverage(Workflow.load(jit.bundle_dir), verifier)
    assert [r["step_id"] for r in rows] == ["step_001"]
    assert rows[0]["effects"][0]["evidence"] == evidence
    _review(jit)
    calls = _stub_runs(jit, monkeypatch, _report())
    result = jit.canary(principal_id=REVIEWER)
    if evidence == "independent_readback":
        assert result.status == "VERIFIED" and len(calls) == 1
        return
    assert calls == []  # nothing ran against the app
    assert result.status == "refused" and evidence in (result.detail or "")
    assert result.inbox_item is not None
    with pytest.raises(AdmissionError, match="no passing canary"):
        jit.admit(principal_id=REVIEWER, confirm="ok")


@pytest.mark.parametrize("report", [_report("HALTED"), _report(model_calls=1)])
@pytest.mark.usefixtures("at_terminal")
def test_a_canary_that_halts_or_calls_a_model_fails(
    tmp_path: Path, monkeypatch, report: Any
) -> None:
    jit = _jit(tmp_path)
    _review(jit)
    _stub_runs(jit, monkeypatch, report)
    result = jit.canary(principal_id=REVIEWER)
    assert result.inbox_item is not None and not result.agent_called
    with pytest.raises(AdmissionError, match="no passing canary"):
        jit.admit(principal_id=REVIEWER, confirm="ok")


def test_reject_is_terminal(tmp_path: Path) -> None:
    jit = _jit(tmp_path)
    kept = jit.reject(principal_id=REVIEWER, reason="wrong patient")
    assert lifecycle.state_of(kept) is BundleState.REJECTED
    assert jit.candidate() is None


# -- BYOK -----------------------------------------------------------------------


def test_customer_key_comes_from_env_or_named_config(tmp_path: Path) -> None:
    key = load_customer_model_key("anthropic", env={"ANTHROPIC_API_KEY": "sk-cust"})
    assert key.secret() == "sk-cust" and key.source == "env:ANTHROPIC_API_KEY"
    assert "sk-cust" not in repr(key) and "sk-cust" not in json.dumps(key.provenance())
    cfg = tmp_path / "keys.json"
    cfg.write_text(json.dumps({"openai": {"api_key": "sk-cfg"}}))
    key = load_customer_model_key("openai", env={}, config_path=cfg)
    assert key.secret() == "sk-cfg" and key.provenance()["held_by"] == "customer"


def test_no_fallback_when_the_customer_key_is_missing(tmp_path: Path) -> None:
    env = {"OPENADAPT_EXECUTE_API_KEY": "oa-held", "OPENADAPT_BUNDLE_KEY": "oa"}
    with pytest.raises(MissingModelKey, match="OpenAdapt does not supply one"):
        load_customer_model_key("anthropic", env=env)
    with pytest.raises(MissingModelKey):
        load_customer_model_key(
            "anthropic", env=env, config_path=tmp_path / "none.json"
        )


def test_a_model_driven_agent_is_not_recorded_without_a_customer_key(
    tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow.backends import playwright_backend

    def no_browser(*a: Any, **k: Any) -> Any:
        raise AssertionError("the browser must not open")

    monkeypatch.setattr(playwright_backend.PlaywrightBackend, "launch", no_browser)
    warned: list[str] = []
    jit = compile_agent(
        ModelDriven(),
        task=TASK,
        app=WebApp(url="http://127.0.0.1:9/"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
        on_warning=warned.append,
    )
    with pytest.raises(MissingModelKey):
        jit.run()
    assert warned == []  # refused before anything was shown or recorded


# -- the pre-recording warning ---------------------------------------------------


def test_the_warning_is_printed_before_the_browser_opens(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from openadapt_flow.backends import playwright_backend

    events: list[str] = []

    class Stop(Exception):
        pass

    def launch(*a: Any, **k: Any) -> Any:
        events.append("browser")
        raise Stop

    monkeypatch.setattr(playwright_backend.PlaywrightBackend, "launch", launch)
    jit = compile_agent(
        task=TASK,
        app=WebApp(url="http://127.0.0.1:9/"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
        model_key=CustomerModelKey("anthropic", "env:ANTHROPIC_API_KEY", "sk"),
        # A callback that drops the text does not silence the print.
        on_warning=lambda text: events.append("callback"),
    )
    with pytest.raises(Stop):
        with jit.recording():
            pass
    assert events == ["callback", "browser"]
    assert RECORDING_WARNING in capsys.readouterr().err
    assert "sandbox" in RECORDING_WARNING and "sensitive data" in RECORDING_WARNING


# -- the generic dialect -----------------------------------------------------------


def test_generic_actions_map_to_steps() -> None:
    g = lambda **kw: translate("generic", kw)  # noqa: E731
    assert g(kind="click", x=1, y=2) == [Op("click", x=1, y=2)]
    assert g(kind="click", target="#save") == [Op("click", target="#save")]
    assert g(kind="type", text="hi") == [Op("type", text="hi")]
    assert g(kind="press", key="Return") == [Op("press", key="Enter")]
    assert g(kind="hotkey", key="s", modifiers=["ctrl"]) == [
        Op("hotkey", key="s", modifiers=("Control",))
    ]
    assert g(kind="scroll", dy=120) == [Op("scroll", dy=120)]
    assert g(kind="observe") == [Op("observe")]


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "triple_click", "x": 1, "y": 1},
        {"kind": "click", "x": 1},
        {"kind": "click", "x": 1, "y": 1, "button": "middle"},
        {"kind": "click", "x": 1, "y": 1, "target": "#a"},
        {"kind": "type"},
        {"kind": "hotkey", "key": "s", "modifiers": []},
    ],
)
def test_generic_refuses_what_it_cannot_map_exactly(action: dict) -> None:
    with pytest.raises(UnsupportedAction):
        translate("generic", action)


def test_a_dialect_can_be_registered_but_not_replaced() -> None:
    register_dialect("test_upper", lambda a: [Op("type", text=str(a["t"]).upper())])
    assert "test_upper" in dialects()
    assert translate("test_upper", {"t": "ab"}) == [Op("type", text="AB")]
    with pytest.raises(ValueError, match="built in"):
        register_dialect("anthropic", lambda a: [])


# -- the CLI ----------------------------------------------------------------------


def _cli(*argv: str, answer: str = "ok", tty: bool = True) -> int:
    parser = argparse.ArgumentParser()
    jit_cli.add_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["jit", *argv])
    return jit_cli.cmd_jit(args, ask=lambda prompt: answer, interactive=lambda: tty)


@pytest.mark.usefixtures("at_terminal")
def test_cli_review_and_admit_need_a_terminal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    jit = _jit(tmp_path)
    keys = str(tmp_path / "keys")
    task = str(jit.task_dir)
    assert _cli("status", task, "--key-dir", keys, tty=False) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "DRAFT"
    review = ("review", task, "--principal", REVIEWER, "--all-steps-shown")
    assert _cli(*review, tty=False) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert _cli(*review, answer="no") == 2
    assert _cli(*review) == 0
    admit = ("admit", task, "--principal", REVIEWER, "--key-dir", keys)
    assert _cli(*admit) == 2
    assert "no passing canary" in capsys.readouterr().err
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    assert _cli(*admit, tty=False) == 2
    assert _cli("admit", task, "--principal", "agent:x", "--key-dir", keys) == 2
    assert _cli(*admit, "--require-distinct") == 2
    assert _cli(*admit) == 0
    assert "ADMITTED by Dana Reviewer" in capsys.readouterr().out
