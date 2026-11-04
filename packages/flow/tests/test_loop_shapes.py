"""Loops that are not back-to-back repeats: induce, then run held-out rows.

The demonstrations work a list the way people do: search a patient, open the
chart, write a note, save, go back to the list; the last row ends without
going back. Induction must turn that into a loop whose every pass selects the
row's own record (identity-checked) and checks the row's own write.

The behavioral tests run the induced program through the real interpreter
against a small in-memory chart application, with rows the demonstrations
never showed, and check the application's records afterwards: each held-out
row got its note, nothing else changed, and a row whose identity does not
match stops before any input reaches that record.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

from openadapt_flow.compiler.induction import induce_program
from openadapt_flow.compiler.loop_shapes import merge, segment
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    PredicateKind,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match, OcrLine

# --------------------------------------------------------------------------
# The application: a patient list with search, a chart with a note field
# --------------------------------------------------------------------------

PATIENTS = {
    "Phil Crawford": "1972-02-09",
    "Alice Moreno": "1985-03-04",
    "Bob Lindqvist": "1990-07-21",
    "Carol Okafor": "1968-11-30",
    "Dan Whitaker": "1979-01-15",
    "Susan Underwood": "1980-05-05",
    "Robert Getting": "1961-09-09",
}

#: Control -> (template file, region, click point, screen it shows on).
CONTROLS = {
    "search": ("templates/search.png", (20, 10, 60, 16), (40, 18), "list"),
    "row": ("templates/row.png", (100, 50, 50, 20), (120, 60), "list"),
    "note": ("templates/note.png", (40, 90, 80, 16), (60, 98), "chart"),
    "save": ("templates/save.png", (200, 140, 40, 16), (210, 148), "chart"),
    "back": ("templates/back.png", (10, 175, 40, 16), (20, 183), "chart"),
    "signout": ("templates/signout.png", (250, 10, 40, 16), (260, 18), "any"),
}
_COLORS = {name: (20 + 30 * i, 200 - 25 * i, 90) for i, name in enumerate(CONTROLS)}


def _png(size, color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _template_bytes(name: str) -> bytes:
    _path, region, _point, _screen = CONTROLS[name]
    return _png((region[2], region[3]), _COLORS[name])


class ChartApp(FakeBackend):
    """Records live in ``notes``; the screen is the list or one chart."""

    def __init__(self):
        super().__init__()
        self.notes = {name: "" for name in PATIENTS}
        self.screen = "list"
        self.focus: Optional[str] = None
        self.buffers = {"search": "", "note": ""}
        self.results: list[str] = []
        self.open_patient: Optional[str] = None

    def _at(self, x, y) -> Optional[str]:
        for name, (_p, (rx, ry, rw, rh), _pt, screen) in CONTROLS.items():
            if (
                screen in (self.screen, "any")
                and rx <= x < rx + rw
                and ry <= y < ry + rh
            ):
                return name
        return None

    def click(self, x, y, *, double=False):
        super().click(x, y, double=double)
        hit = self._at(x, y)
        if hit in ("search", "note"):
            self.focus = hit
            self._text_value = self.buffers[hit]
        elif hit == "row" and self.results:
            self.open_patient = self.results[0]
            self.screen = "chart"
            self.buffers["note"] = self.notes[self.open_patient]
            self.focus = None
        elif hit == "save" and self.open_patient is not None:
            self.notes[self.open_patient] = self.buffers["note"]
        elif hit == "back":
            self.screen = "list"
            self.open_patient = None
            self.focus = None

    def type_text(self, text):
        super().type_text(text)
        if self.focus is not None:
            self.buffers[self.focus] = self._text_value

    def press(self, key):
        super().press(key)
        if key == "Enter" and self.focus == "search":
            q = self.buffers["search"].casefold()
            self.results = [n for n in PATIENTS if n.casefold() == q]
            self.buffers["search"] = ""
            self._text_value = ""


class ChartVision(FakeVision):
    """Sees what the app shows: its controls, the result row, the row band."""

    def __init__(self, app: ChartApp):
        super().__init__()
        self.app = app
        self._by_template = {_template_bytes(n): n for n in CONTROLS}

    def find_template(self, screen_png, template_png, **kwargs):
        name = self._by_template.get(template_png)
        if name is None or CONTROLS[name][3] not in (self.app.screen, "any"):
            return None
        if name == "row":
            # The crop shows the demonstrated row; another row never matches.
            return None
        _p, region, point, _s = CONTROLS[name]
        return Match(point=point, region=region, confidence=0.97)

    def find_text(self, screen_png, text, **kwargs):
        if self.app.screen == "list" and self.app.results:
            if text.casefold() == self.app.results[0].casefold():
                return Match(point=(120, 60), region=(100, 50, 50, 20), confidence=0.95)
        return None

    def ocr(self, screen_png, *, region=None):
        if self.app.screen == "list" and self.app.results:
            name = self.app.results[0]
            return [
                OcrLine(
                    f"Open chart for {name} {PATIENTS[name]}", region=(160, 52, 240, 16)
                )
            ]
        return []


class NoteStore:
    """Effect verifier: reads the app's records (the system of record)."""

    substrate = "chart-app"

    def __init__(self, app: ChartApp):
        self.app = app

    def capture_pre_state(self):
        return EffectState(substrate=self.substrate, reachable=True)

    def verify(self, effect, before):
        del before
        name = str(effect.match["name"])
        ok = self.app.notes.get(name) == str(effect.value)
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if ok else Verdict.REFUTED,
            kind=effect.kind,
            substrate=self.substrate,
        )


