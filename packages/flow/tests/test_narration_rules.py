"""Spoken narration proposes a branch guard; recordings, a probe or a person
certify it.

Synthetic data only: invented payers and amounts, a fake scrubber, and (in one
optional test) synthetic speech made with macOS ``say``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from openadapt_flow.compiler.induction import induce_program
from openadapt_flow.compiler.narration_rules import parse_rule
from openadapt_flow.ir import ActionKind, PredicateKind, StateKind, Step, Workflow
from openadapt_flow.narration import (
    Narration,
    NarrationLine,
    NarrationRefused,
    align,
    attach_narration,
    load_narration,
)
from tests.test_replayer import FakeBackend, FakeVision
from openadapt_flow.runtime.replayer import Replayer

RULE = "If the insurance is Medicare, skip the copay screen."


def _type(sid: str, field: str, value: str, *, risk: str = "reversible") -> Step:
    return Step(id=sid, intent=f"type {field}", action=ActionKind.TYPE, text=value, risk=risk)


def _key(sid: str, key: str, intent: str, *, risk: str = "reversible") -> Step:
    return Step(id=sid, intent=intent, action=ActionKind.KEY, key=key, risk=risk)


def visit(payer: str, copay: bool) -> Workflow:
    """Check in a visit. The copay is a consequential write, done for some payers."""
    steps = [_type("step_000", "patient", "Test Patient"), _type("step_001", "insurance", payer)]
    if copay:
        steps.append(_type("step_002", "copay", "20", risk="irreversible"))
    steps.append(_key("step_003", "Enter", "press Enter to finish"))
    return Workflow(name="check-in", steps=steps)


def said(text: str, step_id: str) -> Narration:
    return Narration(lines=[NarrationLine(t_start=1.0, t_end=3.0, text=text, step_id=step_id)])


def _opt(result):
    return next(
        s
        for s in result.program.states.values()
        if s.kind is StateKind.ACTION and s.step and s.step.intent == "type copay"
    )


def _run(workflow, tmp_path, params):
    backend = FakeBackend()
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / f"run-{len(list(tmp_path.iterdir()))}",
        params=params,
    )
    return backend, report


# -- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,value,skip,runs",
    [
        (RULE, "Medicare", True, False),
        ("Skip the copay screen when the insurance is Medicare.", "Medicare", True, False),
        ("Since it's Medicare, I don't fill in the copay.", "Medicare", True, False),
        ("If the insurance is not Medicare, enter the copay.", "Medicare", False, False),
        ("Unless the payer is Aetna, leave the referral blank.", "Aetna", True, True),
        ("If it says Prior Auth Required, click Request Auth.", "Prior Auth Required", False, True),
    ],
)
def test_sentences_parse_into_condition_and_action(sentence, value, skip, runs):
    rule = parse_rule(sentence)
    assert rule is not None
    assert (rule.value, rule.skip, rule.runs_when_condition_holds) == (value, skip, runs)


def test_plain_narration_is_not_a_rule():
    assert parse_rule("Now I click save.") is None
    assert parse_rule("Open the chart for the patient.") is None


# -- required cases -----------------------------------------------------------


def test_a_narrated_branch_becomes_a_proposed_guard():
    """Insurance is on screen but never typed: the sentence maps to a screen
    element. Nothing checked it, so it is a flagged proposal and the point
    stays refused."""
    traces = [
        Workflow(name="c", steps=[_type("step_000", "patient", "A"), _key("step_003", "Enter", "finish")]),
        Workflow(
            name="c",
            steps=[
                _type("step_000", "patient", "A"),
                _type("step_002", "copay", "20", risk="irreversible"),
                _key("step_003", "Enter", "finish"),
            ],
        ),
    ]
    result = induce_program(traces, narration=[said(RULE, "step_003"), None])
    (ng,) = result.narrated_guards
    assert ng.status == "proposed" and ng.certified_by is None
    assert ng.guard.kind is PredicateKind.NOT
    assert ng.guard.operands[0].kind is PredicateKind.TEXT_PRESENT
    assert ng.guard.operands[0].text == "Medicare"
    assert ng.sentence == RULE
    assert not result.certified and result.program is None
    (u,) = result.uncertainties
    assert u.proposal is not None and u.proposal.trusted is False
    assert u.proposal.source == "narration"
    assert any(o.key == "narrated" for o in u.question.options)
    assert "--accept-narrated opt_" in u.record_next


def test_the_guard_certifies_when_every_recording_agrees(tmp_path):
    traces = [visit("Medicare", False), visit("Aetna", True), visit("Cigna", True)]
    result = induce_program(traces, narration=[said(RULE, "step_003"), None, None])
    (ng,) = result.narrated_guards
    assert ng.status == "certified" and ng.certified_by == "recordings_agree"
    assert ng.agree == [0, 1, 2]
    assert result.certified, result.render()
    state = _opt(result)
    guard = state.step.guard
    assert guard.on_unmet == "skip"
    assert guard.predicate.kind is PredicateKind.NOT
    inner = guard.predicate.operands[0]
    assert (inner.kind, inner.value) == (PredicateKind.PARAM_EQUALS, "Medicare")
    source = result.workflow.guard_sources[state.id]
    assert source.sentence == RULE and source.certified_by == "recordings_agree"

    # The compiled program follows the rule on new runs.
    pname = inner.param
    params = {k: v for k, v in result.workflow.params.items()}
    backend, report = _run(result.workflow, tmp_path, {**params, pname: "Medicare"})
    assert report.success and ("type", "20") not in backend.actions
    backend, report = _run(result.workflow, tmp_path, {**params, pname: "Humana"})
    assert report.success and ("type", "20") in backend.actions


def test_a_contradicting_recording_keeps_it_refused():
    traces = [
        visit("Medicare", False),
        visit("Aetna", True),
        visit("Medicare", True),  # did the copay for Medicare after all
    ]
    result = induce_program(
        traces,
        narration=[said(RULE, "step_003"), None, None],
        accepted_narration={"opt_2": "reviewer"},
    )
    (ng,) = result.narrated_guards
    assert ng.status == "contradicted" and ng.contradict == [2]
    assert not result.certified and result.program is None
    (u,) = result.uncertainties
    assert u.kind == "unconfirmed_optional"
    assert "not used" in u.detail
    assert not any(o.key == "narrated" for o in u.question.options)


def test_healthy_runs_make_no_model_calls(tmp_path):
    class CountingInterpreter:
        source = "narration+model"
        calls = 0

        def interpret(self, sentence, context):
            CountingInterpreter.calls += 1
            return None

    traces = [visit("Medicare", False), visit("Aetna", True)]
    result = induce_program(
        traces, narration=[said(RULE, "step_003"), None], interpreter=CountingInterpreter()
    )
    assert result.certified
    # The parser read the sentence, so the compile-time model was not asked.
    assert CountingInterpreter.calls == 0
    pname = result.narrated_guards[0].guard.operands[0].param
    for payer in ("Medicare", "Aetna"):
        _, report = _run(result.workflow, tmp_path, {**result.workflow.params, pname: payer})
        assert report.success
        assert report.model_calls == 0
        assert report.est_model_cost_usd == 0.0


def test_the_transcript_is_scrubbed_before_it_is_stored(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [{"i": 0, "kind": "type", "t": 1.0}, {"i": 1, "kind": "key", "t": 4.0}])
        + "\n"
    )

    class Scrubber:
        def scrub_text(self, text, is_separated=False):
            return text.replace("John Smith", "<PERSON>")

    sentences = [(0.2, 0.9, "Patient John Smith."), (2.0, 3.5, RULE)]
    from openadapt_flow import privacy

    privacy.set_text_scrubber(None)
    try:
        with pytest.raises(NarrationRefused):
            attach_narration(rec, sentences)
    finally:
        privacy.reset_scrubbers()
    assert not (rec / "narration.json").exists()

    attach_narration(rec, sentences, scrubber=Scrubber())
    raw = (rec / "narration.json").read_text()
    assert "John Smith" not in raw
    nar = load_narration(rec)
    assert [(line.text, line.step_id) for line in nar.lines] == [
        ("Patient <PERSON>.", "step_000"),
        (RULE, "step_001"),
    ]
    data = json.loads(raw)
    data["scrubbed"] = False
    (rec / "narration.json").write_text(json.dumps(data))
    with pytest.raises(NarrationRefused):
        load_narration(rec)


# -- the other ways to certify --------------------------------------------------


def test_a_person_can_accept_a_guard_nothing_contradicts():
    traces = [
        Workflow(name="c", steps=[_type("step_000", "patient", "A"), _key("step_003", "Enter", "finish")]),
        Workflow(
            name="c",
            steps=[
                _type("step_000", "patient", "A"),
                _type("step_002", "copay", "20", risk="irreversible"),
                _key("step_003", "Enter", "finish"),
            ],
        ),
    ]
    result = induce_program(
        traces, narration=[said(RULE, "step_003"), None], accepted_narration={"opt_1": "R. Reviewer"}
    )
    (ng,) = result.narrated_guards
    assert (ng.status, ng.certified_by, ng.accepted_by) == ("certified", "person", "R. Reviewer")
    assert result.certified
    assert result.workflow.guard_sources["opt_1"].accepted_by == "R. Reviewer"


def test_a_screen_element_guard_certifies_from_the_recorded_screens():
    traces = [
        Workflow(name="c", steps=[_type("step_000", "patient", "A"), _key("step_003", "Enter", "finish")]),
        Workflow(
            name="c",
            steps=[
                _type("step_000", "patient", "A"),
                _type("step_002", "copay", "20", risk="irreversible"),
                _key("step_003", "Enter", "finish"),
            ],
        ),
    ]
    screens = {0: ["Coverage: Medicare Part B"], 1: ["Coverage: Aetna PPO"]}
    result = induce_program(
        traces,
        narration=[said(RULE, "step_003"), None],
        screen_texts=lambda t, step_id: screens[t],
    )
    (ng,) = result.narrated_guards
    assert ng.status == "certified" and ng.certified_by == "recordings_agree"
    assert result.certified


def test_a_narrated_divergence_becomes_a_branch(tmp_path):
    def trace(payer: str) -> Workflow:
        decide = (
            _key("step_002", "M", "press M to bill medicare", risk="irreversible")
            if payer == "Medicare"
            else _key("step_002", "C", "press C to collect the copay", risk="irreversible")
        )
        return Workflow(
            name="bill",
            steps=[_type("step_001", "insurance", payer), decide, _key("step_003", "Enter", "finish")],
        )

    plain = induce_program([trace("Medicare"), trace("Aetna")])
    assert not plain.certified  # a divergence with no detectable condition

    rule = "If the insurance is Medicare, bill Medicare directly."
    result = induce_program(
        [trace("Medicare"), trace("Aetna"), trace("Cigna")],
        narration=[said(rule, "step_002"), None, None],
    )
    (ng,) = result.narrated_guards
    assert ng.location.startswith("divergence_") and ng.status == "certified"
    assert result.certified, result.render()
    branch = next(s for s in result.program.states.values() if s.kind is StateKind.BRANCH)
    assert branch.id in result.workflow.guard_sources
    pname = ng.guard.param
    backend, report = _run(result.workflow, tmp_path, {**result.workflow.params, pname: "Medicare"})
    assert report.success and ("press", "M") in backend.actions
    backend, report = _run(result.workflow, tmp_path, {**result.workflow.params, pname: "Humana"})
    assert report.success and ("press", "C") in backend.actions


def test_a_sentence_about_another_step_is_not_used():
    traces = [visit("Medicare", False), visit("Aetna", True)]
    result = induce_program(
        traces, narration=[said("If the insurance is Medicare, skip the referral.", "step_003"), None]
    )
    assert result.narrated_guards == []
    assert not result.certified


# -- every backend --------------------------------------------------------------


@pytest.mark.parametrize("backend", ["web", "native", "rdp", "citrix"])
def test_narration_is_the_same_for_every_backend(tmp_path, backend):
    """Narration only uses the events' ``t`` clock, so a web, native, RDP or
    Citrix recording of the same demonstration lines up and proposes the same
    guard."""

    class Scrubber:
        def scrub_text(self, text, is_separated=False):
            return text

    def recording(name: str, wf: Workflow, sentences) -> Path:
        rec = tmp_path / backend / name
        rec.mkdir(parents=True)
        (rec / "meta.json").write_text(json.dumps({"id": name, "backend": backend}))
        events = [
            {"i": int(s.id.split("_")[1]), "kind": s.action.value, "t": 2.0 * k}
            for k, s in enumerate(wf.steps)
        ]
        (rec / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
        if sentences:
            attach_narration(rec, sentences, scrubber=Scrubber())
        return rec

    traces = [visit("Medicare", False), visit("Aetna", True)]
    recs = [
        recording("medicare", traces[0], [(3.2, 4.0, RULE)]),
        recording("aetna", traces[1], []),
    ]
    narration = [load_narration(r) for r in recs]
    assert narration[0].lines[0].step_id == "step_003"
    result = induce_program(traces, narration=narration)
    (ng,) = result.narrated_guards
    assert (ng.status, ng.certified_by, ng.location) == ("certified", "recordings_agree", "opt_2")


def test_alignment_attaches_a_sentence_to_the_next_action():
    events = [{"i": 0, "t": 1.0}, {"i": 1, "t": 5.0}, {"i": 2, "t": 9.0}]
    lines = align([(0.0, 0.5, "a."), (4.9, 6.0, "b."), (12.0, 13.0, "c.")], events)
    assert [line.step_id for line in lines] == ["step_000", "step_001", "step_002"]


def test_capture_narration_is_carried_into_the_recording(tmp_path):
    from types import SimpleNamespace

    from openadapt_flow.adapters.capture import _write_capture_narration

    session = SimpleNamespace(
        narration=lambda: [SimpleNamespace(start=103.0, end=104.0, text=RULE)]
    )
    _write_capture_narration(
        session, tmp_path, 100.0, [{"i": 0, "t": 1.0, "kind": "type"}, {"i": 1, "t": 3.5, "kind": "key"}]
    )
    nar = load_narration(tmp_path)
    assert nar.source == "openadapt-capture"
    assert [(line.t_start, line.step_id) for line in nar.lines] == [(3.0, "step_001")]


# -- real local speech (optional) ---------------------------------------------

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "capture"
    / "tests"
    / "fixtures"
    / "narration"
    / "medicare_copay.wav"
)


@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None or not FIXTURE.is_file(),
    reason="faster-whisper or the synthetic speech fixture is not available",
)
def test_synthetic_speech_end_to_end(tmp_path):
    """Synthetic speech -> local Whisper -> scrub -> align -> induce. Skipped
    unless the tiny.en model is already cached (never downloads)."""
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache("Systran/faster-whisper-tiny.en", "model.bin")
    except Exception:  # noqa: BLE001
        cached = None
    if not isinstance(cached, str):
        pytest.skip("tiny.en Whisper model is not cached locally")
    from openadapt_flow.narration import sentences_from_transcript, transcribe_audio

    class Scrubber:
        def scrub_text(self, text, is_separated=False):
            return text.replace("John Smith", "<PERSON>")

    traces = [visit("Medicare", False), visit("Aetna", True)]
    rec = tmp_path / "rec"
    rec.mkdir()
    events = [{"i": int(s.id.split("_")[1]), "kind": "x", "t": 3.0 * k} for k, s in enumerate(traces[0].steps)]
    (rec / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    result = transcribe_audio(FIXTURE, model=str(Path(cached).parent), backend="faster-whisper")
    attach_narration(rec, sentences_from_transcript(result, offset=1.5), scrubber=Scrubber())
    assert "john smith" not in (rec / "narration.json").read_text().lower()
    induced = induce_program(traces, narration=[load_narration(rec), None])
    (ng,) = induced.narrated_guards
    assert ng.status == "certified" and induced.certified


# -- probing on a test copy -----------------------------------------------------


def test_probing_confirms_a_narrated_guard_the_recordings_cannot_check(tmp_path, monkeypatch):
    """Coverage shows on screen and is never typed, so the recordings cannot
    check "skip the copay when it's Medicare". The scripted test copy can:
    the narrated reading produces the recorded effect in both contexts and
    dropping the copay does not."""
    from types import SimpleNamespace

    from openadapt_flow.compiler import probing
    from openadapt_flow.reward.models import RewardBundle

    coverage = {"p-med": "Medicare", "p-aet": "Aetna"}

    class ScreenVision(FakeVision):
        def __init__(self, patient):
            super().__init__()
            self.patient = patient

        def text_present(self, screen_png, text, **_kw):
            return text.lower() in f"coverage: {coverage[self.patient]}".lower()

    class TestCopy:
        name = "scripted-test-copy"

        def __init__(self):
            self.last: dict[str, bool] = {}

        def reset(self):
            pass

        def run(self, workflow, *, bundle_dir, run_dir, params):
            patient = params["patient_id"]
            backend = FakeBackend()
            report = Replayer(backend, vision=ScreenVision(patient), poll_interval_s=0.01).run(
                workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
            )
            copay = ("type", "20") in backend.actions
            # The recorded effect: a copay exactly when coverage is not Medicare.
            self.last[patient] = report.success and copay == (coverage[patient] != "Medicare")
            return probing.RunSignal(probing.runtime_signal_of(report), report.model_calls)

    target = TestCopy()

    class Scorer:
        def __init__(self, bundle):
            pass

        def baseline(self, identity):
            return None

        def judge(self, identity, signal, before):
            ok = target.last.get(identity["patient_id"], False)
            return SimpleNamespace(
                outcome=SimpleNamespace(value="verified" if ok else "wrong_effect"), reason=""
            )

    bundle = SimpleNamespace(identity_keys=("patient_id",), check_identity=lambda i: None)
    monkeypatch.setattr(probing, "_Scorer", Scorer)
    monkeypatch.setattr(RewardBundle, "load", classmethod(lambda cls, x: x))

    def recording(patient, copay):
        steps = [_type("step_000", "patient", patient)]
        if copay:
            steps.append(_type("step_002", "copay", "20", risk="irreversible"))
        steps.append(_key("step_003", "Enter", "finish"))
        return probing.ProbeTrace(
            workflow=Workflow(name="c", steps=steps, params={"patient_id": patient}),
            bundle_dir=tmp_path / patient,
            label=patient,
        )

    traces = [recording("p-med", False), recording("p-aet", True)]
    plain = induce_program([t.workflow for t in traces], narration=[said(RULE, "step_003"), None])
    assert plain.narrated_guards[0].status == "proposed" and not plain.certified

    result = probing.probe_and_induce(
        traces,
        target=target,
        reward_contract=bundle,
        work_dir=tmp_path / "probe",
        narration=[said(RULE, "step_003"), None],
    )
    (verdict,) = result.probe_verdicts
    assert verdict.certified == "narrated", verdict
    assert verdict.interpretations["narrated"] == "passed"
    assert verdict.interpretations["incidental"] == "failed"
    (ng,) = result.narrated_guards
    assert (ng.status, ng.certified_by) == ("certified", "probe")
    assert result.certified
    assert all(run.model_calls == 0 for run in result.probe_runs)


# -- CLI ------------------------------------------------------------------------


def _screen_only_traces():
    return [
        Workflow(name="c", steps=[_type("step_000", "patient", "A"), _key("step_003", "Enter", "finish")]),
        Workflow(
            name="c",
            steps=[
                _type("step_000", "patient", "A"),
                _type("step_002", "copay", "20", risk="irreversible"),
                _key("step_003", "Enter", "finish"),
            ],
        ),
    ]


def test_cli_induce_accepts_a_narrated_guard_only_with_a_name(tmp_path, capsys):
    from openadapt_flow.__main__ import main
    from openadapt_flow.narration import write_narration

    dirs = []
    for i, wf in enumerate(_screen_only_traces()):
        d = tmp_path / f"b{i}"
        wf.save(d)
        dirs.append(str(d))
    write_narration(Path(dirs[0]), said(RULE, "step_003"))
    out = tmp_path / "out"

    assert main(["induce", *dirs, "--out", str(out)]) == 2
    assert "[narrated]" in capsys.readouterr().out and not out.exists()

    assert main(["induce", *dirs, "--out", str(out), "--accept-narrated", "opt_1"]) == 2
    assert "--accepted-by" in capsys.readouterr().out

    rc = main(
        ["induce", *dirs, "--out", str(out), "--accept-narrated", "opt_1", "--accepted-by", "R. Reviewer"]
    )
    assert rc == 0, capsys.readouterr().out
    written = Workflow.load(out)
    (source,) = written.guard_sources.values()
    assert (source.sentence, source.accepted_by) == (RULE, "R. Reviewer")


def test_cli_narration_attach_scrubs_a_transcript(tmp_path, capsys):
    from openadapt_flow import privacy
    from openadapt_flow.__main__ import main

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text(json.dumps({"i": 0, "kind": "type", "t": 2.0}) + "\n")
    transcript = tmp_path / "t.json"
    transcript.write_text(
        json.dumps({"segments": [{"start": 0.5, "end": 1.5, "text": "Patient John Smith is Medicare."}]})
    )

    class Scrubber:
        def scrub_text(self, text, is_separated=False):
            return text.replace("John Smith", "<PERSON>")

    privacy.set_text_scrubber(None)
    try:
        assert main(["narration", "attach", str(rec), "--transcript", str(transcript)]) == 2
        assert not (rec / "narration.json").exists()
        privacy.set_text_scrubber(Scrubber())
        assert main(["narration", "attach", str(rec), "--transcript", str(transcript)]) == 0
    finally:
        privacy.reset_scrubbers()
    out = capsys.readouterr().out
    assert "John Smith" not in out and "<PERSON>" in out
    assert "John Smith" not in (rec / "narration.json").read_text()
