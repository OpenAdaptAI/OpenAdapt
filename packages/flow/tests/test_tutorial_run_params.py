"""A tutorial run can take its own note, and its record check reads that note.

The launcher's quickstart runs the compiled tutorial five times with a new
note each time. The bundle on disk never changes: the note reaches the run
through ``run_tutorial_workflow(params=...)``, the run authorization binds it,
and a record check compiled with ``bind_effect_params`` compares the saved
record with this run's note instead of the demonstrated one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import tutorial
from openadapt_flow.backends._browser_process import shared_browser
from openadapt_flow.compiler import compile_recording
from openadapt_flow.mockmed.fault_server import serve


@pytest.mark.timeout(600)
def test_a_run_with_a_new_note_is_checked_against_that_note(tmp_path: Path) -> None:
    base_url, _db, stop = serve()
    try:
        with shared_browser(headless=True):
            tutorial.record_tutorial(base_url, tmp_path / "recording")
            bundle_dir = tmp_path / "bundle"
            workflow = compile_recording(
                tmp_path / "recording",
                bundle_dir,
                name=tutorial.TUTORIAL_WORKFLOW_NAME,
                mine_effects=True,
                bind_effect_params=True,
            )
            tutorial.certify_tutorial(workflow)
            digest = (bundle_dir / "workflow.json").read_bytes()

            note = "Recheck blood pressure next month"
            report = tutorial.run_tutorial_workflow(
                base_url=base_url,
                workflow=workflow,
                bundle_dir=bundle_dir,
                run_dir=tmp_path / "run",
                params={"note": note},
            )
            records = tutorial._records(base_url)
    finally:
        stop()

    assert report.execution_outcome == "VERIFIED"
    assert report.params["note"] == note
    assert report.outcome_envelope.passed_contracts.effect == 2
    assert [record["note"] for record in records] == [note]
    # The program itself is unchanged; only this run's input differs.
    assert (bundle_dir / "workflow.json").read_bytes() == digest
    assert workflow.params["note"] == tutorial.TUTORIAL_NOTE
