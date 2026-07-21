"""Backend factory + CLI backend-selector.

The CLI used to hardcode the Playwright browser backend; the Windows (WAA) and
RDP/remote-display backends were library-only. These tests pin the factory that
turns a :class:`~openadapt_flow.deployment.BackendConfig` into the right backend
(fail-loud on an unknown kind or a missing target), the flag->config merge, and
that the default web path is untouched — no browser, no network.
"""

from __future__ import annotations

import pytest
from PIL import Image

from openadapt_flow.__main__ import _resolve_backend_config, build_parser
from openadapt_flow.backends.factory import _normalize_kind, build_backend
from openadapt_flow.deployment import BackendConfig, DeploymentConfig

# --- kind normalization -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "web"),
        ("web", "web"),
        ("WEB", "web"),
        (" windows ", "windows"),
        ("macos", "macos"),
        ("linux", "linux"),
        ("rdp", "rdp"),
        ("remote-display", "rdp"),
        ("remote_display", "rdp"),
        ("citrix", "rdp"),
    ],
)
def test_normalize_kind(raw, expected) -> None:
    assert _normalize_kind(raw) == expected


# --- web --------------------------------------------------------------------


class _FakePage:
    """A stand-in Playwright page (PlaywrightBackend only stores it)."""


def test_web_builds_playwright_backend_from_page() -> None:
    backend = build_backend(BackendConfig(kind="web"), page=_FakePage())
    assert type(backend).__name__ == "PlaywrightBackend"


def test_web_without_page_fails_loud() -> None:
    with pytest.raises(ValueError, match="needs a live Playwright page"):
        build_backend(BackendConfig(kind="web"))


# --- windows ----------------------------------------------------------------


def test_windows_builds_windows_backend() -> None:
    backend = build_backend(
        BackendConfig(kind="windows", agent_url="http://localhost:5001")
    )
    assert type(backend).__name__ == "WindowsBackend"
    # server_url is normalized (trailing slash stripped) by the backend.
    assert backend.server_url == "http://localhost:5001"


def test_windows_threads_auth_token() -> None:
    # Loopback URL: this asserts token-threading only, NOT the TLS guard. A
    # non-loopback ``http://`` would (correctly) be refused by WindowsBackend's
    # fail-closed require_tls default (#112); loopback plaintext is the sanctioned
    # dev channel, so the token assertion runs without tripping that guard.
    backend = build_backend(
        BackendConfig(
            kind="windows", agent_url="http://localhost:5001", agent_token="s3cr3t"
        )
    )
    assert backend._auth_token == "s3cr3t"


def test_windows_requires_agent_url() -> None:
    with pytest.raises(ValueError, match="requires backend.agent_url"):
        build_backend(BackendConfig(kind="windows"))


def test_windows_threads_tls_pin_into_pinned_session() -> None:
    fingerprint = "ab" * 32
    backend = build_backend(
        BackendConfig(
            kind="windows",
            agent_url="https://host:5001",
            agent_tls_pin=fingerprint,
        )
    )
    assert backend._pin_fingerprint == fingerprint


# --- native macOS -----------------------------------------------------------


def test_macos_cli_flags_override_config() -> None:
    cfg = DeploymentConfig()
    args = _replay_args(
        [
            "--backend",
            "macos",
            "--macos-app",
            "TextEdit",
            "--macos-window-title",
            "oa-trial",
        ]
    )
    merged = _resolve_backend_config(args, cfg)
    assert merged.kind == "macos"
    assert merged.macos_app == "TextEdit"
    assert merged.macos_window_title == "oa-trial"


def test_linux_cli_flags_override_config() -> None:
    cfg = DeploymentConfig()
    args = _replay_args(
        [
            "--backend",
            "linux",
            "--linux-app",
            "gedit",
            "--linux-window-title",
            "oa-trial.txt",
            "--linux-allow-physical-input",
        ]
    )
    merged = _resolve_backend_config(args, cfg)
    assert merged.kind == "linux"
    assert merged.linux_app == "gedit"
    assert merged.linux_window_title == "oa-trial.txt"
    assert merged.linux_allow_physical_input is True


# --- rdp (network, via injected transport) ----------------------------------


