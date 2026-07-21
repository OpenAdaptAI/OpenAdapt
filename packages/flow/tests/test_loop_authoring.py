"""Data-driven LOOP authoring (RFC docs/design/WORKFLOW_PROGRAM_IR.md §2.3).

The AUTHORING WIRE from a single demonstration to a governed
``run-this-for-each-record`` loop. The Phase-2 interpreter already runs a
``LOOP`` over a worklist safely (bounded, ``$0``, zero-model, identity-gated and
effect-verified per iteration, halt-on-ambiguity -- proven by
``test_program_ir_phase2``). These tests pin the missing compile-time step:
:func:`~openadapt_flow.compiler.loop_authoring.author_data_driven_loop`, which
wraps a demonstrated linear body in exactly that ``LOOP``.

Two layers of evidence:

1. *Authoring unit tests* -- the emitted ``ProgramGraph`` shape, the explicit
   column -> parameter mapping, and the FAIL-LOUD validation (a mismatch never
   compiles a bundle).
2. *Real-Replayer evidence* -- an authored loop run through the ACTUAL
   ``Replayer._interpret_program`` (backend + vision faked as everywhere in this
   suite; ZERO model calls), proving a healthy multi-record run confirms each
   record's write INDEPENDENTLY against the real in-process MockMed system of
   record, stays bounded, and that a POISONED record (identity mismatch OR an
   effect the system of record refutes) triggers a safe HALT -- no wrong write,
   no silent skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from openadapt_flow.compiler.loop_authoring import (
    DEFAULT_BODY_ID,
    LOOP_STATE_ID,
    LoopAuthoringError,
    author_data_driven_loop,
    body_param_names,
    resolve_column_map,
)
from openadapt_flow.ir import (
    ActionKind,
    ParamSpec,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.replayer import Replayer

# Reuse the scripted fakes from the main replayer unit tests (pytest's prepend
# import mode puts tests/ on sys.path).
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    OcrLine,
    context_click_step,
    make_png,
)

# ===========================================================================
# fixtures / helpers
# ===========================================================================


@pytest.fixture()
def dirs(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle, tmp_path / "run"


def _typed_note_body() -> Workflow:
    """A single-demonstration linear body: type a note into a form (the
    ``note`` slot is the recorded parameter)."""
    return Workflow(
        name="note-demo",
        steps=[
            Step(
                id="type_note",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            )
        ],
        param_specs={
            "note": ParamSpec(name="note", example="demo note"),
        },
    )


def _encounter_body() -> Workflow:
    """A single-demonstration 'save encounter' body: type the patient id, type
    the note, then save -- the save is a consequential write with typed,
    PARAM-BOUND system-of-record effects (so each record is verified against the
    value it actually wrote, RFC §Effect / P0-3)."""
    return Workflow(
        name="save-encounter",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            ),
            Step(
                id="type_note",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            ),
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={
                            "patient_id": ValueExpr(param="patient_id"),
                            "type": "Triage",
                        },
                        expected_count=1,
                        timeout_s=2.0,
                    ),
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"patient_id": ValueExpr(param="patient_id")},
                        field="note",
                        value=ValueExpr(param="note"),
                        timeout_s=2.0,
                    ),
                ],
            ),
        ],
        param_specs={
            "patient_id": ParamSpec(name="patient_id", example="phil"),
            "note": ParamSpec(name="note", example="phil-note"),
        },
    )


class _RowWritingBackend(FakeBackend):
    """A fake GUI backend that, on the consequential save keypress, POSTs the
    record it just 'typed' this iteration to the MockMed system of record.

    Captures the two ``type_text`` calls of the body in order (patient id, then
    note) and writes them; ``corrupt`` optionally simulates a GUI drift by
    writing a DIFFERENT patient id than was typed for one record (so the
    param-bound effect verifier -- looking for the record's OWN patient -- can
    catch the wrong write and HALT)."""

    def __init__(self, sor_url, *, corrupt=None):
        super().__init__(viewport=(300, 200))
        self.sor_url = sor_url.rstrip("/")
        self._typed: list[str] = []
        self.posted: list[tuple[str, str]] = []
        self._corrupt = dict(corrupt or {})

    def type_text(self, text):
        super().type_text(text)
        self._typed.append(text)

    def press(self, key):
        super().press(key)
        patient_id, note = self._typed[0], self._typed[1]
        written = self._corrupt.get(patient_id, patient_id)
        requests.post(
            f"{self.sor_url}/api/encounter",
            json={"patient_id": written, "type": "Triage", "note": note},
            timeout=5,
        )
        self.posted.append((written, note))
        self._typed = []


def _vision_confirms_saved() -> FakeVision:
    vision = FakeVision()
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    return vision


class _ScriptedIdentityVision(FakeVision):
    """A vision that resolves the recorded target every iteration (constant
    template match) but returns a SCRIPTED identity band per iteration -- so one
    record's live band can name a DIFFERENT entity and trip the identity gate."""

    def __init__(self, bands: list[str]):
        super().__init__()
        self._match = Match(
            point=(110, 105), region=(100, 100, 50, 20), confidence=0.99
        )
        self._bands = list(bands)
        self._i = 0

    def find_template(self, *args, **kwargs):
        self.template_calls.append(kwargs.get("search_region"))
        return self._match

    def ocr(self, screen_png, *, region=None):
        band = self._bands[min(self._i, len(self._bands) - 1)]
        self._i += 1
        return [OcrLine(band)]


