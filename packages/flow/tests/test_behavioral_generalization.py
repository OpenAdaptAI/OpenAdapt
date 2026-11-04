"""Behavioral held-out score: classes, summary, and execution through the replayer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.validation import behavioral_generalization as bg


@pytest.mark.parametrize(
    ("engine", "independent", "expected"),
    [
        ("VERIFIED", "VERIFIED", "generalized"),
        ("TransactionOutcome.VERIFIED", "VERIFIED", "generalized"),
        ("VERIFIED", "WRONG", "silent_wrong"),
        ("VERIFIED", "MISSING", "silent_wrong"),
        ("HALTED_BEFORE_EFFECT", "MISSING", "halted_safely"),
        ("RECONCILIATION_REQUIRED", "MISSING", "halted_safely"),
        ("RECONCILIATION_REQUIRED", "WRONG", "wrong_effect"),
        ("HALTED_BEFORE_EFFECT", "VERIFIED", "unconfirmed_write"),
        ("ERROR", "MISSING", "error"),
        ("ERROR", "WRONG", "wrong_effect"),
        ("VERIFIED", None, "generalized"),
        ("HALTED_BEFORE_EFFECT", None, "halted_safely"),
    ],
)
def test_classify_case(engine, independent, expected):
    assert bg.classify_case(engine, independent) == expected


def _case(cls, independent="VERIFIED"):
    return bg.HeldOutCaseResult(
        inputs_sha256="0" * 64,
        engine_outcome="X",
        independent=independent,
        case_class=cls,
    )


def test_summary_counts_and_generalizes():
    good = bg.summarize([_case("generalized")] * 3)
    assert good.score == 1.0 and good.generalizes and good.independent
    assert "3/3" in good.render() and "independent check" in good.render()

    mixed = bg.summarize([_case("generalized"), _case("silent_wrong", "WRONG")])
    assert mixed.score == 0.5 and not mixed.generalizes and mixed.silent_wrong == 1

    weak = bg.summarize([_case("generalized", None)])
    assert weak.generalizes and not weak.independent
    assert "engine read-back only" in weak.render()

    assert not bg.summarize([]).generalizes


def test_an_unprofiled_run_counts_when_every_read_back_confirmed():
    def report(success, verdicts):
        return SimpleNamespace(
            success=success,
            results=[
                SimpleNamespace(
                    effect_evidence=[SimpleNamespace(final_verdict=v) for v in verdicts]
                )
            ],
        )

    assert bg.readback_confirmed(report(True, ["confirmed", "confirmed"]))
    assert not bg.readback_confirmed(report(True, ["confirmed", "refuted"]))
    assert not bg.readback_confirmed(report(True, []))  # nothing was read back
    assert not bg.readback_confirmed(report(False, ["confirmed"]))
    assert (
        bg.classify_case("COMPLETED_UNVERIFIED", "VERIFIED", readback_confirmed=True)
        == "generalized"
    )
    assert bg.classify_case("COMPLETED_UNVERIFIED", "VERIFIED") == "unconfirmed_write"


def test_inputs_are_digested_not_stored():
    a = bg.inputs_digest({"note": "x", "n": 1})
    assert a == bg.inputs_digest({"n": "1", "note": "x"}) and len(a) == 64


def test_score_held_out_executes_each_item(monkeypatch, tmp_path):
    """Every item runs through the replayer with its own params and verifier."""

    store: list[dict] = []
    ran: list[dict] = []

    class FakeReplayer:
        def __init__(self, backend, *, effect_verifier, **kw):
            self.verifier = effect_verifier

        def run(self, workflow, *, params, bundle_dir, run_dir):
            ran.append(dict(params))
            # The program writes the note; "bad" writes to the wrong patient.
            pid = "p2" if params["note"] == "bad" else "p1"
            store.append({"note": params["note"], "patient_id": pid})
            ok = self.verifier == params["note"]
            return SimpleNamespace(
                transaction_outcome="VERIFIED" if ok else "HALTED_BEFORE_EFFECT",
                execution_outcome=None,
                results=[SimpleNamespace(step_id="s1", ok=ok)],
                model_calls=0,
            )

    monkeypatch.setattr("openadapt_flow.runtime.Replayer", FakeReplayer)
    monkeypatch.setattr(
        "openadapt_flow.ir.Workflow.load",
        classmethod(
            lambda cls, path: SimpleNamespace(
                model_copy=lambda deep: None, data_sources={}
            )
        ),
    )
    closed = []

    def open_session():
        return object(), lambda: closed.append(1)

    def independent(item, before, after):
        new = after[len(before) :]
        mine = [r for r in new if r["note"] == item["note"]]
        if not mine:
            return "MISSING"
        return "VERIFIED" if mine[0]["patient_id"] == "p1" else "WRONG"

    result = bg.score_held_out(
        tmp_path,
        [{"note": "a"}, {"note": "b"}, {"note": "bad"}],
        open_session=open_session,
        make_verifier=lambda params: params["note"],
        run_root=tmp_path / "runs",
        independent=independent,
        snapshot=lambda: list(store),
    )
    assert [p["note"] for p in ran] == ["a", "b", "bad"]
    assert len(closed) == 3
    assert result.generalized == 2 and result.silent_wrong == 1
    assert not result.generalizes and result.independent


def test_verify_yaml_supplies_session_reset_and_record_check(monkeypatch, tmp_path):
    """The helper wires verify.yaml's session, reset and verifier into the run."""

    calls: dict = {}

    def fake_score(bundle_dir, items, **kw):
        calls.update(kw)
        return bg.summarize([])

    monkeypatch.setattr(bg, "score_held_out", fake_score)
    monkeypatch.setattr(
        "openadapt_flow.qualify.config.load_verify_config", lambda path: "cfg"
    )
    monkeypatch.setattr(
        "openadapt_flow.qualify.session.session_opener", lambda c: ("open", c)
    )
    monkeypatch.setattr(
        "openadapt_flow.qualify.session.verifier_factory", lambda c: ("verify", c)
    )
    monkeypatch.setattr(
        "openadapt_flow.qualify.session.reset_runner", lambda c: ("reset", c)
    )
    bg.score_held_out_with_verify(
        tmp_path,
        [{"note": "a"}],
        tmp_path / "verify.yaml",
        run_root=tmp_path,
        reset_each=True,
    )
    assert calls["open_session"] == ("open", "cfg")
    assert calls["make_verifier"] == ("verify", "cfg")
    assert calls["reset"] == ("reset", "cfg") and calls["reset_each"] is True