class _FakeRDPTransport:
    """Minimal RDPTransport: a solid framebuffer, records connect/disconnect."""

    def __init__(self) -> None:
        self.connected = False
        self._img = Image.new("RGB", (320, 240), (10, 20, 30))

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def framebuffer(self):
        return self._img, self._img.width, self._img.height

    def pointer(self, x, y, button, down) -> None:  # pragma: no cover - unused
        pass

    def key(self, keysym_or_char, down) -> None:  # pragma: no cover - unused
        pass

    def wheel(self, dx, dy) -> None:  # pragma: no cover - unused
        pass


def test_rdp_builds_freerdp_backend_from_host() -> None:
    transport = _FakeRDPTransport()
    backend = build_backend(
        BackendConfig(kind="rdp", rdp_host="10.0.0.5"), rdp_transport=transport
    )
    assert type(backend).__name__ == "FreeRDPBackend"
    # FreeRDPBackend connects its transport on construction (connect=True).
    assert transport.connected is True
    assert backend.viewport == (320, 240)


def test_rdp_missing_target_fails_loud() -> None:
    with pytest.raises(ValueError, match="requires backend.rdp_host"):
        build_backend(BackendConfig(kind="rdp"))


# --- rdp (local remote-display client window) -------------------------------


class _FakeWindowClient:
    """Minimal remote-display WindowClient (no macOS, no window)."""

    def input_trusted(self) -> bool:  # pragma: no cover - unused here
        return True

    def frontmost_pid(self):  # pragma: no cover - unused here
        return None

    def find_windows(self, owner, title):  # pragma: no cover
        return []

    def key_window_id(self, pid):  # pragma: no cover - unused here
        return None

    def window_at_point(self, x, y):  # pragma: no cover - unused here
        return None

    def capture(self, window_id):  # pragma: no cover - unused here
        return b"", 0, 0

    def activate(self, pid) -> None:  # pragma: no cover - unused here
        pass

    def mouse(self, *a, **k) -> None:  # pragma: no cover - unused here
        pass

    def mouse_move(self, *a, **k) -> None:  # pragma: no cover - unused here
        pass

    def type_chars(self, text) -> None:  # pragma: no cover - unused here
        pass

    def key(self, *a, **k) -> None:  # pragma: no cover - unused here
        pass

    def scroll(self, dx, dy) -> None:  # pragma: no cover - unused here
        pass

    def resolve_key(self, token):  # pragma: no cover - unused here
        return None


def test_rdp_window_builds_remote_display_backend() -> None:
    backend = build_backend(
        BackendConfig(
            kind="rdp", rdp_window="Parallels Desktop", rdp_window_title="Win11"
        ),
        window_client=_FakeWindowClient(),
    )
    assert type(backend).__name__ == "RemoteDisplayBackend"
    assert backend._owner_substr == "Parallels Desktop"
    assert backend._title_substr == "Win11"


def test_rdp_safety_config_threads_frame_age_and_readiness(monkeypatch) -> None:
    transport = _FakeRDPTransport()
    seen: dict[str, object] = {}

    def fake_find_text(png, expected, *, min_ratio):
        seen.update(png=png, expected=expected, min_ratio=min_ratio)
        return object()

    monkeypatch.setattr("openadapt_flow.vision.find_text", fake_find_text)
    backend = build_backend(
        BackendConfig(
            kind="rdp",
            rdp_host="10.0.0.5",
            rdp_max_frame_age_s=3.5,
            rdp_readiness_text="Expected App",
            rdp_readiness_min_ratio=0.9,
        ),
        rdp_transport=transport,
    )
    assert backend._max_frame_age_s == 3.5
    assert backend._readiness_probe is not None
    assert backend._readiness_probe(b"png") is True
    assert seen == {"png": b"png", "expected": "Expected App", "min_ratio": 0.9}


# --- unknown kind -----------------------------------------------------------


def test_unknown_kind_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown backend.kind"):
        build_backend(BackendConfig(kind="teleport"))


# --- flag -> config merge ---------------------------------------------------


def _replay_args(argv):
    return build_parser().parse_args(["replay", "b", *argv])


