"""Pipelined read-back in the real Replayer: same check, later timing."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.compiler.loop_authoring import author_data_driven_loop
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from openadapt_flow.runtime.replayer import Replayer
from tests.test_loop_authoring import (
    _encounter_body,
    _RowWritingBackend,
    _vision_confirms_saved,
)
from tests.test_replayer import make_png


@pytest.fixture()
def dirs(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle, tmp_path / "run"


def _run(records, dirs, *, corrupt=None, pipeline=None, verifier_factory=None):
    bundle, run_dir = dirs
    looped = author_data_driven_loop(_encounter_body(), records)
    url, db, stop = fault_serve()
    backend = _RowWritingBackend(url, corrupt=corrupt)
    replayer = Replayer(
        backend,
        vision=_vision_confirms_saved(),
        effect_verifier=(verifier_factory or RestRecordVerifier)(url),
        poll_interval_s=0.01,
        readback_pipeline=pipeline,
    )
    try:
        report = replayer.run(looped, bundle_dir=bundle, run_dir=run_dir)
        handoff = pipeline.take() if pipeline is not None else None
        settled = handoff.settled.result(timeout=30) if handoff is not None else None
    finally:
        stop()
    return report, settled, backend, run_dir


def test_confirmed_readback_settles_the_saved_report(dirs) -> None:
    pipeline = ReadbackPipeline()
    report, settled, backend, run_dir = _run(
        [{"patient_id": "alice", "note": "alice triage"}], dirs, pipeline=pipeline
    )
    assert settled is report
    assert report.success is True
    save = [r for r in report.results if r.step_id == "save"][-1]
    assert save.effect_verified is True
    assert any("CONFIRMED" in line for line in save.effect_results)
    assert not any(line.startswith("[pipelined]") for line in save.effect_results)
    saved = json.loads((run_dir / "report.json").read_text())
    saved_save = [r for r in saved["results"] if r["step_id"] == "save"][-1]
    assert saved_save["effect_verified"] is True
    pipeline.close()


def test_refuted_readback_halts_the_item_after_the_fact(dirs) -> None:
    pipeline = ReadbackPipeline()
    report, settled, backend, run_dir = _run(
        [{"patient_id": "bob", "note": "bob triage"}],
        dirs,
        corrupt={"bob": "mallory"},
        pipeline=pipeline,
    )
    assert settled is report
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert report.halt is not None and "HALTED" in report.halt.reason
    save = [r for r in report.results if r.step_id == "save"][-1]
    assert save.effect_verified is False and save.ok is False
    saved = json.loads((run_dir / "report.json").read_text())
    assert saved["success"] is False
    pipeline.close()


def test_a_later_step_waits_for_the_pending_readback_and_halts(dirs) -> None:
    """Inside one run, the next record's first step joins the deferred check."""

    pipeline = ReadbackPipeline()
    report, settled, backend, _ = _run(
        [
            {"patient_id": "alice", "note": "alice triage"},
            {"patient_id": "bob", "note": "bob triage"},
            {"patient_id": "cara", "note": "cara triage"},
        ],
        dirs,
        corrupt={"bob": "mallory"},
        pipeline=pipeline,
    )
    # bob's wrong write was caught before cara's first keystroke.
    assert backend.posted == [("alice", "alice triage"), ("mallory", "bob triage")]
    assert report.success is False
    assert settled is None  # nothing left pending: the run settled inline
    saves = [r for r in report.results if r.step_id == "save"]
    assert [s.effect_verified for s in saves] == [True, False]
    # The refusal is cara's first step, refused on bob's deferred verdict.
    refused = [r for r in report.results if r.step_id == "type_patient" and not r.ok]
    assert len(refused) == 1 and "HALTED" in (refused[0].error or "")
    pipeline.close()


def test_without_a_pipeline_the_readback_stays_inline(dirs) -> None:
    report, settled, backend, _ = _run(
        [{"patient_id": "alice", "note": "alice triage"}], dirs
    )
    assert settled is None
    assert report.success is True
    save = [r for r in report.results if r.step_id == "save"][-1]
    assert save.effect_verified is True


def test_screen_bound_verifier_is_never_deferred(dirs) -> None:
    class ScreenVerifier(RestRecordVerifier):
        backend = object()  # reads the screen: needs the browser

    pipeline = ReadbackPipeline()
    report, settled, backend, _ = _run(
        [{"patient_id": "alice", "note": "alice triage"}],
        dirs,
        pipeline=pipeline,
        verifier_factory=ScreenVerifier,
    )
    assert settled is None
    assert pipeline.inline_reason == "the read-back uses the screen"
    assert report.success is True
    pipeline.close()