# --------------------------------------------------------------------------
# Demonstrations (as compiled steps)
# --------------------------------------------------------------------------


def _anchor(control: str, **extra) -> Anchor:
    path, region, point, _s = CONTROLS[control]
    return Anchor(template=path, region=region, click_point=point, **extra)


def _click(sid, control, intent, **kw) -> Step:
    anchor_extra = kw.pop("anchor_extra", {})
    return Step(
        id=sid,
        intent=intent,
        action=ActionKind.CLICK,
        anchor=_anchor(control, **anchor_extra),
        **kw,
    )


def _type(sid, label, value) -> Step:
    return Step(
        id=sid,
        intent=f"type '{value}'",
        action=ActionKind.TYPE,
        text=value,
        field_label=label,
    )


def _row(i: int, name: str, note: str, *, back: bool, write_check=True) -> list[Step]:
    p = f"r{i}"
    effects = (
        [
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"name": name},
                field="note",
                value=note,
                risk="irreversible",
            )
        ]
        if write_check
        else []
    )
    steps = [
        _click(
            f"{p}_search",
            "search",
            "click 'Search'",
            anchor_extra={"ocr_text": "Search"},
        ),
        _type(f"{p}_name", "Search", name),
        Step(id=f"{p}_enter", intent="press Enter", action=ActionKind.KEY, key="Enter"),
        _click(
            f"{p}_open",
            "row",
            f"click '{name}'",
            anchor_extra={
                "ocr_text": name,
                "context_text": f"Open chart for {name} {PATIENTS[name]}",
            },
        ),
        _click(f"{p}_note", "note", "click 'Note'", anchor_extra={"ocr_text": "Note"}),
        _type(f"{p}_type_note", "Note", note),
        _click(
            f"{p}_save",
            "save",
            "click 'Save'",
            anchor_extra={"ocr_text": "Save"},
            risk="irreversible",
            effects=effects,
        ),
    ]
    if back:
        steps.append(
            _click(
                f"{p}_back",
                "back",
                "click 'Back to list'",
                anchor_extra={"ocr_text": "Back to list"},
            )
        )
    return steps


def _demo(rows: list[tuple[str, str]], **kw) -> Workflow:
    steps: list[Step] = []
    for i, (name, note) in enumerate(rows):
        steps += _row(i, name, note, back=i + 1 < len(rows), **kw)
    steps.append(
        _click(
            "done", "signout", "click 'Sign out'", anchor_extra={"ocr_text": "Sign out"}
        )
    )
    return Workflow(name="notes", steps=steps)


def _demos(**kw) -> list[Workflow]:
    return [
        _demo(
            [("Phil Crawford", "Seen, stable"), ("Alice Moreno", "Refill sent")], **kw
        ),
        _demo(
            [
                ("Bob Lindqvist", "Call back"),
                ("Carol Okafor", "Labs ordered"),
                ("Dan Whitaker", "No change"),
            ],
            **kw,
        ),
    ]


@pytest.fixture()
def bundle(tmp_path) -> Path:
    root = tmp_path / "bundle"
    (root / "templates").mkdir(parents=True)
    for name, (path, *_rest) in CONTROLS.items():
        (root / path).write_bytes(_template_bytes(name))
    return root