# ===========================================================================
# 1. authoring: emitted graph shape + explicit, validated mapping
# ===========================================================================


def test_author_emits_single_loop_over_the_demonstrated_body():
    body = _typed_note_body()
    records = [{"note": "a"}, {"note": "b"}, {"note": "c"}]

    looped = author_data_driven_loop(body, records, loop_var="note")

    # A program:true bundle with exactly one LOOP entry over one relation.
    assert isinstance(looped.program, ProgramGraph)
    assert looped.program.entry == LOOP_STATE_ID
    loop_state = looped.program.states[LOOP_STATE_ID]
    assert loop_state.kind is StateKind.LOOP
    assert loop_state.loop is not None
    assert loop_state.loop.body == DEFAULT_BODY_ID
    # The body subflow is the mechanical lift of the demonstrated linear steps.
    assert DEFAULT_BODY_ID in looped.subflows
    body_states = looped.subflows[DEFAULT_BODY_ID].states
    assert any(
        s.kind is StateKind.ACTION and s.step is not None and s.step.id == "type_note"
        for s in body_states.values()
    )
    # Each record became one worklist row bound to the body's param.
    rel = looped.data_sources[loop_state.loop.relation]
    assert [r["note"] for r in rel.rows] == ["a", "b", "c"]
    # The demonstrated body's params/steps are preserved untouched.
    assert list(looped.param_specs) == ["note"]
    assert [s.id for s in looped.steps] == ["type_note"]


def test_author_remaps_worklist_columns_to_params_explicitly():
    body = _typed_note_body()
    records = [{"clinical_note": "hello"}]

    looped = author_data_driven_loop(
        body, records, column_map={"clinical_note": "note"}
    )

    rel = looped.data_sources["worklist"]
    assert rel.rows == [{"note": "hello"}]  # remapped column -> param


def test_body_param_names_excludes_secrets():
    body = Workflow(
        name="login",
        steps=[
            Step(id="u", intent="type user", action=ActionKind.TYPE, param="user"),
            Step(
                id="p",
                intent="type password",
                action=ActionKind.TYPE,
                param="password",
                secret=True,
            ),
        ],
        secret_params=["password"],
    )
    assert body_param_names(body) == {"user"}


@pytest.mark.parametrize(
    "records, column_map, needle",
    [
        # an unmapped worklist column (silently dropped data)
        ([{"note": "x", "extra": "y"}], None, "extra"),
        # a column mapping to an unknown parameter
        ([{"note": "x"}], {"note": "ghost"}, "ghost"),
    ],
)
def test_author_fails_loud_on_mismatch(records, column_map, needle):
    body = _typed_note_body()
    with pytest.raises(LoopAuthoringError) as exc:
        author_data_driven_loop(body, records, column_map=column_map)
    assert needle in str(exc.value)


def test_author_refuses_to_bind_a_secret_from_a_worklist():
    body = Workflow(
        name="login",
        steps=[
            Step(
                id="p",
                intent="type password",
                action=ActionKind.TYPE,
                param="password",
                secret=True,
            )
        ],
        param_specs={"password": ParamSpec(name="password", example=None)},
        secret_params=["password"],
    )
    with pytest.raises(LoopAuthoringError) as exc:
        author_data_driven_loop(body, [{"password": "hunter2"}])
    assert "SECRET" in str(exc.value)


def test_author_refuses_empty_and_bound_exceeding_worklists():
    body = _typed_note_body()
    with pytest.raises(LoopAuthoringError):
        author_data_driven_loop(body, [])
    with pytest.raises(LoopAuthoringError) as exc:
        author_data_driven_loop(
            body, [{"note": str(i)} for i in range(4)], max_iterations=2
        )
    assert "max_iterations" in str(exc.value)


