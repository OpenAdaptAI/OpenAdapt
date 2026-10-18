"""Read-back verifiers taught by demonstration, for apps with no API.

The person records the save, then shows how an auditor proves it persisted:
throw away client state (reload, new search, reopened window), open the
record, read the fields. These tests cover what that check must refuse and
how its verdict is graded. MockMed's encounter save on fake backends: no
browser, no model calls.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from openadapt_flow.assurance import (
    INDEPENDENT_READBACK,
    SCREEN_READBACK,
    UNCHECKED,
    assurance_for,
    weakest_assurance,
)
from openadapt_flow.cli_check import (
    build_marks,
    cmd_check_admit,
    cmd_check_show,
    guided_check,
    parse_reset,
)
from openadapt_flow.compiler.readback_authoring import (
    CHECK_PROMPT,
    CheckError,
    DeclaredReset,
    MarkedField,
    admit_check,
    apply_check,
    detect_reset,
    load_check,
    write_check,
)
from openadapt_flow.ir import ActionKind, EffectVerificationEvidence, Step
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    ReadbackField,
    ReadbackNav,
    ReadbackReset,
    ReadbackSpec,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.effects.readback_values import (
    compare_value,
    infer_value_kind,
    normalize_value,
)
from openadapt_flow.verification import VerificationTier
from tests.test_readback_authoring import (
    NOTE_BOX,
    RUN,
    SHOWS,
    VISIT_BOX,
    RecordBackend,
    _anchor,
    _marked_screen,
    _recording,
    _run,
    _workflow,
)

MRN_BOX = (100, 320, 120, 30)


# -- the four outcomes, through a real replay -------------------------------------


def test_a_correct_write_is_confirmed_with_zero_model_calls(tmp_path, monkeypatch):
    backend = RecordBackend()
    report = _run(tmp_path, monkeypatch, backend, RUN)
    assert report.success, report.halt
    assert report.model_calls == 0
    (evidence,) = report.results[-1].effect_evidence
    assert evidence.final_verdict == "confirmed"
    assert evidence.verification_tier == int(
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    assert evidence.assurance == SCREEN_READBACK


def test_a_wrong_value_is_refuted(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, RecordBackend(saved_visit="Triage"), RUN)
    assert not report.success
    assert any("REFUTED" in line for line in report.results[-1].effect_results)
    assert report.results[-1].effect_evidence[-1].assurance == UNCHECKED


def test_a_missing_record_halts_as_indeterminate(tmp_path, monkeypatch):
    # Nothing was saved: the re-opened record shows no value, which proves
    # nothing either way, so the run halts for reconciliation.
    report = _run(tmp_path, monkeypatch, RecordBackend(phantom=True), RUN)
    assert not report.success
    assert any("INDETERMINATE" in line for line in report.results[-1].effect_results)


# -- state reset ------------------------------------------------------------------


def _clicks_only_workflow():
    """A check that clicks away and back: no reload, no search."""
    wf = _workflow()
    steps = wf.steps[:3] + [
        Step(
            id="step_003",
            intent="click 'History' tab",
            action=ActionKind.CLICK,
            anchor=_anchor(10, 10),
        ),
        Step(
            id="step_004",
            intent="click 'Encounter' tab",
            action=ActionKind.CLICK,
            anchor=_anchor(50, 60),
        ),
    ]
    return wf.model_copy(update={"steps": steps})


def test_a_stale_buffer_check_without_reset_is_refused_at_compile(tmp_path):
    # Two clicks used to count as re-opening the record (tier 3). Switching
    # tabs in a client-side page re-renders the same buffer, so it is refused.
    rec = _recording(tmp_path, n_events=5)
    with pytest.raises(CheckError, match="without first throwing away"):
        apply_check(_clicks_only_workflow(), rec, vision=_marked_screen(SHOWS))


def test_a_new_search_is_recognized_as_the_reset(tmp_path):
    updated, outcome = apply_check(
        _workflow(), _recording(tmp_path), vision=_marked_screen(SHOWS)
    )
    assert (outcome.reset, outcome.reset_evidence) == ("new_search", "detected")
    spec = updated.steps[2].effects[0].readback
    assert spec.demonstrated and spec.reset == ReadbackReset(
        kind="new_search", at=1, evidence="detected"
    )
    assert spec.admission is None and outcome.needs_admission


@pytest.mark.parametrize(
    ("key", "modifiers", "kind"),
    [("F5", [], "reload"), ("r", ["ctrl"], "reload"), ("w", ["meta"], "reopen_window")],
)
def test_reload_and_reopen_keys_are_resets(key, modifiers, kind):
    steps = [
        Step(
            id="step_010",
            intent="key",
            action=ActionKind.HOTKEY if modifiers else ActionKind.KEY,
            key=key,
            modifiers=modifiers,
        ),
        Step(
            id="step_011",
            intent="reopen",
            action=ActionKind.CLICK,
            anchor=_anchor(5, 5),
        ),
    ]
    reset = detect_reset(steps)
    assert reset is not None and reset.kind == kind and reset.at == 0


def test_a_close_key_with_nothing_after_it_is_not_a_reset():
    steps = [Step(id="step_010", intent="close", action=ActionKind.KEY, key="Alt+F4")]
    assert detect_reset(steps) is None


def test_a_declared_reset_is_recorded_as_declared(tmp_path):
    rec = tmp_path / "rec"
    _recording(tmp_path, n_events=5)
    marks = load_check(rec)
    write_check(
        rec,
        start_event=3,
        fields=marks.fields,
        reset=DeclaredReset(kind="reload", event=3),
    )
    updated, outcome = apply_check(
        _clicks_only_workflow(), rec, vision=_marked_screen(SHOWS)
    )
    assert (outcome.reset, outcome.reset_evidence) == ("reload", "declared")


def test_a_reset_declared_outside_the_check_is_refused(tmp_path):
    rec = tmp_path / "rec"
    _recording(tmp_path, n_events=5)
    marks = load_check(rec)
    write_check(
        rec,
        start_event=3,
        fields=marks.fields,
        reset=DeclaredReset(kind="reload", event=1),
    )
    with pytest.raises(CheckError, match="isn't a step of the check"):
        apply_check(_clicks_only_workflow(), rec, vision=_marked_screen(SHOWS))


def test_a_hand_edited_demonstrated_check_without_reset_earns_screen_tier():
    spec = _spec(reset=None)
    effect = _effect(spec)
    assert OnScreenReadbackVerifier.verification_tier_for(effect) is (
        VerificationTier.IMMEDIATE_SCREEN
    )


# -- admission --------------------------------------------------------------------


class ScreenBackend:
    """Shows ``text`` in every region after the re-navigation."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def screenshot(self):
        return b"png"

    def click(self, x, y, *, double=False):
        self.calls.append(("click", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def press(self, key):
        self.calls.append(("press", key))


def _ocr_showing(text, confidence=0.95):
    # One line across the whole screen: it is also the label under every
    # click, which the read-only guard reads before clicking.
    return SimpleNamespace(
        ocr=lambda png, region=None: [
            SimpleNamespace(text=text, confidence=confidence, region=(0, 0, 400, 300))
        ]
    )


def _spec(*, reset="default", kind="code", admitted=False, region=MRN_BOX):
    spec = ReadbackSpec(
        region=region,
        different_path=True,
        renavigation=[
            ReadbackNav(action="key", key="F5"),
            ReadbackNav(action="click", point=(50, 60)),
        ],
        fields=[
            ReadbackField(
                name="mrn",
                region=region,
                value=ValueExpr(param="mrn"),
                value_kind=kind,
            )
        ],
        demonstrated=True,
        reset=(ReadbackReset(kind="reload", at=0) if reset == "default" else reset),
    )
    if admitted:
        effect = _effect(spec)
        wf = _workflow().model_copy(
            update={
                "steps": [_workflow().steps[2].model_copy(update={"effects": [effect]})]
            }
        )
        wf, _ = admit_check(wf, admitted_by="reviewer")
        spec = wf.steps[0].effects[0].readback
    return spec


def _effect(spec):
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="mrn",
        value=ValueExpr(param="mrn"),
        readback=spec,
    )


def _verify(spec, shows, params=None, confidence=0.95):
    params = params or {"mrn": "MG44O8"}
    effect = _effect(spec).resolve(params)
    verifier = OnScreenReadbackVerifier(
        ScreenBackend(shows), vision=_ocr_showing(shows, confidence)
    )
    before = EffectState(substrate="onscreen", reachable=False)
    return verifier.verify(effect, before, params)


def test_an_unadmitted_check_confirms_nothing():
    verdict = _verify(_spec(), "MG44O8")
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "no person has admitted it" in verdict.reason
    assert verdict.assurance == UNCHECKED


def test_an_admitted_check_confirms_as_a_screen_readback():
    spec = _spec(admitted=True)
    assert spec.admitted() and spec.admission.admitted_by == "reviewer"
    verdict = _verify(spec, "MG44O8")
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.assurance == SCREEN_READBACK


def test_an_admission_does_not_carry_over_to_a_changed_check():
    spec = _spec(admitted=True)
    moved = spec.model_copy(
        update={
            "fields": [spec.fields[0].model_copy(update={"region": (0, 0, 50, 20)})]
        }
    )
    assert not moved.admitted()
    verdict = _verify(moved, "MG44O8")
    assert verdict.verdict is Verdict.INDETERMINATE


def _admitted_by(spec, who):
    """A hand-written admission with a correct digest, in ``who``'s name."""
    from openadapt_flow.runtime.effects.effect import ReadbackAdmission

    clean = spec.model_copy(update={"admission": None})
    return clean.model_copy(
        update={
            "admission": ReadbackAdmission(
                admitted_by=who,
                admitted_at="2026-10-17T00:00:00Z",
                spec_digest=clean.admission_digest(),
            )
        }
    )


@pytest.mark.parametrize(
    "who", ["agent", "agent:anthropic", "model:x", "bot:ci", "person"]
)
def test_an_agent_cannot_admit_a_check(who):
    effect = _effect(_spec())
    wf = _workflow().model_copy(
        update={
            "steps": [_workflow().steps[2].model_copy(update={"effects": [effect]})]
        }
    )
    with pytest.raises(CheckError, match="not a person"):
        admit_check(wf, admitted_by=who)
    # Written into the bundle by hand, with the right digest, it admits nothing.
    forged = _admitted_by(_spec(), who)
    assert not forged.admitted()
    verdict = _verify(forged, "MG44O8")
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.assurance == UNCHECKED
    # The same hand-written admission in a person's name is the control case.
    assert _verify(_admitted_by(_spec(), "reviewer"), "MG44O8").confirmed


def test_the_check_records_who_demonstrated_it(tmp_path):
    rec = _recording(tmp_path)
    meta = rec / "meta.json"
    data = json.loads(meta.read_text()) if meta.is_file() else {}
    meta.write_text(json.dumps({**data, "demonstrated_by": "agent"}))
    updated, _ = apply_check(_workflow(), rec, vision=_marked_screen(SHOWS))
    step = updated.steps[2]
    spec = step.effects[0].readback
    assert spec.demonstrated_by == "agent"
    from openadapt_flow.compiler.readback_authoring import describe_check

    assert "demonstrated by agent" in describe_check(step, step.effects[0])


def test_a_check_without_reset_cannot_be_admitted():
    effect = _effect(_spec(reset=None))
    wf = _workflow().model_copy(
        update={
            "steps": [_workflow().steps[2].model_copy(update={"effects": [effect]})]
        }
    )
    with pytest.raises(CheckError, match="no state reset"):
        admit_check(wf, admitted_by="reviewer")


# -- OCR ambiguity and normalization ----------------------------------------------


@pytest.mark.parametrize(
    ("expected", "shows"),
    [("MG44O8", "MG4408"), ("AL1001", "A11001"), ("ID-10O1", "ID-1001")],
)
def test_ocr_glyph_confusion_is_indeterminate_never_a_pass(expected, shows):
    verdict = _verify(_spec(admitted=True), shows, params={"mrn": expected})
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "can't tell apart" in verdict.reason


def test_a_confusable_last_glyph_in_free_text_is_indeterminate():
    # The fuzzy text match would accept 9 of 10 characters; the glyph rule
    # stops it.
    spec = _spec(admitted=True, kind="text")
    verdict = _verify(spec, "acct 10021", params={"mrn": "acct 1002l"})
    assert verdict.verdict is Verdict.INDETERMINATE


def test_a_legible_different_code_is_refuted():
    verdict = _verify(_spec(admitted=True), "MG4471")
    assert verdict.verdict is Verdict.REFUTED


def test_a_structure_read_has_no_glyph_excuse():
    assert compare_value("MG4408", "MG44O8", kind="code", source="structure") == (
        "mismatch"
    )


@pytest.mark.parametrize(
    ("observed", "expected", "kind", "order", "result"),
    [
        ("2026-03-04", "Mar 4, 2026", "date", None, "match"),
        ("4 March 2026", "2026-03-04", "date", None, "match"),
        ("03/04/2026", "2026-03-04", "date", None, "ambiguous"),
        ("03/04/2026", "2026-03-04", "date", "mdy", "match"),
        ("03/04/2026", "2026-03-04", "date", "dmy", "mismatch"),
        ("2026-03-05", "2026-03-04", "date", None, "mismatch"),
        ("$1,250.00", "1250", "currency", None, "match"),
        ("USD 1250.50", "$1,250.5", "currency", None, "match"),
        ("$1,205.00", "1250", "currency", None, "mismatch"),
        ("$1,25O.00", "1250", "currency", None, "ambiguous"),
        ("mg-44-08", "MG4408", "code", None, "match"),
        ("MG44 08", "MG4408", "code", None, "match"),
    ],
)
def test_values_are_normalized_by_kind(observed, expected, kind, order, result):
    assert (
        compare_value(observed, expected, kind=kind, source="ocr", date_order=order)
        == result
    )


def test_normalize_and_infer():
    assert normalize_value("Mar 4, 2026", "date") == "2026-03-04"
    assert normalize_value("03/04/2026", "date") is None  # two readings
    assert normalize_value("$1,250.00", "currency") == "1250"
    assert normalize_value("mg-44-08", "code") == "mg4408"
    assert infer_value_kind("2026-03-04") == "date"
    assert infer_value_kind("$12.50") == "currency"
    assert infer_value_kind("MG44O8") == "code"
    assert infer_value_kind("chest pain follow up") == "text"


def test_the_compiler_infers_a_code_field(tmp_path):
    wf = _workflow().model_copy(
        update={"params": {**_workflow().params, "visit": "ENC-2041"}}
    )
    rec = _recording(tmp_path)
    shows = {NOTE_BOX: SHOWS[NOTE_BOX], VISIT_BOX: "ENC-2041"}
    updated, _ = apply_check(wf, rec, vision=_marked_screen(shows))
    kinds = [f.value_kind for f in updated.steps[2].effects[0].readback.fields]
    assert kinds == ["text", "code"]


# -- non-DOM surfaces -------------------------------------------------------------


class A11yBackend(ScreenBackend):
    """A native backend whose accessibility tree shows ``element``."""

    def __init__(self, element):
        super().__init__("")
        self.element = element

    def displayed_text_at(self, x, y):
        return self.element


def _verify_structure(element, expected="MG44O8"):
    spec = _spec(admitted=True)
    spec = spec.model_copy(
        update={"fields": [spec.fields[0].model_copy(update={"source": "structure"})]}
    )
    # Re-admit: the field changed.
    effect = _effect(spec.model_copy(update={"admission": None}))
    wf = _workflow().model_copy(
        update={
            "steps": [_workflow().steps[2].model_copy(update={"effects": [effect]})]
        }
    )
    wf, _ = admit_check(wf, admitted_by="reviewer")
    params = {"mrn": expected}
    resolved = wf.steps[0].effects[0].resolve(params)
    verifier = OnScreenReadbackVerifier(
        A11yBackend(element), vision=_ocr_showing("unused")
    )
    return verifier.verify(
        resolved, EffectState(substrate="onscreen", reachable=False), params
    )


def test_an_accessibility_read_confirms_the_exact_value():
    assert _verify_structure("MG44O8").verdict is Verdict.CONFIRMED


def test_an_accessibility_row_that_contains_the_value_is_indeterminate():
    # A whole row (name, DOB, MRN) is not the field.
    verdict = _verify_structure("Ada Park 1980-02-01 MG44O8", expected="MG44O8")
    assert verdict.verdict is Verdict.INDETERMINATE


def test_an_accessibility_read_of_another_value_is_refuted():
    assert _verify_structure("MG4471").verdict is Verdict.REFUTED


def test_native_backends_offer_displayed_text_but_windows_does_not():
    from openadapt_flow.backends.linux_backend import LinuxBackend
    from openadapt_flow.backends.macos_backend import MacOSBackend
    from openadapt_flow.backends.windows_backend import WindowsBackend

    assert hasattr(MacOSBackend, "displayed_text_at")
    assert hasattr(LinuxBackend, "displayed_text_at")
    # UIA's point read returns a row without the clicked cell, not a field.
    assert not hasattr(WindowsBackend, "displayed_text_at")


# -- graded assurance -------------------------------------------------------------


def test_assurance_grades_follow_the_tier():
    assert assurance_for(VerificationTier.INDEPENDENT_SYSTEM) == INDEPENDENT_READBACK
    # A separate signed-in session still reads the application's screens, so
    # it is a screen read-back even though it is independent of actuation.
    assert VerificationTier.INDEPENDENT_SESSION.is_independent_of_actuation()
    assert assurance_for(VerificationTier.INDEPENDENT_SESSION) == SCREEN_READBACK
    assert (
        assurance_for(VerificationTier.PERSISTED_STATE_REACQUISITION) == SCREEN_READBACK
    )
    assert assurance_for(VerificationTier.IMMEDIATE_SCREEN) == SCREEN_READBACK
    assert assurance_for(1, confirmed=False) == UNCHECKED
    assert assurance_for(None) == UNCHECKED
    assert weakest_assurance([INDEPENDENT_READBACK, SCREEN_READBACK]) == (
        SCREEN_READBACK
    )
    assert weakest_assurance([]) == UNCHECKED


def test_evidence_assurance_is_derived_and_not_stored():
    evidence = EffectVerificationEvidence(
        effect_contract_hash="sha256:" + "0" * 64,
        substrate="rest",
        verification_tier=1,
        initial_verdict="confirmed",
        final_verdict="confirmed",
    )
    assert evidence.assurance == INDEPENDENT_READBACK
    assert "assurance" not in evidence.model_dump(mode="json")


def test_old_readback_contracts_serialize_as_before():
    spec = ReadbackSpec(
        region=(1, 2, 3, 4),
        different_path=True,
        renavigation=[ReadbackNav(action="click", point=(1, 1))] * 2,
        fields=[
            ReadbackField(name="a", region=(1, 2, 3, 4), value=ValueExpr(param="a"))
        ],
    )
    dumped = spec.model_dump(mode="json")
    assert not {"demonstrated", "reset", "admission"} & set(dumped)
    assert not {"value_kind", "date_order"} & set(dumped["fields"][0])


# -- the CLI ----------------------------------------------------------------------


def _bundle(tmp_path):
    updated, _ = apply_check(
        _workflow(), _recording(tmp_path), vision=_marked_screen(SHOWS)
    )
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    updated.save(bundle)
    return bundle


def _admit_args(bundle, by="Richard"):
    return argparse.Namespace(bundle=str(bundle), step=None, by=by)


def test_admit_refuses_without_a_terminal(tmp_path, capsys):
    bundle = _bundle(tmp_path)
    code = cmd_check_admit(_admit_args(bundle), interactive=lambda: False)
    assert code == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_admit_needs_the_typed_word(tmp_path):
    from openadapt_flow.ir import Workflow

    bundle = _bundle(tmp_path)
    code = cmd_check_admit(
        _admit_args(bundle), ask=lambda _: "y", interactive=lambda: True
    )
    assert code == 1
    assert Workflow.load(bundle).steps[2].effects[0].readback.admission is None


def test_a_person_admits_at_the_terminal_and_it_is_recorded(tmp_path, capsys):
    from openadapt_flow.ir import Workflow

    bundle = _bundle(tmp_path)
    code = cmd_check_admit(
        _admit_args(bundle), ask=lambda _: "admit", interactive=lambda: True
    )
    assert code == 0
    spec = Workflow.load(bundle).steps[2].effects[0].readback
    assert spec.admitted()
    assert spec.admission.admitted_by == "Richard"
    assert spec.admission.role == "person"
    out = capsys.readouterr().out
    assert "state reset: new_search (detected)" in out

    cmd_check_show(argparse.Namespace(bundle=str(bundle), as_json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["admitted"] is True and rows[0]["reset"] == "new_search"


def test_guided_check_asks_and_writes_check_json(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"kind": "type", "text": "x"},
                {"kind": "key", "key": "Enter"},
                {"kind": "key", "key": "F5"},
                {"kind": "click", "x": 5, "y": 5},
            ]
        )
        + "\n"
    )
    answers = iter(["2", "note=100,200,300,40", "", ""])
    said: list[str] = []
    path = guided_check(rec, ask=lambda _: next(answers), echo=said.append)
    assert path is not None and said[0] == CHECK_PROMPT
    marks = load_check(rec)
    assert marks.start_event == 2 and marks.prompt == CHECK_PROMPT
    assert marks.fields == [MarkedField(param="note", region=(100, 200, 300, 40))]


