"""Code as action as the default first-run agent, with a read-back on every save.

On MockMed, in a real browser, with no model calls:

* compile-agent's run 1 reads each save back from the system of record and
  tells the agent what landed; the read-backs are kept with run 1;
* a save that lands on another patient (``?fault=wrong_record``) stops run 1
  at the read-back, before the agent goes on; nothing can be admitted;
* a save the server drops while the screen says "saved"
  (``?fault=optimistic``) is reported to the agent as "nothing was saved",
  and run 1 fails its record check;
* the MCP teach path's ``teach_run`` does the task from scripts and drafts a
  program; with a person approving saves, the script stops before the save,
  and the approval (bound to that line and that moment) lets exactly that
  line through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from openadapt_flow.compile_agent import FirstRunCompiler, ScriptedCodeAgent
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.tutorial import _http_json, _records

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
SCRIPTS = [
    'click role=button name=Open near="Jane Sample"\ntext grep=MRN',
    'click name="New Encounter"\nclick name=Triage\nfill name=Note value="{note}"\n'
    'click name="Save Encounter"',
    "text grep=saved",
]


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _reset(base: str):
    return lambda: _http_json(f"{base}/api/reset", method="POST", body={})


def _verify_yaml(tmp_path: Path, base: str) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )
    return path


def _compiler(tmp_path: Path, base: str, fault: str = "ok") -> FirstRunCompiler:
    return FirstRunCompiler(
        PROMPT,
        params=["note"],
        url=f"{base}/?fault={fault}&idempotency=demo#tasks",
        verify=_verify_yaml(tmp_path, base),
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "MRN P1"],
        task_id=f"mockmed.code_default.{fault}",
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
        reset=_reset(base),
        on_warning=lambda text: None,
    )


def test_every_save_in_run_1_is_read_back(mockmed: str, tmp_path: Path) -> None:
    agent = ScriptedCodeAgent(SCRIPTS)
    result = _compiler(tmp_path, mockmed).record(agent, {"note": NOTE})
    assert result.status == "draft", result.detail
    assert result.run1["readbacks"] == [
        {
            "control": "Save Encounter",
            "outcome": "landed",
            "detail": "1 record saved for patient_id=p1, type=Triage, with every input",
            "fields": ["id", "key", "note", "patient_id", "source", "type"],
        }
    ]
    assert result.run1["harness"]["readbacks"] == 1
    assert "read back: 1 record saved for patient_id=p1" in agent.outputs[1]
    assert result.run1["code_action"]["readbacks"] == {"landed": 1}


def test_a_save_on_another_patient_stops_run_1_at_the_read_back(
    mockmed: str, tmp_path: Path
) -> None:
    agent = ScriptedCodeAgent(SCRIPTS)
    compiler = _compiler(tmp_path, mockmed, fault="wrong_record")
    result = compiler.record(agent, {"note": NOTE})
    assert result.status == "readback_wrong_record", result.detail
    assert "the task does not name" in (result.detail or "")
    assert result.run1["readbacks"][0]["outcome"] == "wrong_record"
    # The gate saw the right patient on screen; the read-back caught the save.
    assert result.run1["gates"][0]["decision"] == "passed_no_hold"
    # The agent never got to its next script.
    assert len(agent.outputs) == 1
    assert compiler._jit().candidate() is None


def test_a_dropped_save_is_reported_as_not_saved(mockmed: str, tmp_path: Path) -> None:
    agent = ScriptedCodeAgent(SCRIPTS[:2])
    result = _compiler(tmp_path, mockmed, fault="optimistic").record(
        agent, {"note": NOTE}
    )
    assert result.run1["readbacks"][0]["outcome"] == "unconfirmed"
    assert "unconfirmed" in agent.outputs[1]
    assert "Do not save again" in agent.outputs[1]
    # Flow's own check after run 1 refuses it too: nothing landed.
    assert result.status == "readback_refused", result.detail
    assert _records(mockmed) == []


# -- the MCP teach path ----------------------------------------------------------


def _teach(tmp_path: Path, base: str, *, hold: bool) -> object:
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig

    return TeachBridge(
        TeachConfig(
            verify=_verify_yaml(tmp_path, base),
            cache_dir=tmp_path / "teach",
            url=f"{base}/?fault=ok&idempotency=demo#tasks",
            key_dir=tmp_path / "keys",
            hold=hold,
            reset=_reset(base),
        )
    )


def _start(teach: object) -> dict:
    return teach.dispatch(  # type: ignore[attr-defined]
        "teach_start",
        {
            "task": PROMPT,
            "inputs": {"note": NOTE},
            "record_fields": {"patient_id": "p1", "type": "Triage"},
            "on_screen": ["Jane Sample", "MRN P1"],
            "name": "add_note",
        },
    )


def test_teach_run_is_offered_and_drafts_a_program(
    mockmed: str, tmp_path: Path
) -> None:
    teach = _teach(tmp_path, mockmed, hold=False)
    try:
        names = [spec.name for spec in teach.list_tool_specs()]  # type: ignore[attr-defined]
        assert names.index("teach_run") < names.index("teach_act")
        started = _start(teach)
        assert started["started"] and "teach_run" in started["next"]
        for script in SCRIPTS[:2]:
            out = teach.dispatch(  # type: ignore[attr-defined]
                "teach_run", {"script": script.replace("{note}", NOTE)}
            )
            assert out["status"] == "ok", out
        assert "read back: 1 record saved" in out["output"]
        report = teach.dispatch("teach_finish", {})  # type: ignore[attr-defined]
        assert report["status"] == "draft", report
    finally:
        teach.close()  # type: ignore[attr-defined]


def test_teach_run_stops_before_a_save_a_person_must_approve(
    mockmed: str, tmp_path: Path
) -> None:
    teach = _teach(tmp_path, mockmed, hold=True)
    try:
        _start(teach)
        out = teach.dispatch("teach_run", {"script": SCRIPTS[0]})  # type: ignore[attr-defined]
        assert out["status"] == "ok", out
        out = teach.dispatch(  # type: ignore[attr-defined]
            "teach_run", {"script": SCRIPTS[1].replace("{note}", NOTE)}
        )
        assert out["status"] == "needs_person", out
        assert out["script"] == 'click name="Save Encounter"'
        assert _records(mockmed) == []
        # The MCP layer previews the first line, a person approves it in the
        # client's form, and the approval is bound to it.
        hold = teach.hold_needed({"script": out["script"]}, tool="teach_run")  # type: ignore[attr-defined]
        assert hold and hold["commit"] == "Save Encounter"
        stale = teach.dispatch(  # type: ignore[attr-defined]
            "teach_run",
            {"script": out["script"]},
            held_by_person=hold["binding"].replace('"click', '"press'),
        )
        assert stale["status"] == "error" and _records(mockmed) == []
        done = teach.dispatch(  # type: ignore[attr-defined]
            "teach_run", {"script": out["script"]}, held_by_person=hold["binding"]
        )
        assert done["status"] == "ok", done
        assert "read back: 1 record saved" in done["output"]
        assert [(r["patient_id"], r["note"]) for r in _records(mockmed)] == [
            ("p1", NOTE)
        ]
        report = teach.dispatch("teach_finish", {})  # type: ignore[attr-defined]
        assert report["status"] == "draft", report
    finally:
        teach.close()  # type: ignore[attr-defined]