# --------------------------------------------------------------------------
# Shape: segmentation and the induced program
# --------------------------------------------------------------------------


def test_merge_keeps_steps_some_iterations_lack():
    positions = merge([["a", "b", "c"], ["a", "c"], ["a", "x", "c"]])
    assert [p.sig for p in positions] == ["a", "b", "x", "c"]
    assert positions[1].at == {0: 1}
    assert positions[3].at == {0: 2, 1: 1, 2: 2}


def test_segment_finds_a_last_iteration_without_the_return():
    sigs = ["s", "t", "o", "w", "b", "s", "t", "o", "w", "end"]
    seg = next(segment(sigs, 0))
    assert seg.chunks == [(0, 5), (5, 9)]
    assert [seg.positions[q].sig for q in seg.tail] == ["b"]


def test_search_open_act_return_induces_a_checked_loop():
    result = induce_program(_demos())
    assert result.certified, result.render()
    [cert] = result.loop_certificates
    assert cert.returns_to_list is True
    assert set(cert.row_columns) == {"search", "note"}
    assert cert.identity_columns == ["search_identity_1"]  # the date of birth
    assert any("by 'search'" in s for s in cert.selections)
    assert any("note" in w and "search" in w for w in cert.writes)
    assert "at most 1000" in cert.bound

    wf = result.workflow
    body = wf.subflows[wf.program.states[cert.location].loop.body]
    entry = body.states[body.entry]
    assert entry.kind is StateKind.BRANCH
    guard = entry.transitions[0].guard
    assert guard.kind is PredicateKind.NOT
    assert guard.operands[0].kind is PredicateKind.ANCHOR_RESOLVES
    steps = [st.step for st in body.states.values() if st.step is not None]
    [opener] = [s for s in steps if s.target_param]
    assert opener.target_param == "search"
    [save] = [s for s in steps if s.effects]
    eff = save.effects[0]
    assert eff.match["name"].param == "search"
    assert eff.value.param == "note"
    rows = wf.data_sources[cert.relation].rows
    assert [r["search"] for r in rows] == [
        "Bob Lindqvist",
        "Carol Okafor",
        "Dan Whitaker",
    ]
    assert rows[0]["search_identity_1"] == "1990-07-21"


def test_a_write_without_a_record_check_is_refused():
    result = induce_program(_demos(write_check=False))
    assert result.certified is False
    u = next(u for u in result.uncertainties if u.kind == "ambiguous_loop")
    assert "no record check" in u.detail


def test_a_record_check_on_the_demonstrated_row_is_refused():
    demos = _demos()
    for wf in demos:
        for step in wf.steps:
            for eff in step.effects:
                eff.match = {"name": "Phil Crawford"}  # always the first demo's patient
                eff.value = "constant"
    result = induce_program(demos)
    assert result.certified is False
    assert any("names no row value" in u.detail for u in result.uncertainties)


def test_a_click_no_row_value_names_is_refused():
    demos = _demos()
    for wf in demos:
        for step in wf.steps:
            if step.id.endswith("_open"):
                step.anchor.ocr_text = f"Row {step.id}"
                step.intent = f"click 'Row {step.id}'"
    result = induce_program(demos)
    assert result.certified is False


def test_a_band_without_the_value_is_refused():
    demos = _demos()
    for wf in demos:
        for step in wf.steps:
            if step.id.endswith("_open"):
                step.anchor.context_text = "Open chart"
    result = induce_program(demos)
    assert result.certified is False
    assert any(u.kind == "ambiguous_selection" for u in result.uncertainties)


# --------------------------------------------------------------------------
# Behavior: run rows the demonstrations never showed
# --------------------------------------------------------------------------


def _run(workflow, bundle, run_dir, rows):
    app = ChartApp()
    vision = ChartVision(app)
    relation = next(iter(workflow.data_sources))
    report = Replayer(
        app, vision=vision, effect_verifier=NoteStore(app), poll_interval_s=0.01
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir, worklists={relation: rows})
    return app, report


HELD_OUT = [
    {
        "search": "Susan Underwood",
        "search_identity_1": "1980-05-05",
        "note": "BP recheck",
    },
    {
        "search": "Robert Getting",
        "search_identity_1": "1961-09-09",
        "note": "Flu shot given",
    },
]


