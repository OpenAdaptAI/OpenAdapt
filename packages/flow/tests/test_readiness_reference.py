"""Reference episodes from a person's recorded tasks. Synthetic data only."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.readiness.reference import (
    REFERENCE_SCHEMA,
    import_episodes,
    load_episodes,
)

APP = "http://127.0.0.1:9811"
RECORD = {"patient_id": "p1", "name": "Jane Sample"}


def _task(folder: Path, task_id: str = "ref-task-0001", **extra: Any) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "openadapt.readiness-task/v1",
        "task_id": task_id,
        "instruction": "Add the triage note.",
        "inputs": {"note": "Cough, 3 days"},
        "expected_record": RECORD,
        **extra,
    }
    (folder / "task.json").write_text(json.dumps(doc))
    return folder


def _capture(folder: Path, lines: list[dict[str, Any]], events: Optional[list[float]] = None):
    rec = folder / "recording"
    rec.mkdir(parents=True, exist_ok=True)
    rows = [{"schema": "openadapt.network-capture/1", "exchanges": len(lines)}]
    rows += [{"seq": i, "resource_type": "fetch", "frame_url": None, **line}
             for i, line in enumerate(lines)]
    (rec / "network.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    if events is not None:
        (rec / "events.jsonl").write_text(
            "\n".join(json.dumps({"t": t, "kind": "click"}) for t in events) + "\n"
        )


def _save(t: float = 42.5, status: int = 200, **kw: Any) -> dict[str, Any]:
    return {
        "t": t,
        "method": "POST",
        "url": APP + "/api/encounter?fault=ok",
        "status": status,
        "request_type": "application/json",
        "body": json.dumps({"patient_id": "p1", "type": "Triage", "note": "Cough, 3 days",
                            "csrf_token": "<session:csrf_token>"}),
        **kw,
    }


def test_import_from_network_capture(tmp_path: Path) -> None:
    folder = _task(tmp_path / "src" / "a")
    _capture(folder, [{"t": 0.1, "method": "GET", "url": APP + "/", "status": 200}, _save()],
             events=[2.5, 10.0, 40.0])
    result = import_episodes(tmp_path / "src", tmp_path / "eps", app_url=APP + "/")
    assert result.imported == ["ref-task-0001"] and result.refused == []
    out = tmp_path / "eps" / "ref-task-0001"
    ep = json.loads((out / "reference_episode.json").read_text())
    assert ep["schema"] == REFERENCE_SCHEMA
    assert ep["human_source"] == "network_capture"
    assert ep["human_fields"] == [
        {"name": "patient_id", "value": "p1"},
        {"name": "type", "value": "Triage"},
        {"name": "note", "value": "Cough, 3 days"},
    ]
    assert ep["human_duration_s"] == 40.0
    assert ep["save"] == {"method": "POST", "url_path": "/api/encounter"}
    assert ep["withheld_fields"] == 1
    assert ep["inputs_digest"].startswith("sha256:")
    for name in ("reference_episode.json", "inputs.json", "guard_task.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    guard_task = json.loads((out / "guard_task.json").read_text())
    assert guard_task["schema_version"] == "openadapt.guard-task/v1"
    assert guard_task["intended_fields"] == {"type": None, "note": None}
    from openadapt_flow.guard import load_task

    assert load_task(out / "guard_task.json").expected_record == RECORD
    assert [doc["task_id"] for _p, doc in load_episodes(tmp_path / "eps")] == ["ref-task-0001"]


@pytest.mark.parametrize(
    ("lines", "extra", "code"),
    [
        ([{"t": 1, "method": "GET", "url": APP + "/", "status": 200}], {}, "no_save_in_capture"),
        ([_save(status=500)], {}, "save_not_confirmed"),
        ([_save(failed=True, status=None)], {}, "save_not_confirmed"),
        ([_save(body=None, body_omitted="multipart")], {}, "save_unreadable"),
        ([_save(request_truncated=True)], {}, "save_unreadable"),
        ([{**_save(), "url": APP + "/api/lock", "t": 1}, _save()], {}, "ambiguous_save"),
    ],
)
def test_refusals(tmp_path: Path, lines: list, extra: dict, code: str) -> None:
    folder = _task(tmp_path / "src" / "a", **extra)
    _capture(folder, lines)
    result = import_episodes(tmp_path / "src", tmp_path / "eps", app_url=APP)
    assert result.imported == []
    assert [r["code"] for r in result.refused] == [code]
    assert not (tmp_path / "eps" / "ref-task-0001").exists()


def test_named_save_resolves_ambiguity_and_skips_not_saves(tmp_path: Path) -> None:
    folder = _task(
        tmp_path / "src" / "a",
        save={"method": "POST", "path": "/api/encounter"},
        not_saves=[{"method": "POST", "path": "/login"}],
    )
    _capture(folder, [
        {**_save(), "url": APP + "/login", "t": 0.5},
        {**_save(), "url": APP + "/api/lock", "t": 1},
        _save(),
    ])
    assert import_episodes(tmp_path / "src", tmp_path / "eps").imported == ["ref-task-0001"]


def test_no_source_and_bad_task(tmp_path: Path) -> None:
    _task(tmp_path / "src" / "a")
    _task(tmp_path / "src" / "b", task_id="short")
    result = import_episodes(tmp_path / "src", tmp_path / "eps")
    assert sorted(r["code"] for r in result.refused) == ["no_source", "task_invalid"]


def test_readback_source(tmp_path: Path) -> None:
    records = [
        {"id": 1, "patient_id": "p1", "name": "Jane Sample", "type": "Triage", "note": "old"},
        {"id": 2, "patient_id": "p2", "name": "Alex", "type": "Consult", "note": "x"},
    ]
    _task(tmp_path / "src" / "a", fields=["type", "note"],
          readback={"verify": "verify.yaml", "match": {"id": 1}})
    _task(tmp_path / "src" / "b", task_id="ref-task-0002", fields=["type"],
          readback={"verify": "verify.yaml"},
          expected_record={"patient_id": "p9", "name": "Nobody"})
    result = import_episodes(tmp_path / "src", tmp_path / "eps",
                             reader=lambda rb, folder, expected: records)
    assert result.imported == ["ref-task-0001"]
    assert [r["code"] for r in result.refused] == ["readback_ambiguous"]
    ep = json.loads((tmp_path / "eps" / "ref-task-0001" / "reference_episode.json").read_text())
    assert ep["human_source"] == "readback"
    assert ep["human_fields"] == [{"name": "type", "value": "Triage"}, {"name": "note", "value": "old"}]


def test_readback_needs_field_names_and_a_reachable_store(tmp_path: Path) -> None:
    _task(tmp_path / "src" / "a", readback={"verify": "verify.yaml"})
    _task(tmp_path / "src" / "b", task_id="ref-task-0002", fields=["note"],
          readback={"verify": "missing.yaml"})
    result = import_episodes(tmp_path / "src", tmp_path / "eps")
    assert sorted(r["code"] for r in result.refused) == [
        "readback_invalid", "readback_unavailable"]


def test_readback_through_the_verify_config(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    from benchmark.agent_guard.app import serve
    from benchmark.effect_e2e.record_service import ENCOUNTERS_READ_CONTRACT

    app = serve(tmp_path / "app.sqlite")
    try:
        app.store.apply_write("ok", patient_id="p3", enc_type="Consult",
                              note="Synthetic readback note", key=None)
        import yaml

        folder = _task(
            tmp_path / "src" / "a",
            expected_record={"patient_id": "p3"},
            fields=["type", "note"],
            readback={"verify": "verify.yaml", "match": {"note": "Synthetic readback note"}},
        )
        (folder / "verify.yaml").write_text(
            "schema_version: openadapt.verify/v1\n"
            + yaml.safe_dump({"verifier": {"kind": "rest", "base_url": app.base_url,
                                           **ENCOUNTERS_READ_CONTRACT}})
            + "effects:\n  - kind: record_written\n    match:\n"
            "      patient_id: {param: patient_id}\n    expected_count: 1\n"
        )
        result = import_episodes(tmp_path / "src", tmp_path / "eps")
        assert result.refused == [] and result.imported == ["ref-task-0001"]
        ep = json.loads((tmp_path / "eps" / "ref-task-0001" / "reference_episode.json").read_text())
        assert ep["human_fields"] == [
            {"name": "type", "value": "Consult"},
            {"name": "note", "value": "Synthetic readback note"},
        ]
    finally:
        app.stop()