def test_resolve_column_map_reports_every_problem_at_once():
    body = _typed_note_body()
    with pytest.raises(LoopAuthoringError) as exc:
        resolve_column_map(["a", "b"], body, {"a": "ghost"})
    msg = str(exc.value)
    assert "b" in msg  # unmapped column
    assert "ghost" in msg  # unknown param


def test_authored_bundle_round_trips_through_save_load(dirs):
    bundle, _run = dirs
    body = _typed_note_body()
    looped = author_data_driven_loop(body, [{"note": "a"}, {"note": "b"}])
    looped.save(bundle)
    reloaded = Workflow.load(bundle)
    assert reloaded.program is not None
    assert reloaded.program.states[LOOP_STATE_ID].kind is StateKind.LOOP
    assert len(reloaded.data_sources["worklist"].rows) == 2


# ===========================================================================
# 2. real-Replayer evidence: healthy multi-record run + poisoned-record HALT
# ===========================================================================


def test_healthy_loop_confirms_each_record_zero_model_calls(dirs):
    """An authored loop over a 3-record worklist runs the demonstrated body
    once per record through the REAL interpreter: every record's write is
    INDEPENDENTLY effect-verified against the in-process MockMed system of
    record, the run stays bounded, and it makes ZERO model calls."""
    bundle, run_dir = dirs
    body = _encounter_body()
    records = [
        {"patient_id": "alice", "note": "alice triage"},
        {"patient_id": "bob", "note": "bob triage"},
        {"patient_id": "cara", "note": "cara triage"},
    ]
    looped = author_data_driven_loop(body, records, loop_var="patient")

    url, db, stop = fault_serve()
    try:
        backend = _RowWritingBackend(url)
        report = Replayer(
            backend,
            vision=_vision_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        ).run(looped, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        assert report.terminal_outcome == "success"
        assert report.model_calls == 0  # $0 runtime preserved across the loop
        # Body ran once per record, in order, binding each record.
        assert backend.posted == [
            ("alice", "alice triage"),
            ("bob", "bob triage"),
            ("cara", "cara triage"),
        ]
        # Each record's consequential write was effect-verified (CONFIRMED).
        saves = [r for r in report.results if r.step_id == "save"]
        assert len(saves) == 3
        assert all(r.effect_verified is True for r in saves)
        # The system of record holds exactly the three intended encounters.
        written = {rec["patient_id"] for rec in db.snapshot()["records"]}
        assert written == {"alice", "bob", "cara"}
    finally:
        stop()


def test_poisoned_record_effect_refute_halts_no_silent_skip(dirs):
    """One record's GUI write drifts to the WRONG patient. The param-bound
    effect verifier -- checking the record's OWN patient -- refutes and the run
    HALTs on that record: the healthy record before it committed, the poisoned
    record is NOT reported as success, and the record AFTER it never runs (a
    HALT, never a silent skip that marches on)."""
    bundle, run_dir = dirs
    body = _encounter_body()
    records = [
        {"patient_id": "alice", "note": "alice triage"},
        {"patient_id": "bob", "note": "bob triage"},  # poisoned below
        {"patient_id": "cara", "note": "cara triage"},
    ]
    looped = author_data_driven_loop(body, records)

    url, db, stop = fault_serve()
    try:
        # The GUI, for bob, actually writes 'mallory' (a wrong-patient drift).
        backend = _RowWritingBackend(url, corrupt={"bob": "mallory"})
        report = Replayer(
            backend,
            vision=_vision_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        ).run(looped, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        assert report.terminal_outcome == "halt"
        # alice committed; bob's write drifted; cara NEVER ran (halt, not skip).
        assert backend.posted == [("alice", "alice triage"), ("mallory", "bob triage")]
        assert not any(pid == "cara" for pid, _ in backend.posted)
        # bob's save was NOT verified (the effect gate caught the wrong write).
        bob_save = [r for r in report.results if r.step_id == "save"][-1]
        assert bob_save.effect_verified is False
        # No encounter for 'cara' exists in the system of record.
        assert not any(rec["patient_id"] == "cara" for rec in db.snapshot()["records"])
    finally:
        stop()


def test_committed_showcase_loop_bundle_is_a_valid_program(dirs):
    """The SHIPPED data-driven-loop showcase bundle
    (``docs/showcase-loop/bundle``, built by
    ``scripts/build_showcase_loop_bundle.py``) is a real, loadable, PHI-free
    ``program:true`` artifact -- the proof the authoring wire is reachable
    end-to-end on a real bundle, guarded in CI so it cannot silently regress to
    ``program:false``."""
    repo = Path(__file__).resolve().parent.parent
    bundle = repo / "docs" / "showcase-loop" / "bundle"
    wf = Workflow.load(bundle)
    assert wf.program is not None  # program:true (the missing piece, now shipped)
    assert wf.contains_phi is False
    loop_states = [s for s in wf.program.states.values() if s.kind is StateKind.LOOP]
    assert len(loop_states) == 1
    loop = loop_states[0].loop
    assert loop is not None
    # The loop's body subflow and worklist relation both resolve.
    assert loop.body in wf.subflows
    assert loop.relation in wf.data_sources
    assert len(wf.data_sources[loop.relation].rows) >= 1


def test_committed_encounter_showcase_loop_replays_through_the_interpreter(tmp_path):
    """The SHIPPED compact showcase bundle
    (``docs/showcase-encounter-loop/bundle``, built by
    ``scripts/build_showcase_encounter_loop_bundle.py`` and rendered in the
    launcher README) is a real, loadable, PHI-free ``program:true`` loop whose
    body actually REPLAYS once per worklist record through the real
    ``Replayer._interpret_program``: every record's consequential write is
    INDEPENDENTLY effect-verified against the in-process MockMed system of
    record and the run makes ZERO model calls -- the proof the README graph is
    an interpreted program, not a hand-drawn diagram."""
    repo = Path(__file__).resolve().parent.parent
    bundle = repo / "docs" / "showcase-encounter-loop" / "bundle"
    wf = Workflow.load(bundle)

    # Static shape: program:true, PHI-free, exactly one LOOP whose body + relation
    # resolve, guarded so it cannot silently regress to program:false.
    assert wf.program is not None
    assert wf.contains_phi is False
    loop_states = [s for s in wf.program.states.values() if s.kind is StateKind.LOOP]
    assert len(loop_states) == 1
    loop = loop_states[0].loop
    assert loop is not None
    assert loop.body in wf.subflows
    assert loop.relation in wf.data_sources
    records = wf.data_sources[loop.relation].rows
    assert len(records) >= 1

    # Dynamic proof: the loop actually interprets, once per record.
    run_dir = tmp_path / "run"
    url, db, stop = fault_serve()
    try:
        backend = _RowWritingBackend(url)
        report = Replayer(
            backend,
            vision=_vision_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        ).run(wf, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        assert report.terminal_outcome == "success"
        assert report.model_calls == 0  # $0 runtime preserved across the loop
        # The body ran once per record, in worklist order.
        assert [pid for pid, _ in backend.posted] == [r["patient_id"] for r in records]
        # Each record's consequential write was effect-verified (CONFIRMED).
        saves = [r for r in report.results if r.step_id == "save"]
        assert len(saves) == len(records)
        assert all(r.effect_verified is True for r in saves)
        # The system of record holds exactly the intended encounters.
        written = {rec["patient_id"] for rec in db.snapshot()["records"]}
        assert written == {r["patient_id"] for r in records}
    finally:
        stop()


def test_poisoned_record_identity_mismatch_halts_no_wrong_click(dirs):
    """A body whose action is identity-gated (clicks a named target). The
    worklist's second record lands on a screen whose live band names a DIFFERENT
    entity: the identity gate refuses and the run HALTs -- without ever clicking
    the wrong row -- exactly as a linear identity mismatch would, now PER
    RECORD inside the authored loop."""
    bundle, run_dir = dirs
    click = context_click_step(
        "Jane Sample Knee pain referral High", step_id="open_row"
    )
    body = Workflow(
        name="open-referral",
        steps=[click],
        # A benign per-record parameter so the worklist has a column to bind.
        param_specs={"row": ParamSpec(name="row", example="1")},
    )
    # Make the click bind the row param (so the worklist is meaningfully typed).
    body.steps[0].param = None  # a CLICK types nothing; row is metadata only

    records = [{"row": "1"}, {"row": "2"}]
    looped = author_data_driven_loop(body, records)

    backend = FakeBackend()
    # iteration 1: band matches (verified); iteration 2: band names another
    # entity (mismatch -> HALT).
    vision = _ScriptedIdentityVision(
        bands=[
            "Jane Sample Knee pain referral High",
            "Taylor Duplicate Knee pain referral High",
        ]
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        looped, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert report.terminal_outcome == "halt"
    # Exactly ONE click happened (record 1); record 2 refused before clicking.
    clicks = [a for a in backend.actions if a[0] == "click"]
    assert len(clicks) == 1
    mismatched = [
        r
        for r in report.results
        if r.identity is not None and r.identity.status == "mismatch"
    ]
    assert len(mismatched) == 1