def test_cli_parsers():
    marks = build_marks(["mrn=1,2,3,4"], kinds=["mrn=code"])
    assert marks[0].value_kind == "code" and marks[0].region == (1, 2, 3, 4)
    assert parse_reset("fresh_session@7") == DeclaredReset(
        kind="fresh_session", event=7
    )
    with pytest.raises(ValueError):
        parse_reset("reload")


def test_check_commands_through_main(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    bundle = _bundle(tmp_path / "b")
    # Under pytest stdin is not a terminal, so admission is refused.
    assert main(["check", "admit", str(bundle)]) == 2
    rec = tmp_path / "r"
    rec.mkdir()
    code = main(
        [
            "check",
            "mark",
            str(rec),
            "--start-event",
            "3",
            "--field",
            "mrn=1,2,30,10",
            "--kind",
            "mrn=code",
            "--reset",
            "fresh_session@3",
        ]
    )
    assert code == 0
    marks = load_check(rec)
    assert marks.reset == DeclaredReset(kind="fresh_session", event=3)
    assert marks.fields[0].value_kind == "code"


def test_assurance_grades_are_the_published_contract_labels() -> None:
    """The grades are the shared ReadBackTier labels, strongest first."""

    from openadapt_flow.assurance import ASSURANCE_GRADES, assurance_for
    from openadapt_flow.contracts.approval import ReadBackTier

    assert ASSURANCE_GRADES == tuple(tier.label for tier in sorted(ReadBackTier))
    from typing import get_args

    from openadapt_flow.assurance import Assurance
    from openadapt_flow.contracts.approval import VerificationTierLabel

    assert get_args(Assurance) == get_args(VerificationTierLabel)
    for flow_tier in (1, 2, 3, 4):
        grade = assurance_for(flow_tier)
        expected = (
            ReadBackTier.INDEPENDENT_READBACK
            if flow_tier == 1
            else ReadBackTier.SCREEN_READBACK
        )
        assert ReadBackTier.from_label(grade) is expected
    assert ReadBackTier.from_label(assurance_for(1, confirmed=False)) is (
        ReadBackTier.UNCHECKED
    )
