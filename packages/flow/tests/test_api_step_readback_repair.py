"""The API step with pipelined read-back and with self-repair.

- Pipelined read-back (docs/THROUGHPUT.md) defers an API save's record check
  the same way it defers a GUI save's: same check, later timing, and the
  item's report stays unsuccessful until it settles. A durable run verifies
  inline and says why.
- Self-repair's probes and its resumed run of a repaired bundle run the GUI
  steps: the repair and the person's approval are about the GUI step, so an
  API step there would save without exercising the repair.

The system of record is the MockMed fault server; the values are made up.
"""

from __future__ import annotations

import json

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.repair.self_repair import (
    REPAIR_GUI_ONLY,
    ReplayTarget,
    keep_gui_for_repair,
)
from openadapt_flow.runtime.actuators import ActuationStatus
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from openadapt_flow.runtime.replayer import Replayer
from tests.test_api_tier_web_form import (
    EnterWritesBackend,
    ScriptedActuator,
    _span_workflow,
)
from tests.test_execution_profiles import _sealed
from tests.test_replayer import FakeVision


def _run(tmp_path, *, pipeline, durable=False, verifier_url=None):
    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        workflow, bundle = _sealed(tmp_path, _span_workflow(), encrypted=False)
        actuator = ScriptedActuator(url, ActuationStatus.ACTUATED)
        report = Replayer(
            EnterWritesBackend(url),
            vision=FakeVision(),
            effect_verifier=RestRecordVerifier(verifier_url or url),
            web_form_actuator=actuator,
            poll_interval_s=0.01,
            readback_pipeline=pipeline,
            durable=durable,
        ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
        handoff = pipeline.take()
        settled = handoff.settled.result(timeout=30) if handoff is not None else None
        return report, settled, actuator, db.snapshot()["records"]
    finally:
        stop()


def test_an_api_save_reads_back_while_the_next_item_runs(tmp_path):
    pipeline = ReadbackPipeline()
    try:
        report, settled, actuator, records = _run(tmp_path, pipeline=pipeline)
    finally:
        pipeline.close()

    assert actuator.calls == 1 and [r["note"] for r in records] == ["api"]
    assert settled is report and report.success is True
    save = report.results[-1]
    assert save.actuation == "api" and save.effect_verified is True
    assert not any(line.startswith("[pipelined]") for line in save.effect_results)
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert saved["success"] is True


def test_a_durable_api_save_reads_back_inline_and_says_why(tmp_path):
    pipeline = ReadbackPipeline()
    try:
        report, settled, actuator, records = _run(
            tmp_path, pipeline=pipeline, durable=True
        )
    finally:
        pipeline.close()

    assert settled is None and report.success is True
    assert report.results[-1].effect_verified is True
    assert pipeline.inline_reason == "durable runs verify inline"


def test_self_repair_runs_a_repaired_bundle_on_the_gui(tmp_path):
    replayer = Replayer(EnterWritesBackend("http://127.0.0.1:9"), vision=FakeVision())
    keep_gui_for_repair(replayer)
    span = replayer._web_form_span_at(_span_workflow(), 0, durable=True)
    assert span is not None and span[2] == REPAIR_GUI_ONLY


class _Session:
    def __init__(self, backend) -> None:
        self.backend = backend


def test_a_probe_or_resumed_run_never_sends_the_api_step(tmp_path):
    from openadapt_flow.approval_mode.gate import ApprovalGate

    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        workflow, bundle = _sealed(tmp_path, _span_workflow(), encrypted=False)
        actuator = ScriptedActuator(url, ActuationStatus.ACTUATED)
        backend = EnterWritesBackend(url)
        target = ReplayTarget(
            lambda _fault: _Session(backend),
            effect_verifier=RestRecordVerifier(url),
            policy="permissive",
            profile="demo",
            replayer_kwargs={
                "vision": FakeVision(),
                "web_form_actuator": actuator,
                "poll_interval_s": 0.01,
            },
        )
        report = target.execute(
            workflow,
            bundle,
            _Session(backend),
            tmp_path / "probe",
            approval_gate=ApprovalGate("not_gated"),
        )
    finally:
        stop()

    assert actuator.calls == 0
    assert report.results[0].api_fallback == REPAIR_GUI_ONLY