def test_no_flags_preserves_web_default() -> None:
    cfg = DeploymentConfig()
    merged = _resolve_backend_config(_replay_args([]), cfg)
    assert merged.kind == "web"
    assert merged.agent_url is None and merged.rdp_host is None
    # Same object contents as the config's backend (web unchanged).
    assert merged == cfg.backend


def test_flags_override_config_backend() -> None:
    cfg = DeploymentConfig(backend=BackendConfig(kind="web", url="http://demo"))
    args = _replay_args(["--backend", "windows", "--agent-url", "http://a:5001"])
    merged = _resolve_backend_config(args, cfg)
    assert merged.kind == "windows"
    assert merged.agent_url == "http://a:5001"
    # Untouched config fields survive the merge.
    assert merged.url == "http://demo"


def test_rdp_host_flag_overrides_config() -> None:
    cfg = DeploymentConfig()
    args = _replay_args(["--backend", "rdp", "--rdp-host", "10.1.2.3"])
    merged = _resolve_backend_config(args, cfg)
    assert merged.kind == "rdp"
    assert merged.rdp_host == "10.1.2.3"


def test_config_backend_used_when_no_flag() -> None:
    # A deployment config selects windows; no CLI flag needed.
    cfg = DeploymentConfig(
        backend=BackendConfig(kind="windows", agent_url="http://cfg:5001")
    )
    merged = _resolve_backend_config(_replay_args([]), cfg)
    assert merged.kind == "windows"
    assert merged.agent_url == "http://cfg:5001"


# --- record refuses non-web -------------------------------------------------


@pytest.mark.parametrize("kind", ["windows", "macos", "linux", "rdp"])
def test_record_desktop_backend_invokes_capture(monkeypatch, tmp_path, kind) -> None:
    """Every desktop selector routes to capture, not the web recorder."""
    from openadapt_flow.__main__ import _cmd_record

    captured: dict = {}

    def fake_record(
        out_dir, *, task_description, params, identifier_region=None, window=None
    ):
        captured["out"] = out_dir
        captured["params"] = params
        captured["window"] = window
        return out_dir

    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture", fake_record
    )
    args = build_parser().parse_args(
        ["record", "--out", str(tmp_path / "rec"), "--backend", kind]
    )
    assert _cmd_record(args) == 0
    assert captured["params"] == {}
    assert captured["window"] is None  # no --window: full-screen capture


# --- CLI replay drives the desktop backend (no browser, stubbed agent) ------


class _FakeReport:
    success = True
    screenshots_may_leave_box = False


def test_replay_windows_constructs_windows_backend(monkeypatch, tmp_path) -> None:
    """`replay --backend windows` builds a WindowsBackend and runs it — no
    browser, no MockMed, the agent stubbed out."""
    import openadapt_flow.__main__ as m

    captured: dict = {}

    def fake_run(backend, **kwargs):
        captured["backend"] = backend
        return _FakeReport()

    monkeypatch.setattr(m, "_build_and_run_replayer", fake_run)
    monkeypatch.setattr(
        "openadapt_flow.report.render_run_report", lambda run_dir: "REPORT.md"
    )
    monkeypatch.setattr("openadapt_flow.ir.Workflow.load", lambda bundle: object())

    args = m.build_parser().parse_args(
        [
            "replay",
            "bundle",
            "--backend",
            "windows",
            "--agent-url",
            "http://localhost:5001",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    rc = m._cmd_replay(args)

    assert rc == 0
    assert type(captured["backend"]).__name__ == "WindowsBackend"
    assert captured["backend"].server_url == "http://localhost:5001"


def test_replay_desktop_refuses_drift(monkeypatch, tmp_path) -> None:
    """--drift is a MockMed web teaching aid; it must be refused on a desktop
    backend rather than silently ignored."""
    import openadapt_flow.__main__ as m

    monkeypatch.setattr("openadapt_flow.ir.Workflow.load", lambda bundle: object())
    args = m.build_parser().parse_args(
        [
            "replay",
            "bundle",
            "--backend",
            "windows",
            "--agent-url",
            "http://localhost:5001",
            "--drift",
            "theme",
        ]
    )
    with pytest.raises(SystemExit, match="drift"):
        m._cmd_replay(args)
