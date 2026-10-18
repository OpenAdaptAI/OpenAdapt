"""An agent drives recording in native Windows apps and over RDP or Citrix.

No Windows host or remote session is needed: Flow's backend factory is
replaced with a recorder of the config it receives, and the Flow session runs
over an in-memory backend. Flow's own windows-mock suite drives the real
WindowsBackend and RemoteDisplayBackend through the same session.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_agent.authoring import (
    AuthoringBridge,
    AuthoringError,
    open_authoring_session,
    pin_local_backend,
    project_observe,
)

flow_authoring = pytest.importorskip("openadapt_flow.authoring")
if not hasattr(flow_authoring, "SCREEN_ONLY_KINDS"):
    pytest.skip("needs an openadapt-flow that drives Windows/RDP/Citrix", allow_module_level=True)


def _png(size=(320, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


class FakeDesktopBackend:
    """Stands in for WindowsBackend / RemoteDisplayBackend: pixels in, input out."""

    def __init__(self) -> None:
        self.frame = _png()
        self.calls: list[tuple] = []

    @property
    def viewport(self):
        return (320, 200)

    def screenshot(self) -> bytes:
        return self.frame

    def click(self, x, y, *, double=False):
        self.calls.append(("click", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def press(self, key):
        self.calls.append(("press", key))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


@pytest.fixture()
def factory(monkeypatch):
    import openadapt_flow.backends.factory as factory_mod

    built: list = []

    def build_backend(cfg, **_kwargs):
        backend = FakeDesktopBackend()
        built.append((cfg, backend))
        return backend

    monkeypatch.setattr(factory_mod, "build_backend", build_backend)
    return built


def _events(rec_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (rec_dir / "events.jsonl").read_text().splitlines()]


def test_windows_agent_url_pins_flow_windows_backend(factory):
    backend, kind, _close = pin_local_backend(
        windows_agent_url="http://localhost:5001", windows_agent_token="t0k"
    )
    cfg, built = factory[0]
    assert backend is built
    assert kind == "windows"
    assert cfg.kind == "windows"
    assert cfg.agent_url == "http://localhost:5001"
    assert cfg.agent_token == "t0k"


@pytest.mark.parametrize(("citrix", "kind"), [(False, "rdp"), (True, "citrix")])
def test_remote_window_pins_client_window(factory, citrix, kind):
    _backend, pinned_kind, _close = pin_local_backend(
        remote_window="Microsoft Remote Desktop",
        remote_window_title="Clinic",
        citrix=citrix,
    )
    cfg, _built = factory[0]
    assert pinned_kind == kind
    assert cfg.kind == kind
    assert cfg.rdp_window == "Microsoft Remote Desktop"
    assert cfg.rdp_window_title == "Clinic"


def test_pin_refuses_two_targets(factory):
    with pytest.raises(AuthoringError, match="pin one target"):
        pin_local_backend(windows_agent_url="http://localhost:5001", citrix=True)
    with pytest.raises(AuthoringError, match="can't be combined"):
        pin_local_backend(url="https://example.invalid/", citrix=True)


def test_agent_drives_native_windows_recording(factory, tmp_path):
    session = open_authoring_session(
        out_dir=tmp_path / "rec",
        windows_agent_url="http://localhost:5001",
        settle_interval_s=0.0,
        settle_stable_frames=1,
        settle_timeout_s=0.1,
    )
    bridge = AuthoringBridge(session, out_dir=tmp_path)
    observed = bridge.dispatch("observe", {})
    assert observed["backend"] == "windows"
    assert observed["agent_drive"] is True
    assert observed["coach_only"] is False
    bridge.dispatch("start_record", {})
    bridge.dispatch("click", {"x": 40, "y": 50})
    bridge.dispatch("type", {"text": "follow up in two weeks", "param": "note"})
    bridge.dispatch("stop_record", {})
    _cfg, backend = factory[0]
    assert backend.calls == [("click", 40, 50), ("type", "follow up in two weeks")]
    rec_dir = session.recording_dir
    assert [e["actor"] for e in _events(rec_dir)] == ["agent", "agent"]
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["demonstrated_by"] == "agent"
    assert meta["surface"] == "windows"


def test_agent_drives_citrix_recording_screen_only(factory, tmp_path):
    session = open_authoring_session(
        out_dir=tmp_path / "rec",
        remote_window="Citrix Viewer",
        citrix=True,
        settle_interval_s=0.0,
        settle_stable_frames=1,
        settle_timeout_s=0.1,
    )
    bridge = AuthoringBridge(session, out_dir=tmp_path, screens="unscrubbed")
    observed = bridge.dispatch("observe", {})
    assert observed["backend"] == "citrix"
    assert observed["provider"] == "none"
    assert observed["tree"] == []
    screen = bridge.dispatch("screen", {})
    assert screen["scrubbed"] is False
    assert (screen["width"], screen["height"]) == (320, 200)
    assert base64.b64decode(screen["screen_png_base64"]).startswith(b"\x89PNG")
    bridge.dispatch("start_record", {})
    bridge.dispatch("click", {"x": 100, "y": 60})
    bridge.dispatch("stop_record", {})
    meta = json.loads((session.recording_dir / "meta.json").read_text())
    assert meta["surface"] == "citrix"
    assert meta["authoring"]["screen_only"] is True
    assert meta["backend_hints"] == {"backend": "citrix", "rdp_window": "Citrix Viewer"}


def test_screen_tool_is_off_unless_the_operator_turns_it_on(factory, tmp_path):
    session = open_authoring_session(out_dir=tmp_path / "rec", citrix=True)
    bridge = AuthoringBridge(session, out_dir=tmp_path)
    assert "screen" not in {spec.name for spec in bridge.list_tool_specs()}
    assert bridge.handles("screen") is False
    with pytest.raises(AuthoringError, match="unknown tool"):
        bridge.dispatch("screen", {})
    with pytest.raises(AuthoringError, match="screens must be"):
        AuthoringBridge(session, screens="always")


def test_scrubbed_screen_scrubs_or_shares_nothing(factory, tmp_path):
    session = open_authoring_session(out_dir=tmp_path / "rec", citrix=True)

    class Scrubber:
        def validate_ready(self, modalities):
            pass

        def scrub_image(self, image):
            return Image.new("RGB", image.size, (0, 0, 0))

        def evidence(self, modalities):
            return {"status": "completed", "provider": "fake"}

    bridge = AuthoringBridge(session, screens="scrubbed", scrubber_factory=Scrubber)
    screen = bridge.dispatch("screen", {})
    assert screen["scrubbed"] is True

    class Broken(Scrubber):
        def scrub_image(self, image):
            raise RuntimeError("no OCR")

    bridge = AuthoringBridge(session, screens="scrubbed", scrubber_factory=Broken)
    with pytest.raises(AuthoringError, match="no screen was shared"):
        bridge.dispatch("screen", {})


def test_windows_agent_drive_survives_older_types_contract():
    projected = project_observe(
        {
            "backend": "windows",
            "provider": "windows_uia",
            "agent_drive": True,
            "coach_only": False,
            "window": {
                "process_name": "Windows",
                "role": "window",
                "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
            },
            "tree": [],
        }
    )
    assert projected["agent_drive"] is True
    assert projected["coach_only"] is False


def test_desktop_pin_flags_need_authoring():
    from openadapt_agent.cli import main

    assert main(["serve", "--citrix"]) == 2


# -- the deployment file names the app (docs/BACKEND_PARITY.md) -----------------

SECTIONS = {
    "windows": {"kind": "windows", "agent_url": "http://localhost:5001"},
    "macos": {"kind": "macos", "macos_app": "TextEdit"},
    "linux": {"kind": "linux", "linux_app": "gedit", "linux_window_title": "Notes"},
    "rdp": {"kind": "rdp", "rdp_window": "mstsc", "rdp_window_title": "Clinic"},
    "citrix": {
        "kind": "citrix",
        "rdp_window": "Citrix Viewer",
        "rdp_readiness_text": "Patient Notes",
    },
}


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_a_deployment_section_pins_any_surface(factory, kind):
    _backend, pinned, _close = pin_local_backend(deployment_backend=SECTIONS[kind])
    cfg, _built = factory[0]
    assert pinned == kind and cfg.kind == kind
    for key, value in SECTIONS[kind].items():
        assert getattr(cfg, key) == value


def test_a_deployment_section_is_one_target(factory):
    with pytest.raises(AuthoringError, match="pin one target"):
        pin_local_backend(deployment_backend=SECTIONS["linux"], url="https://example.invalid/")
    with pytest.raises(AuthoringError, match="names a web app"):
        pin_local_backend(deployment_backend={"kind": "web", "url": "https://x/"})
    with pytest.raises(AuthoringError, match="network RDP"):
        pin_local_backend(deployment_backend={"kind": "rdp", "rdp_host": "10.0.0.5"})
    with pytest.raises(AuthoringError, match="pin one target"):
        pin_local_backend(deployment_backend=SECTIONS["citrix"], remote_window_title="Clinic")
    assert factory == []


def test_a_deployment_windows_section_keeps_the_env_agent_token(factory):
    pin_local_backend(deployment_backend=SECTIONS["windows"], windows_agent_token="t")
    assert factory[0][0].agent_token == "t"
    pin_local_backend(
        deployment_backend=dict(SECTIONS["windows"], agent_token="file"),
        windows_agent_token="t",
    )
    assert factory[1][0].agent_token == "file"


@pytest.mark.parametrize("kind", ["linux", "citrix"])
def test_agent_records_the_app_a_deployment_file_names(factory, tmp_path, kind):
    session = open_authoring_session(
        out_dir=tmp_path / "rec",
        deployment_backend=SECTIONS[kind],
        settle_interval_s=0.0,
        settle_stable_frames=1,
        settle_timeout_s=0.1,
    )
    bridge = AuthoringBridge(session, out_dir=tmp_path)
    assert bridge.dispatch("observe", {})["backend"] == kind
    bridge.dispatch("start_record", {})
    bridge.dispatch("click", {"x": 40, "y": 50})
    bridge.dispatch("stop_record", {})
    meta = json.loads((session.recording_dir / "meta.json").read_text())
    assert meta["surface"] == kind and meta["demonstrated_by"] == "agent"
    if kind == "citrix":
        assert meta["backend_hints"] == {
            "backend": "citrix",
            "rdp_window": "Citrix Viewer",
            "rdp_readiness_text": "Patient Notes",
        }


def test_serve_pins_the_config_section_only_when_asked(tmp_path):
    import argparse

    from openadapt_flow.agent.cli import _authoring_section, build_parser

    linux = tmp_path / "linux.yaml"
    linux.write_text("backend:\n  kind: linux\n  linux_app: gedit\n  linux_window_title: Notes\n")
    web = tmp_path / "web.yaml"
    web.write_text("backend:\n  kind: web\n  url: https://example.invalid/\n")
    broken = tmp_path / "broken.yaml"
    broken.write_text("backend:\n  kind: [not, a, kind]\n")

    def ns(*argv):
        return build_parser().parse_args(["serve", "--authoring", *argv])

    # A --config given for runs alone never pins authoring.
    assert _authoring_section(ns("--config", str(linux))) is None
    asked = ns("--config", str(linux), "--authoring-from-config")
    assert _authoring_section(asked).kind == "linux"
    with pytest.raises(AuthoringError, match="names a web app"):
        _authoring_section(ns("--config", str(web), "--authoring-from-config"))
    with pytest.raises(AuthoringError, match="needs --config"):
        _authoring_section(ns("--authoring-from-config"))
    with pytest.raises(AuthoringError, match="could not read"):
        _authoring_section(ns("--config", str(broken), "--authoring-from-config"))
    assert isinstance(asked, argparse.Namespace)
