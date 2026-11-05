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