def test_held_out_rows_each_write_their_own_record(bundle, tmp_path):
    result = induce_program(_demos())
    app, report = _run(result.workflow, bundle, tmp_path / "run", HELD_OUT)
    errors = [r.error for r in report.results if r.error]
    assert report.success is True, errors
    assert app.notes["Susan Underwood"] == "BP recheck"
    assert app.notes["Robert Getting"] == "Flu shot given"
    untouched = {
        k: v
        for k, v in app.notes.items()
        if k not in ("Susan Underwood", "Robert Getting")
    }
    assert set(untouched.values()) == {""}
    opened = [
        r
        for r in report.results
        if r.identity is not None and r.identity.mode == "param"
    ]
    assert len(opened) == 2
    assert all(r.identity.status == "verified" for r in opened)
    assert report.model_calls == 0 if hasattr(report, "model_calls") else True


def test_a_row_whose_identity_differs_stops_before_its_record(bundle, tmp_path):
    rows = [dict(HELD_OUT[0], search_identity_1="1999-12-31")] + HELD_OUT[1:]
    result = induce_program(_demos())
    app, report = _run(result.workflow, bundle, tmp_path / "run", rows)
    assert report.success is False
    assert set(app.notes.values()) == {""}  # nothing written anywhere
    halted = next(r for r in report.results if r.error)
    assert halted.identity is not None and halted.identity.status == "mismatch"


def test_a_row_missing_its_identity_column_stops(bundle, tmp_path):
    rows = [{"search": "Susan Underwood", "note": "BP recheck"}]
    result = induce_program(_demos())
    app, report = _run(result.workflow, bundle, tmp_path / "run", rows)
    assert report.success is False
    assert app.notes["Susan Underwood"] == ""


# --------------------------------------------------------------------------
# Rows that differ: an optional field, a status-dependent write
# --------------------------------------------------------------------------


def _demos_with_optional_note() -> list[Workflow]:
    """Some rows get no note at all: the note steps are absent for them."""
    demos = _demos()
    for wf in demos:
        wf.steps = [
            s
            for s in wf.steps
            if not (s.id.startswith("r1_") and s.id.endswith(("_note", "_type_note")))
        ]
    return demos


def test_a_value_some_rows_lack_runs_only_when_the_row_has_it():
    result = induce_program(_demos_with_optional_note())
    assert result.certified, result.render()
    [cert] = result.loop_certificates
    assert any("note" in c for c in cert.conditional_steps)
    body = next(iter(result.workflow.subflows.values()))
    [typed] = [
        st.step
        for st in body.states.values()
        if st.step is not None and st.step.param == "note"
    ]
    assert typed.guard is not None and typed.guard.on_unmet == "skip"
    assert typed.guard.predicate.kind is PredicateKind.NOT
    assert typed.guard.predicate.operands[0].kind is PredicateKind.PARAM_EQUALS


def _demos_with_status_write() -> list[Workflow]:
    """Rows in status 'Pending' also get 'Sign off' (a second write)."""
    demos = _demos()
    for t, wf in enumerate(demos):
        out = []
        for s in wf.steps:
            out.append(s)
            if s.id.endswith("_save") and s.id.startswith("r0_"):
                name = next(x.text for x in wf.steps if x.id == "r0_name")
                out.append(
                    _click(
                        f"r0_signoff_{t}",
                        "save",
                        "click 'Sign off'",
                        anchor_extra={"ocr_text": "Sign off"},
                        risk="irreversible",
                        effects=[
                            Effect(
                                kind=EffectKind.FIELD_EQUALS,
                                match={"name": name},
                                field="status",
                                value="signed",
                                risk="irreversible",
                            )
                        ],
                    )
                )
        wf.steps = out
    return demos


def _screens(trace: int, step_id: str):
    # Before each row's save, the chart shows its status.
    return ["Status: Pending"] if step_id.startswith("r0_") else ["Status: Final"]


def test_a_write_some_rows_get_is_refused_with_a_proposed_screen_guard():
    result = induce_program(_demos_with_status_write(), screen_texts=_screens)
    assert result.certified is False
    u = next(u for u in result.uncertainties if u.kind == "unconfirmed_loop_branch")
    assert "Status: Pending" in u.detail
    assert any(
        p.kind == "guard" and "Status: Pending" in p.content for p in result.proposed
    )


