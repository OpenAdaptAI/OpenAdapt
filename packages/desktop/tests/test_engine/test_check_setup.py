"""Check it pre-filled from the recording: proposals and answers.

Recordings here are written by hand in the ``openadapt.network-capture/1``
shape the recorder's network capture writes. All values are made up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("openadapt_flow.record_check_synthesis")

from openadapt_desktop.check_setup import (  # noqa: E402
    CheckSetupError,
    SetupStore,
    accept_record_check,
    answer_values,
    carry_to_sealed,
    learn_record_check,
    quiet_steps,
    value_proposals,
)

NOTE = "Synthetic follow-up in two weeks"
BASE = "http://127.0.0.1:9"


def _line(seq: int, method: str, path: str, t: float, body: Any = None, text: str = "") -> dict:
    line: dict[str, Any] = {
        "method": method,
        "url": BASE + path,
        "resource_type": "xhr",
        "frame_url": BASE + "/",
        "status": 200,
        "response_type": "application/json",
        "response_text": text,
        "seq": seq,
        "t": t,
    }
    if body is not None:
        line["request_type"] = "application/json"
        line["body"] = json.dumps(body)
    return line


def _recording(
    root: Path,
    *,
    params: dict[str, str] | None = None,
    stray_write: bool = False,
    network: bool = True,
) -> Path:
    """Open a record (t=1.0), type the note, save (t=2.0); the app reads after."""

    root.mkdir(parents=True)
    events = [
        {
            "i": 0,
            "kind": "click",
            "x": 10,
            "y": 10,
            "t": 1.0,
            "structural": {"role": "button", "name": "Open"},
        },
        {"i": 1, "kind": "type", "text": NOTE, "param": "note", "t": 1.5},
        {
            "i": 2,
            "kind": "click",
            "x": 300,
            "y": 400,
            "t": 2.0,
            "structural": {"role": "button", "name": "Save encounter"},
        },
    ]
    (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (root / "meta.json").write_text(json.dumps({"params": params or {"note": NOTE}}))
    if not network:
        return root
    saved = {"id": 1, "patient_id": "p1", "type": "Triage", "note": NOTE, "key": "demo-key-p1"}
    lines = [
        _line(0, "GET", "/api/db", 0.5, text=json.dumps({"records": []})),
    ]
    if stray_write:
        lines.append(_line(len(lines), "POST", "/api/audit", 1.2, body={"opened": "p1"}, text="{}"))
    lines += [
        _line(
            len(lines),
            "POST",
            "/api/encounter",
            1.9,
            body={"patient_id": "p1", "type": "Triage", "note": NOTE, "key": "demo-key-p1"},
            text=json.dumps({"ok": True, "id": 1}),
        ),
        _line(len(lines) + 1, "GET", "/api/db", 2.4, text=json.dumps({"records": [saved]})),
    ]
    with (root / "network.jsonl").open("w") as fh:
        fh.write(
            json.dumps({"schema": "openadapt.network-capture/1", "exchanges": len(lines)}) + "\n"
        )
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return root


def test_learns_the_binding_a_person_would_type_from_the_read_after_the_save(
    tmp_path: Path,
) -> None:
    learned = learn_record_check(_recording(tmp_path / "rec"), inputs={"note": {}})

    assert learned["found"] is True
    assert learned["read"] == "GET /api/db"
    binding = learned["binding"]
    # Selected through a run parameter, never a literal alone.
    assert binding["match_field"] == "note" and binding["match_param"] == "note"
    assert binding["kind"] == "record_written" and binding["count_new_only"] is True
    # What the save sent besides the typed value narrows the match.
    assert binding["extra_match"] == {
        "patient_id": {"literal": "p1"},
        "type": {"literal": "Triage"},
    }
    # The app's own anti-double-save key is checked as sent.
    assert binding["idempotency_literal"] == "demo-key-p1"
    assert binding["key_field"] == "key"
    assert binding["forbid_collateral_loss"] is True
    assert learned["effects"] == {
        "kind": "rest",
        "base_url": BASE,
        "records_path": "/api/db",
        "records_key": "records",
    }
    # The evidence is for this binding, including what it can't catch.
    test = learned["fault_test"]
    assert test["caught"] + len(test["missed_kinds"]) == test["bad_saves"]
    assert test["caught"] >= 3
    assert "nothing saved in api_db" in test["caught_kinds"]


def test_no_network_capture_proposes_nothing(tmp_path: Path) -> None:
    learned = learn_record_check(_recording(tmp_path / "rec", network=False), inputs={"note": {}})

    assert learned["found"] is False
    assert "no network capture" in learned["reason"]


def test_a_secret_input_is_never_used_to_find_the_record(tmp_path: Path) -> None:
    learned = learn_record_check(
        _recording(tmp_path / "rec"), inputs={"note": {}}, secret_inputs=frozenset({"note"})
    )

    assert learned["found"] is False


def test_steps_are_quiet_only_when_every_write_was_the_save(tmp_path: Path) -> None:
    quiet = quiet_steps(_recording(tmp_path / "one"))
    assert quiet is not None and quiet["write_requests"] == 1

    # A write while the record was being opened: no claim about the others.
    assert quiet_steps(_recording(tmp_path / "two", stray_write=True)) is None
    assert quiet_steps(_recording(tmp_path / "none", network=False)) is None


def test_values_from_one_or_several_recordings(tmp_path: Path) -> None:
    inputs = [
        {"name": "note", "example": NOTE},
        {"name": "clinic", "example": "North"},
        {"name": "pin", "example": "0000", "secret": True},
    ]
    one = _recording(tmp_path / "a", params={"note": NOTE, "clinic": "North"})
    proposals = {item["name"]: item for item in value_proposals(inputs, [one])}
    assert set(proposals) == {"note", "clinic"}  # a secret is never offered
    assert proposals["note"]["changes"] and proposals["clinic"]["changes"]

    two = _recording(tmp_path / "b", params={"note": "Another note", "clinic": "North"})
    proposals = {item["name"]: item for item in value_proposals(inputs, [one, two])}
    assert proposals["note"]["changes"] is True
    assert proposals["clinic"]["changes"] is False
    assert proposals["clinic"]["why"] == "The same in all 2 recordings."


class _Dispatcher:
    """Records the qualification commands a "yes" sends, in order."""

    def __init__(self, data_dir: Path, recording: Path, *, project: bool = True) -> None:
        self.config = type("C", (), {"data_dir": data_dir})()
        self.recording = recording
        self.calls: list[tuple[str, dict]] = []
        self.has_project = project
        db = type("DB", (), {})()
        db.get_bundle = lambda _id: {"capture_id": "cap"}
        db.get_capture = lambda _id: {
            "capture_path": str(recording),
            "task_description": "Add a note",
        }
        db.list_captures = lambda limit=200: []
        self.services = type("S", (), {"db": db})()

    def _task_inputs(self, workflow_id: str) -> list[dict]:
        return [{"name": "note", "example": NOTE, "secret": False}]

    def _task_target(self, workflow_id: str) -> dict:
        return {"backend": "web", "url": BASE + "/"}

    def get_qualification(self, **params: Any) -> dict:
        nodes = [
            {"id": "step_000", "kind": "action", "risk": "reversible", "effects": []},
            {"id": "step_001", "kind": "action", "risk": "reversible", "effects": []},
            {"id": "step_002", "kind": "action", "risk": "irreversible", "effects": []},
        ]
        return {
            "ok": True,
            "project": {"cases": []} if self.has_project else None,
            "graph": {"nodes": nodes},
            "controls": {
                "actions": {
                    "step_002": {
                        "identity": {"can_arm": True, "armed": True, "policy": None},
                        "effects": [],
                    }
                }
            },
        }

    def _record(self, name: str, params: dict) -> dict:
        self.calls.append((name, params))
        return {"ok": True}

    def set_qualification_risk(self, **params: Any) -> dict:
        return self._record("set_qualification_risk", params)

    def arm_qualification_identity(self, **params: Any) -> dict:
        return self._record("arm_qualification_identity", params)

    def bind_qualification_effect(self, **params: Any) -> dict:
        return self._record("bind_qualification_effect", params)


def test_yes_sends_the_same_commands_the_forms_send(tmp_path: Path) -> None:
    dispatcher = _Dispatcher(tmp_path / "data", _recording(tmp_path / "rec"))

    result = accept_record_check(dispatcher, "wf1", answered_by="agent")

    names = [name for name, _ in dispatcher.calls]
    assert names == [
        "set_qualification_risk",  # step_000: sent nothing
        "set_qualification_risk",  # step_001: sent nothing
        "arm_qualification_identity",
        "set_qualification_risk",  # the save, confirmed as irreversible
        "bind_qualification_effect",
    ]
    quiet = [params for name, params in dispatcher.calls[:2]]
    assert all(params["risk"] == "read_only" for params in quiet)
    assert "by the agent" in quiet[0]["explanation"]
    assert dispatcher.calls[3][1]["risk"] == "irreversible"
    assert dispatcher.calls[4][1]["match_param"] == "note"
    assert result["answers"]["record_check"]["by"] == "agent"
    # The read becomes the record-check settings every later run uses.
    config = SetupStore(tmp_path / "data").record_check_config("wf1")
    assert config is not None and "records_path: /api/db" in config.read_text()


def test_no_learned_check_means_no_yes(tmp_path: Path) -> None:
    dispatcher = _Dispatcher(tmp_path / "data", _recording(tmp_path / "rec", network=False))

    with pytest.raises(CheckSetupError, match="no record check to propose"):
        accept_record_check(dispatcher, "wf1", answered_by="person")
    assert dispatcher.calls == []


def test_fixed_values_are_the_recorded_ones_and_follow_the_sealed_version(tmp_path: Path) -> None:
    dispatcher = _Dispatcher(tmp_path / "data", _recording(tmp_path / "rec"))
    answer_values(dispatcher, "wf1", [], answered_by="person")
    store = SetupStore(tmp_path / "data")
    assert store.load("wf1")["fixed_values"] == {"note": NOTE}

    with pytest.raises(CheckSetupError, match="Unknown value"):
        answer_values(dispatcher, "wf1", ["member_id"], answered_by="person")

    store.write_record_check_config("wf1", {"kind": "rest", "base_url": BASE})
    saved = store.load("wf1")
    saved["record_check_config"] = str(store.root / "wf1.record-check.yaml")
    store.save("wf1", saved)
    carry_to_sealed(tmp_path / "data", "wf1", "wf2")
    assert store.load("wf2")["fixed_values"] == {"note": NOTE}
    assert store.record_check_config("wf2") is not None


def test_the_store_refuses_a_workflow_id_that_is_a_path(tmp_path: Path) -> None:
    with pytest.raises(CheckSetupError):
        SetupStore(tmp_path).load("../other")
