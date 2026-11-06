"""Real default Linux authoring on an isolated Xvfb/D-Bus GTK fixture.

Opt in with OAFLOW_LINUX_AUTHORING_E2E=1. Start Xvfb before dbus-run-session
so the private accessibility registry inherits the owned display. This module
owns its openbox and GTK children; it never connects to the Windows desktop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("OAFLOW_LINUX_AUTHORING_E2E") != "1",
    reason="requires an explicitly owned Linux Xvfb/D-Bus session",
)

FIXTURE = (
    Path(__file__).parents[1]
    / "scripts"
    / "fixtures"
    / "linux_atspi_qualification_app.py"
)
APP = "OpenAdapt Linux Qualification"


def _wait(predicate, *, process, description: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, f"owned GTK process exited during {description}"
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"bounded readiness failed: {description}")


def _stop_owned(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    assert process.poll() is not None
    return {"pid": process.pid, "returncode": process.returncode, "closed": True}


@pytest.fixture(scope="module")
def owned_window_manager(tmp_path_factory):
    assert os.environ.get("DISPLAY")
    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert os.environ.get("XDG_SESSION_TYPE") == "x11"
    root = tmp_path_factory.mktemp("linux-authoring-window-manager")
    with (root / "openbox.log").open("w") as log:
        process = subprocess.Popen(
            ["openbox", "--sm-disable"], stdout=log, stderr=subprocess.STDOUT
        )
        try:
            yield process
        finally:
            cleanup = _stop_owned(process)
            (root / "cleanup.json").write_text(json.dumps(cleanup))


@pytest.fixture
def owned_app(tmp_path, owned_window_manager, request):
    from openadapt_flow.backends.linux_backend import AtspiLinuxClient, LinuxBackend

    mode = "stale" if "stale" in request.node.name else "clean"
    title = f"Authoring smoke {uuid.uuid4().hex}"
    root = tmp_path / "linux-authoring"
    root.mkdir()
    effect, control = root / "effect", root / "control"
    client = AtspiLinuxClient()
    # Activate the private registry before launching this one counted GTK app.
    assert client.find_windows(APP, title) == []
    report = {"mode": mode, "outcome": "UNRUN", "physical_fallback": False}
    with (root / "gtk.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(FIXTURE),
                "--title",
                title,
                "--effect-path",
                str(effect),
                "--control-path",
                str(control),
                "--mode",
                mode,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:

            def ready():
                assert owned_window_manager.poll() is None
                windows = client.find_windows(APP, title)
                assert len(windows) <= 1
                if not windows:
                    return None
                window = windows[0]
                assert window.pid == process.pid
                return window if client.window_is_active(window) else None

            _wait(ready, process=process, description="unique owned active GTK window")
            backend = LinuxBackend(client, app=APP, window_title=title)
            assert backend._allow_physical_input is False
            yield SimpleNamespace(
                backend=backend,
                process=process,
                root=root,
                effect=effect,
                control=control,
                title=title,
                report=report,
            )
        finally:
            report["cleanup"] = _stop_owned(process)
            (root / "result.json").write_text(json.dumps(report, sort_keys=True))


def _node(observation, *, role: str, name: str) -> str:
    assert observation["backend"] == "linux"
    assert observation["provider"] == "linux_atspi"
    assert observation["agent_drive"] is True
    assert observation["truncated"] is False
    matches = [
        item
        for item in observation["tree"]
        if item["role"] == role and item.get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]["node_id"]


def test_default_linux_authoring_writes_independent_effect(owned_app):
    from openadapt_flow.authoring import AuthoringSession

    app = owned_app
    expected = b"synthetic authoring effect"
    session = AuthoringSession(
        app.backend, app.root / "recording", backend_kind="linux"
    )
    try:
        session.start_record()
        entry = _node(session.observe(), role="text_input", name="Effect value")
        session.click(node_id=entry)
        session.type_text(expected.decode(), param="effect")
        button = _node(session.observe(), role="button", name="Write effect")
        session.click(node_id=button)
        recording = session.stop_record()
        assert app.effect.read_bytes() == expected
        events = [
            json.loads(line)
            for line in (recording / "events.jsonl").read_text().splitlines()
        ]
        assert [event["kind"] for event in events] == ["click", "type", "click"]
        assert all(event["actor"] == "agent" for event in events)
        for index in (0, 2):
            assert events[index]["structural"]["name"] in {
                "Effect value",
                "Write effect",
            }
        for index in range(3):
            for phase in ("before", "after"):
                assert (
                    recording / "frames" / f"{index:04d}_{phase}.png"
                ).stat().st_size > 0
        app.report.update(outcome="PASS", effect_confirmed=True, recorded_actions=3)
    finally:
        session.halt()


def test_default_linux_authoring_refuses_replaced_stale_control(owned_app):
    from openadapt_flow.authoring import AuthoringError, AuthoringSession
    from openadapt_flow.ir import StructuralLocator

    app = owned_app
    session = AuthoringSession(
        app.backend, app.root / "recording", backend_kind="linux"
    )
    try:
        session.start_record()
        button = _node(session.observe(), role="button", name="Write effect")
        locator = StructuralLocator(
            role="button", name="Write effect", window_name=app.title
        )
        initial = app.backend.locate_structural(locator)
        assert initial is not None and initial.target_fingerprint
        app.control.write_text("replace")

        def replaced():
            current = app.backend.locate_structural(locator)
            return (
                current
                if current and current.target_fingerprint != initial.target_fingerprint
                else None
            )

        _wait(
            replaced, process=app.process, description="genuinely replaced GTK control"
        )
        with pytest.raises(AuthoringError) as refused:
            session.click(node_id=button)
        assert refused.value.code == "stale_node"
        recording = session.stop_record()
        assert not app.effect.exists()
        assert (recording / "events.jsonl").read_text() == ""
        app.report.update(
            outcome="PASS", refusal="stale_node", effect_absent=True, recorded_actions=0
        )
    finally:
        session.halt()