def test_an_accepted_screen_guard_certifies_the_status_write():
    demos = _demos_with_status_write()
    refused = induce_program(demos, screen_texts=_screens)
    u = next(u for u in refused.uncertainties if u.kind == "unconfirmed_loop_branch")
    key = u.detail.split("as '")[1].split("'")[0]
    result = induce_program(
        _demos_with_status_write(),
        screen_texts=_screens,
        accepted_loop_guards={key: "reviewer"},
    )
    assert result.certified, result.render()
    body = next(iter(result.workflow.subflows.values()))
    [signoff] = [
        st.step
        for st in body.states.values()
        if st.step is not None and st.step.intent == "click 'Sign off'"
    ]
    assert signoff.guard.predicate.kind is PredicateKind.TEXT_PRESENT
    assert signoff.guard.predicate.text == "Status: Pending"


# --------------------------------------------------------------------------
# A list inside a form: several columns per row, the save after the loop
# --------------------------------------------------------------------------


def test_list_in_form_binds_every_column():
    def demo(lines):
        steps = [
            _click(
                "open",
                "note",
                "click 'New claim'",
                anchor_extra={"ocr_text": "New claim"},
            )
        ]
        for i, (code, qty) in enumerate(lines):
            steps += [
                _click(
                    f"l{i}_add",
                    "row",
                    "click 'Add line'",
                    anchor_extra={"ocr_text": "Add line"},
                ),
                _type(f"l{i}_code", "Code", code),
                _type(f"l{i}_qty", "Quantity", qty),
            ]
        steps.append(
            _click("save", "save", "click 'Save'", anchor_extra={"ocr_text": "Save"})
        )
        return Workflow(name="claim", steps=steps)

    result = induce_program(
        [
            demo([("A01", "1"), ("B22", "3")]),
            demo([("C3", "2"), ("D4", "5"), ("E5", "1")]),
        ]
    )
    assert result.certified, result.render()
    [cert] = result.loop_certificates
    assert set(cert.row_columns) == {"code", "quantity"}
    rows = result.workflow.data_sources[cert.relation].rows
    assert rows == [
        {"code": "C3", "quantity": "2"},
        {"code": "D4", "quantity": "5"},
        {"code": "E5", "quantity": "1"},
    ]


def test_one_demonstration_of_several_rows_is_enough(bundle, tmp_path):
    demo = _demo(
        [
            ("Bob Lindqvist", "Call back"),
            ("Carol Okafor", "Labs ordered"),
            ("Dan Whitaker", "No change"),
        ]
    )
    result = induce_program([demo])
    assert result.certified, result.render()
    app, report = _run(result.workflow, bundle, tmp_path / "run", HELD_OUT)
    assert report.success is True, [r.error for r in report.results if r.error]
    assert app.notes["Susan Underwood"] == "BP recheck"


def test_hashed_identity_still_verifies_each_row(bundle, tmp_path):
    """Induction from recordings hashes the bands after naming the columns."""
    from openadapt_flow.compiler.induction import phi_free_program_identity

    result = induce_program(_demos())
    workflow = result.workflow
    phi_free_program_identity(workflow)
    body = next(iter(workflow.subflows.values()))
    [opener] = [
        st.step for st in body.states.values() if st.step and st.step.target_param
    ]
    tmpl = opener.anchor.identity_template
    assert opener.anchor.context_text is None
    assert {"search", "search_identity_1"} <= set(tmpl.param_token_indices)
    app, report = _run(workflow, bundle, tmp_path / "run", HELD_OUT)
    assert report.success is True, [r.error for r in report.results if r.error]
    assert app.notes["Robert Getting"] == "Flu shot given"

    wrong = [dict(HELD_OUT[0], search_identity_1="1999-12-31")]
    app, report = _run(workflow, bundle, tmp_path / "run2", wrong)
    assert report.success is False
    assert set(app.notes.values()) == {""}


def test_a_row_without_a_note_skips_the_note(bundle, tmp_path):
    result = induce_program(_demos_with_optional_note())
    rows = [
        {"search": "Susan Underwood", "search_identity_1": "1980-05-05", "note": ""},
        {
            "search": "Robert Getting",
            "search_identity_1": "1961-09-09",
            "note": "Flu shot given",
        },
    ]
    app, report = _run(result.workflow, bundle, tmp_path / "run", rows)
    assert report.success is True, [r.error for r in report.results if r.error]
    assert app.notes["Robert Getting"] == "Flu shot given"
    assert app.notes["Susan Underwood"] == ""
    typed = [a for a in app.actions if a[0] == "type"]
    assert ("type", "") not in typed