def test_independent_needs_snapshot(tmp_path):
    with pytest.raises(ValueError):
        bg.score_held_out(
            tmp_path,
            [],
            open_session=lambda: None,
            make_verifier=lambda p: None,
            run_root=tmp_path,
            independent=lambda *a: "VERIFIED",
        )


def test_structural_score_stays_labelled_structural():
    from openadapt_flow.compiler.induction import HeldOutValidation

    text = HeldOutValidation(per_trace=[1.0], mean=1.0, n_traces=2).render()
    assert "STRUCTURAL" in text and "NOT behavioral" in text


def test_a_failing_check_is_an_error_item_not_the_end(monkeypatch, tmp_path):
    """snapshot() or independent() raising marks that item, scoring goes on."""

    class FakeReplayer:
        def __init__(self, backend, *, effect_verifier, **kw):
            pass

        def run(self, workflow, *, params, bundle_dir, run_dir):
            return SimpleNamespace(
                transaction_outcome="VERIFIED",
                execution_outcome=None,
                results=[SimpleNamespace(step_id="s1", ok=True)],
                model_calls=0,
            )

    monkeypatch.setattr("openadapt_flow.runtime.Replayer", FakeReplayer)
    monkeypatch.setattr(
        "openadapt_flow.ir.Workflow.load",
        classmethod(
            lambda cls, path: SimpleNamespace(
                model_copy=lambda deep: None, data_sources={}
            )
        ),
    )
    snaps = iter([1, RuntimeError("store down"), 2, 3, 4, 5])

    def snapshot():
        value = next(snaps)
        if isinstance(value, Exception):
            raise value
        return value

    def independent(item, before, after):
        if item["note"] == "c":
            raise ValueError("unreadable")
        return "VERIFIED"

    result = bg.score_held_out(
        tmp_path,
        [{"note": "a"}, {"note": "b"}, {"note": "c"}],
        open_session=lambda: (object(), lambda: None),
        make_verifier=lambda params: None,
        run_root=tmp_path / "runs",
        independent=independent,
        snapshot=snapshot,
    )
    classes = [r.case_class for r in result.per_item]
    assert classes == ["error", "generalized", "error"]
    assert "store down" in result.per_item[0].detail["snapshot_error"]
    assert "unreadable" in result.per_item[2].detail["independent_error"]
    assert result.errors == 2 and not result.generalizes


# --------------------------------------------------------------------------
# End to end: an induced loop program on an in-memory chart application
# --------------------------------------------------------------------------


def test_held_out_rows_on_an_in_memory_app(tmp_path):
    """Induce a worklist loop, then score rows it never saw against the app.

    The application keeps its records (one note per patient) across items, as
    a system of record does. The independent check reads those records, not
    the engine's report. A row whose identity does not match the patient on
    screen must stop before writing.
    """

    from openadapt_flow.compiler.induction import induce_program
    from tests.test_loop_shapes import (
        CONTROLS,
        HELD_OUT,
        ChartApp,
        ChartVision,
        NoteStore,
        _demos,
        _template_bytes,
    )

    result = induce_program(_demos())
    assert result.certified, result.render()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    for name, (path, *_rest) in CONTROLS.items():
        (bundle / path).write_bytes(_template_bytes(name))
    result.workflow.save(bundle)

    app = ChartApp()

    def open_session():
        app.screen, app.results, app.open_patient, app.focus = "list", [], None, None
        return app, lambda: None

    def independent(item, before, after):
        changed = {k for k in after if after[k] != before[k]}
        if not changed:
            return "MISSING"
        name = item["search"]
        return (
            "VERIFIED" if changed == {name} and after[name] == item["note"] else "WRONG"
        )

    wrong_identity = dict(
        search="Carol Okafor", search_identity_1="1999-12-31", note="Should not land"
    )
    score = bg.score_held_out(
        bundle,
        [*HELD_OUT, wrong_identity],
        open_session=open_session,
        make_verifier=lambda params: NoteStore(app),
        run_root=tmp_path / "runs",
        independent=independent,
        snapshot=lambda: dict(app.notes),
        replayer_kwargs={"vision": ChartVision(app), "poll_interval_s": 0.01},
    )
    classes = [r.case_class for r in score.per_item]
    assert classes == ["generalized", "generalized", "halted_safely"], score.per_item
    assert app.notes["Susan Underwood"] == "BP recheck"
    assert app.notes["Robert Getting"] == "Flu shot given"
    assert app.notes["Carol Okafor"] == ""
    assert score.independent and score.silent_wrong == 0 and score.wrong_effect == 0
    assert not score.generalizes  # one held-out row stopped
