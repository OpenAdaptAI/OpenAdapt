"""The Chrome extension prototype sends input events only while recording.

These tests run ``experimental/chrome_extension/content.js`` and
``experimental/chrome_extension/background.js`` in Node against the small
fake browser in ``tests/chrome_extension_harness.js`` and count the messages
each script sends.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "chrome_extension_harness.js"
CONTENT = ROOT / "experimental" / "chrome_extension" / "content.js"
BACKGROUND = ROOT / "experimental" / "chrome_extension" / "background.js"

INPUT_EVENTS = [
    {"event": "keydown"},
    {"event": "keyup"},
    {"event": "click"},
    {"event": "mousedown"},
    {"event": "mouseup"},
    {"event": "mousemove"},
    {"event": "scroll"},
]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required to test the Chrome extension scripts")
        pytest.skip("Node.js is not installed")
    return node


def _run(kind: str, script: Path, steps: list[dict]) -> list[dict]:
    completed = subprocess.run(
        [_node(), str(HARNESS), kind, str(script), json.dumps(steps)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _user_events(results: list[dict]) -> list[dict]:
    return [m for r in results for m in r["sent"] if m["type"] == "USER_EVENT"]


def test_content_script_stops_sending_input_when_recording_stops():
    results = _run(
        "content",
        CONTENT,
        [{"mode": "record"}, {"event": "keydown", "key": "s"}, {"mode": "idle"}, *INPUT_EVENTS],
    )

    recorded = _user_events(results[:2])
    assert [(m["eventType"], m["key"]) for m in recorded] == [("keydown", "s")]
    assert _user_events(results[2:]) == []
    # Leaving record mode removes the listeners instead of only marking them off.
    assert results[2]["listeners"] == {"document": 0, "body": 0}


def test_content_script_sends_no_input_events_in_replay_mode():
    results = _run(
        "content",
        CONTENT,
        [{"mode": "record"}, {"event": "keydown"}, {"mode": "replay"}, *INPUT_EVENTS],
    )

    assert len(_user_events(results[:2])) == 1
    assert _user_events(results[2:]) == []


def test_content_script_sends_nothing_before_recording_starts():
    results = _run("content", CONTENT, INPUT_EVENTS)

    assert _user_events(results) == []


def test_content_script_record_cycles_send_each_keystroke_once():
    results = _run(
        "content",
        CONTENT,
        [
            {"mode": "record"},
            {"mode": "idle"},
            {"mode": "record"},
            {"mode": "replay"},
            {"mode": "record"},
            {"event": "keydown", "key": "a"},
            {"event": "keyup", "key": "a"},
        ],
    )

    assert [(m["eventType"], m["key"]) for m in _user_events(results)] == [
        ("keydown", "a"),
        ("keyup", "a"),
    ]


def test_content_script_treats_an_unknown_mode_as_not_recording():
    results = _run(
        "content",
        CONTENT,
        [{"mode": "record"}, {"mode": "paused"}, *INPUT_EVENTS],
    )

    assert _user_events(results) == []
    assert results[1]["listeners"] == {"document": 0, "body": 0}


def test_background_relays_input_events_only_while_recording():
    user_event = {"message": {"type": "USER_EVENT", "eventType": "keydown", "key": "s"}}
    results = _run(
        "background",
        BACKGROUND,
        [
            user_event,
            {"mode": "record"},
            user_event,
            {"mode": "idle"},
            user_event,
            {"mode": "replay"},
            user_event,
            {"mode": "record"},
            user_event,
        ],
    )

    relayed = [len(r["sent"]) for r in results if "message" in r["step"]]
    assert relayed == [0, 1, 0, 0, 1]
