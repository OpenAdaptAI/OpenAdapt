"""The trace export: full steps, identity at each write, and the read-back."""

from __future__ import annotations

import json

import pytest

from openadapt_flow import privacy
from openadapt_flow.integrations.harness2 import traces
from openadapt_flow.integrations.harness2.agent import RULE_IDENTITY, ScriptedGuiAgent
from openadapt_flow.integrations.harness2.apps import MockMedTarget
from openadapt_flow.integrations.harness2.rollout import run_rollout
from openadapt_flow.integrations.harness2.tasks import mockmed_tasks


@pytest.fixture(autouse=True)
def _plaintext(monkeypatch):
    # Local writes stay plaintext unless a test installs a scrubber.
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")


@pytest.fixture(scope="module")
def rollouts(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    root = tmp_path_factory.mktemp("rollouts")
    out = {}
    for variant, harness in (
        ("lookalike", ""),
        ("plain", f"Check the {RULE_IDENTITY}."),
    ):
        task = mockmed_tasks((variant,))[0]
        out[variant] = (
            run_rollout(
                task,
                harness,
                ScriptedGuiAgent(),
                root / variant,
                target=MockMedTarget(),
                run_id=f"trace-{variant}",
            ),
            task,
        )
    return out


def test_wrong_patient_write_is_a_mismatch_with_its_readback(rollouts):
    run_dir, task = rollouts["lookalike"]
    trace = traces.from_rollout(run_dir, task, candidate="base1")
    writes = [s for s in trace.steps if s.write]
    assert len(writes) == 1
    assert writes[0].identity.status == "mismatch"
    assert "Taylor Duplicate" in writes[0].identity.observed
    assert "Jane Sample" in writes[0].identity.expected
    assert trace.readback["wrong_record"] is True
    assert trace.readback["other_added"] == 1 and trace.reward == -1.0
    text = traces.render(trace)
    # Full, not summarised: every step's screen is there.
    assert text.count("Screen:\n```") == len(trace.steps)
    assert "Identity at this step: mismatch" in text
    assert "other patients gained 1" in text


def test_right_patient_write_is_verified(rollouts):
    run_dir, task = rollouts["plain"]
    trace = traces.from_rollout(run_dir, task)
    [write] = [s for s in trace.steps if s.write]
    assert write.identity.status == "verified"
    assert trace.outcome == "verified" and trace.passed is True


def test_identity_reads_an_ocr_banner():
    task = mockmed_tasks(("plain",))[0]
    # OCR on a pixel surface drops the spaces around the separators.
    assert (
        traces.screen_identity("JaneSample—MRNP1—DOB 1980-01-01", task).status
        == "verified"
    )
    wrong = traces.screen_identity("TaylorDuplicate—MRNP0—DOB 1982-12-12", task)
    assert wrong.status == "mismatch"
    assert traces.screen_identity("Referral Tasks", task).status == "unreadable"


def test_export_writes_the_filesystem(rollouts, tmp_path):
    items = [traces.from_rollout(d, t, candidate=v) for v, (d, t) in rollouts.items()]
    harness = tmp_path / "h"
    harness.mkdir()
    (harness / "AGENTS.md").write_text("# rules\n")
    out = traces.export(tmp_path / "out", items, harnesses={"plain": harness})
    rows = [json.loads(x) for x in (out / "index.jsonl").read_text().splitlines()]
    assert [r["candidate"] for r in rows] == ["plain", "lookalike"]  # best reward first
    assert rows[0]["schema"] == traces.SCHEMA
    assert rows[1]["identity_mismatches"] == 1
    assert "lookalike" in (out / "INDEX.md").read_text()
    for r in rows:
        d = out / "traces" / r["trace_id"]
        assert (d / "trace.md").is_file()
        assert json.loads((d / "trace.json").read_text())["trace_id"] == r["trace_id"]
    assert (
        out / "candidates" / "plain" / "harness" / "AGENTS.md"
    ).read_text() == "# rules\n"


def test_screen_text_is_scrubbed_but_the_verdict_survives(rollouts, monkeypatch):
    class Names:
        def scrub_text(self, text, is_separated=False):
            return text.replace("Taylor Duplicate", "<PERSON>").replace(
                "Jane Sample", "<PERSON>"
            )

    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    privacy.set_text_scrubber(Names())
    try:
        run_dir, task = rollouts["lookalike"]
        trace = traces.from_rollout(run_dir, task)
    finally:
        privacy.reset_scrubbers()
    text = traces.render(trace)
    assert "Taylor Duplicate" not in text and "Jane Sample" not in text
    [write] = [s for s in trace.steps if s.write]
    assert write.identity.status == "mismatch"


def test_flow_run_report_carries_runtime_identity(tmp_path):
    from openadapt_flow.ir import IdentityCheck, RunReport, StepResult

    report = RunReport(
        workflow_name="triage",
        started_at="2026-10-18T00:00:00Z",
        execution_outcome="HALTED",
        results=[
            StepResult(step_id="step_001", intent="Open the referral", ok=True),
            StepResult(
                step_id="step_005",
                intent="Click Save Encounter",
                ok=False,
                risk="irreversible",
                identity=IdentityCheck(
                    status="mismatch",
                    mode="structured",
                    expected="Jane Sample",
                    observed="Taylor Duplicate",
                ),
                error="identity mismatch: refused before the click",
            ),
        ],
    )
    (tmp_path / "report.json").write_text(report.model_dump_json())
    trace = traces.from_flow_run(tmp_path, task_id="mockmed/lookalike")
    assert trace.source == "flow_run" and trace.outcome == "HALTED"
    assert trace.passed is False
    save = trace.steps[1]
    assert save.write and save.identity.status == "mismatch"
    assert save.identity.mode == "structured"
    assert "refused before the click" in traces.render(trace)
