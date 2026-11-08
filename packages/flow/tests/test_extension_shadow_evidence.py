"""Optional Chrome extension: shadow-DOM paths, secret redaction, recording gate.

Runs ``packages/flow/chrome_extension/{content,background}.js`` in Node against
the fake DOM in ``tests/extension_support/chrome_extension_harness.js``.
These checks fail on a tree that lacks the extension scripts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "extension_support" / "chrome_extension_harness.js"
CONTENT = ROOT / "chrome_extension" / "content.js"
BACKGROUND = ROOT / "chrome_extension" / "background.js"


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
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_extension_files_exist():
    assert CONTENT.is_file()
    assert BACKGROUND.is_file()
    manifest = ROOT / "chrome_extension" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["manifest_version"] == 3
    assert "nativeMessaging" in data["permissions"]
    assert data["content_scripts"][0]["all_frames"] is True


def test_shadow_piercing_path_crosses_open_shadow_root():
    results = _run(
        "content",
        CONTENT,
        [
            {
                "build": {
                    "host": {"tag": "my-field", "attrs": {"id": "wrap"}},
                    "shadow": {
                        "mode": "open",
                        "children": [
                            {"tag": "input", "attrs": {"name": "claim_id", "id": "cid"}}
                        ],
                    },
                },
                "action": "path",
            }
        ],
    )
    path = results[0]["path"]
    assert path["in_shadow"] is True
    assert path["closed_shadow"] is False
    assert ">>>" in path["path"]
    assert "my-field#wrap" in path["path"]
    assert "input" in path["path"]


def test_closed_shadow_root_is_marked_and_stops():
    results = _run(
        "content",
        CONTENT,
        [
            {
                "build": {
                    "host": {"tag": "secure-field", "attrs": {"id": "sec"}},
                    "shadow": {
                        "mode": "closed",
                        "children": [{"tag": "input", "attrs": {"name": "ssn"}}],
                    },
                },
                "action": "path",
            }
        ],
    )
    path = results[0]["path"]
    # Closed shadow: harness attachShadow stores mode=closed; content script
    # sees getRootNode as shadow with mode closed when walking from leaf.
    # Our fake returns shadowRoot=null for closed, but the leaf still has
    # getRootNode → closed shadow root.
    assert path["closed_shadow"] is True
    assert ">>closed" in path["path"]


def test_password_value_is_redacted_in_evidence():
    results = _run(
        "content",
        CONTENT,
        [
            {
                "build": {
                    "host": {"tag": "div", "attrs": {"id": "form"}},
                    "shadow": {
                        "mode": "open",
                        "children": [
                            {
                                "tag": "input",
                                "attrs": {"type": "password", "name": "password"},
                                "value": "hunter2-should-never-leave",
                            }
                        ],
                    },
                },
                "action": "evidence",
                "event": "input",
            }
        ],
    )
    evidence = results[0]["evidence"]
    assert evidence["secret_field"] is True
    assert evidence["value"] == "<secret>"
    assert "hunter2" not in json.dumps(evidence)


def test_no_evidence_sent_before_recording_starts():
    results = _run(
        "content",
        CONTENT,
        [
            {
                "build": {
                    "host": {"tag": "div", "attrs": {"id": "x"}},
                    "shadow": {
                        "mode": "open",
                        "children": [{"tag": "button", "attrs": {"id": "go"}}],
                    },
                },
                "action": "click",
            }
        ],
    )
    assert results[0]["recording"] is False
    assert results[0]["sent"] == []


def test_evidence_sent_only_while_recording():
    results = _run(
        "content",
        CONTENT,
        [
            {"mode": "record"},
            {
                "build": {
                    "host": {"tag": "div", "attrs": {"id": "x"}},
                    "shadow": {
                        "mode": "open",
                        "children": [{"tag": "button", "attrs": {"id": "go"}}],
                    },
                },
                "action": "click",
            },
            {"mode": "idle"},
            {
                "build": {
                    "host": {"tag": "div", "attrs": {"id": "y"}},
                    "shadow": {
                        "mode": "open",
                        "children": [{"tag": "button", "attrs": {"id": "stop"}}],
                    },
                },
                "action": "click",
            },
        ],
    )
    # steps: mode record → result; click → result; mode idle → result; click → result
    click_while = results[1]
    click_after = results[3]
    assert click_while["recording"] is True
    assert any(m["msg"]["type"] == "EXTENSION_EVIDENCE" for m in click_while["sent"])
    assert click_after["recording"] is False
    assert click_after["sent"] == []


def test_background_queues_evidence_only_while_recording():
    evidence = {
        "schema": "openadapt.extension-evidence/1",
        "kind": "target",
        "path": "div#x >>> button#go",
        "in_shadow": True,
        "secret_field": False,
    }
    results = _run(
        "background",
        BACKGROUND,
        [
            {"evidence": evidence},
            {"state": True},
            {"mode": "record"},
            {"evidence": evidence},
            {"state": True},
            {"mode": "idle"},
            {"evidence": evidence},
            {"state": True},
        ],
    )
    # Find state snapshots
    states = [r for r in results if "queued" in r]
    assert states[0]["queued"] == []  # before record
    assert states[1]["queued"] == [evidence] or len(states[1]["queued"]) == 1
    assert states[2]["queued"] == []  # cleared on idle


def test_declared_secret_name_detected():
    results = _run(
        "content",
        CONTENT,
        [
            {"isSecret": {"tag": "input", "attrs": {"name": "password"}}},
            {"isSecret": {"tag": "input", "attrs": {"data-openadapt-secret": "1"}}},
            {"isSecret": {"tag": "input", "attrs": {"name": "claim_id"}}},
        ],
    )
    assert results[0]["secret"] is True
    assert results[1]["secret"] is True
    assert results[2]["secret"] is False


def _shadow_form(*children: dict, mode: str = "open") -> dict:
    return {
        "host": {"tag": "my-form", "attrs": {"id": "wrap"}},
        "shadow": {"mode": mode, "children": list(children)},
    }


def test_benign_name_does_not_hide_secret_id():
    # Review #1977 P1: `name || id` let a benign name mask id="password".
    results = _run(
        "content",
        CONTENT,
        [
            {"isSecret": {"tag": "input", "attrs": {"name": "note", "id": "password"}}},
            {"isSecret": {"tag": "input", "attrs": {"name": "password", "id": "note"}}},
            {
                "isSecret": {
                    "tag": "input",
                    "attrs": {"autocomplete": "current-password"},
                }
            },
            {"isSecret": {"tag": "input", "attrs": {"autocomplete": "one-time-code"}}},
            {"isSecret": {"tag": "input", "attrs": {"name": "note", "id": "memo"}}},
        ],
    )
    assert [r["secret"] for r in results] == [True, True, True, True, False]


def test_dispatched_input_withholds_secret_id_value_before_send():
    live = "hunter2-live-value-must-not-be-sent"
    results = _run(
        "content",
        CONTENT,
        [
            {"mode": "record"},
            {
                "build": _shadow_form(
                    {
                        "tag": "input",
                        "attrs": {"name": "note", "id": "password"},
                        "value": live,
                    }
                ),
                "action": "dispatch",
                "event": "input",
            },
        ],
    )
    sent = results[1]["sent"]
    assert len(sent) == 1
    evidence = sent[0]["msg"]["evidence"]
    assert evidence["secret_field"] is True
    assert evidence["value"] == "<secret>"
    assert live not in json.dumps(sent)


def test_dispatched_click_in_open_shadow_reports_inner_target():
    # Review #1977 P2: document listeners see ev.target retargeted to the host.
    results = _run(
        "content",
        CONTENT,
        [
            {"mode": "record"},
            {
                "build": _shadow_form({"tag": "button", "attrs": {"id": "go"}}),
                "action": "dispatch",
                "event": "click",
            },
        ],
    )
    step = results[1]
    # The harness really retargeted ev.target, so the old code would lose the leaf.
    assert step["dispatched"]["retargeted_id"] == "wrap"
    evidence = step["sent"][0]["msg"]["evidence"]
    assert evidence["tag"] == "button"
    assert evidence["id"] == "go"
    assert evidence["in_shadow"] is True
    assert evidence["path"].endswith(">>> button#go")


def test_dispatched_click_in_closed_shadow_stays_at_host():
    # Closed roots are opaque to page/content JS: evidence stops at the host.
    results = _run(
        "content",
        CONTENT,
        [
            {"mode": "record"},
            {
                "build": _shadow_form(
                    {"tag": "input", "attrs": {"name": "ssn"}, "value": "123-45-6789"},
                    mode="closed",
                ),
                "action": "dispatch",
                "event": "input",
            },
        ],
    )
    sent = results[1]["sent"]
    evidence = sent[0]["msg"]["evidence"]
    assert evidence["tag"] == "my-form"
    assert evidence["in_shadow"] is False
    assert "123-45-6789" not in json.dumps(sent)


def test_frame_url_drops_query_and_fragment():
    # Harness location is https://app.example/form?token=abc#frag
    results = _run(
        "content",
        CONTENT,
        [
            {
                "build": _shadow_form({"tag": "button", "attrs": {"id": "go"}}),
                "action": "evidence",
                "event": "click",
            },
            {"safeUrl": "https://u:p@pay.example:8443/a/b?token=x#y"},
            {"safeUrl": "data:text/html,<b>secret</b>"},
            {"safeUrl": "about:srcdoc"},
            {"safeUrl": "not a url"},
        ],
    )
    evidence = results[0]["evidence"]
    assert evidence["frame_url"] == "https://app.example/form"
    assert evidence["frame_origin"] == "https://app.example"
    assert evidence["is_iframe"] is False
    assert evidence["cross_origin_parent"] is False
    assert "token" not in json.dumps(evidence)
    assert [r["url"] for r in results[1:]] == [
        "https://pay.example:8443/a/b",
        "data:",
        "about:srcdoc",
        None,
    ]


def test_background_stamps_frame_identity_from_sender_not_page():
    spoof = {
        "schema": "openadapt.extension-evidence/1",
        "kind": "target",
        "path": "button#go",
        "frame": {"tab_id": 999, "frame_id": 0, "origin": "https://bank.example"},
    }
    sender = {
        "tab": {"id": 7},
        "frameId": 3,
        "documentId": "DOC-A",
        "origin": "https://child.example",
        "url": "https://child.example/frame?token=SENDER-TOKEN",
    }
    results = _run(
        "background",
        BACKGROUND,
        [
            {"mode": "record"},
            {"evidence": spoof, "sender": sender},
            {"state": True},
        ],
    )
    queued = [r for r in results if "queued" in r][0]["queued"]
    assert len(queued) == 1
    assert queued[0]["frame"] == {
        "tab_id": 7,
        "frame_id": 3,
        "document_id": "DOC-A",
        "origin": "https://child.example",
    }
    assert "SENDER-TOKEN" not in json.dumps(queued)
