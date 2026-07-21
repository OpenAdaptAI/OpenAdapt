"""Unit + conformance tests for the WindowsBackend (WAA HTTP).

No live VM: a stdlib HTTP server mocks the WAA Flask contract
(``GET /screenshot`` -> raw PNG bytes, ``POST /execute_windows`` ->
``exec()`` of bare Python with pyautogui importable). Command payloads are
validated the way the real server consumes them — ``exec()``'d against a
recording fake pyautogui — so quoting/format bugs cannot hide.

The conformance test runs the UNMODIFIED Recorder -> compiler -> Replayer
stack over the WindowsBackend against a stateful mock screen, proving the
4-method Backend protocol needs zero compiler/replayer changes for desktop.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from openadapt_flow.backend import Backend
from openadapt_flow.backends.windows_backend import (
    WindowsBackend,
    normalize_chord,
)

VIEWPORT = (1280, 800)

# The synthetic desktop app (drawn with cv2, like tests/test_compiler.py).
BUTTON = (560, 400, 160, 48)  # x, y, w, h
BUTTON_CENTER = (BUTTON[0] + BUTTON[2] // 2, BUTTON[1] + BUTTON[3] // 2)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"

_CLICK_RE = re.compile(r"pyautogui\.(?:double)?[cC]lick\((\d+), (\d+)\)")


def blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img,
        text,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def app_screens() -> list[bytes]:
    """The 4 states of the synthetic desktop app."""
    s0 = blank()
    draw_text(s0, 520, 84, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")

    s1 = s0.copy()
    draw_text(s1, 420, 244, BANNER_LOADED)

    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)  # inside the 640x240 field region

    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    return [png_bytes(s) for s in [s0, s1, s2, s3]]


class MockWaa:
    """Stateful mock of the WAA Flask server contract.

    ``screens`` is the app's state ladder; ``/execute_windows`` commands
    advance ``state`` exactly as the real app would: a click inside the
    button (state 0->1), typed text (1->2), Enter (2->3). Commands are
    recorded for assertions. ``screenshot_failures`` makes the next N
    screenshot requests return HTTP 500 (retry tests).
    """

    def __init__(self, screens: list[bytes]) -> None:
        self.screens = screens
        self.state = 0
        self.commands: list[str] = []
        self.screenshot_failures = 0
        self._lock = threading.Lock()

        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                pass

            def _consume_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _reply(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _not_found(self) -> None:
                self._reply(404, b"not found", "text/plain")

            def do_GET(self) -> None:
                if self.path != "/screenshot":
                    self._not_found()
                    return
                with mock._lock:
                    if mock.screenshot_failures > 0:
                        mock.screenshot_failures -= 1
                        self._reply(500, b"not ready", "text/plain")
                        return
                    body = mock.screens[mock.state]
                self._reply(200, body, "image/png")

            def do_POST(self) -> None:
                raw = self._consume_body()
                if self.path != "/execute_windows":
                    self._not_found()
                    return
                command = json.loads(raw)["command"]
                with mock._lock:
                    mock.commands.append(command)
                    mock._advance(command)
                self._reply(
                    200,
                    json.dumps({"status": "ok"}).encode(),
                    "application/json",
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def _advance(self, command: str) -> None:
        """State machine of the synthetic app (coordinate-checked)."""
        match = _CLICK_RE.search(command)
        if match and self.state == 0:
            x, y = int(match.group(1)), int(match.group(2))
            bx, by, bw, bh = BUTTON
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.state = 1
            return
        if "pyautogui.write(" in command and self.state == 1:
            self.state = 2
            return
        if "pyautogui.press('enter')" in command and self.state == 2:
            self.state = 3

    def reset(self) -> None:
        with self._lock:
            self.state = 0
            self.commands.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def waa() -> MockWaa:
    server = MockWaa(app_screens())
    yield server
    server.close()


@pytest.fixture()
def backend(waa: MockWaa) -> WindowsBackend:
    waa.reset()
    return WindowsBackend(
        waa.url, screenshot_retry_delay_s=0.01, allow_legacy_exec=True
    )


class FakePyautogui(types.ModuleType):
    """Records every call, standing in for pyautogui on the exec side."""

    def __init__(self) -> None:
        super().__init__("pyautogui")
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        def method(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args, kwargs))

        return method


def exec_last_command(
    waa: MockWaa, monkeypatch: pytest.MonkeyPatch, *, fake_subprocess=None
) -> FakePyautogui:
    """exec() the last sent command the way the WAA server does.

    Proves the payload is bare, valid Python (never ``python -c``-wrapped)
    and reveals the exact pyautogui calls it makes.
    """
    fake = FakePyautogui()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    if fake_subprocess is not None:
        monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
    command = waa.commands[-1]
    assert "python -c" not in command
    exec(command, {})  # noqa: S102 - mirrors the server's exec(command, ...)
    return fake


# -- protocol conformance ------------------------------------------------------


def test_implements_backend_protocol(backend: WindowsBackend) -> None:
    assert isinstance(backend, Backend)


def test_no_structural_observations(backend: WindowsBackend) -> None:
    # Native desktop has no cheap URL/title/page-count: the backend must
    # not fake them (steps stay honestly unverified instead).
    for attr in ("url", "page_title", "page_count"):
        assert not hasattr(backend, attr)


def test_implements_structural_action_protocol(backend: WindowsBackend) -> None:
    # The backend-agnostic resolver drives the UIA structural rung through this
    # protocol; conformance means the ladder needs zero changes for desktop.
    from openadapt_flow.backend import StructuralActionBackend

    assert isinstance(backend, StructuralActionBackend)


# -- hardening: unreachable / non-2xx / empty UIA (never a silent wrong action) --


def test_execute_unreachable_raises_runtime_error() -> None:
    # An action against a dead endpoint must FAIL LOUDLY (a dropped click is a
    # silent wrong action), not raise a bare transport error or no-op.
    backend = WindowsBackend("http://127.0.0.1:9", timeout_s=0.5)
    with pytest.raises(RuntimeError, match="unreachable"):
        backend.click(1, 1)


def test_structural_locator_unreachable_returns_none() -> None:
    # The READ path is tolerant: a dead agent yields None so resolution falls
    # through to the visual ladder (never raises, never a wrong locator).
    from openadapt_flow.ir import StructuralLocator

    backend = WindowsBackend("http://127.0.0.1:9", timeout_s=0.5)
    assert backend.structural_locator_at(10, 10) is None
    assert backend.structured_text_at(10, 10) is None
    assert backend.locate_structural(StructuralLocator(automation_id="x")) is None


def test_structural_locator_empty_uia_returns_none(
    waa: MockWaa, backend: WindowsBackend
) -> None:
    # The mock server does not echo the UIA sentinel, so the read decodes to
    # nothing -> None (empty UIA result is not a silent wrong locator).
    assert backend.structural_locator_at(10, 10) is None


def test_auth_header_sent_when_token_set() -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            seen["auth"] = self.headers.get("Authorization", "")
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps(
                {
                    "status": "delivered",
                    "receipt_id": "physical-1",
                    "operation": "physical_click",
                    "native": False,
                    "target_fingerprint": None,
                    "delivered_at": "2026-07-17T00:00:00+00:00",
                    "outcome_verified": False,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        WindowsBackend(url, auth_token="tok-123").click(1, 1)
        assert seen["auth"] == "Bearer tok-123"
    finally:
        server.shutdown()
        server.server_close()


# -- screenshot / viewport -------------------------------------------------------


def test_screenshot_returns_png_bytes(waa: MockWaa, backend: WindowsBackend) -> None:
    assert backend.screenshot() == waa.screens[0]


def test_viewport_derived_from_frame(backend: WindowsBackend) -> None:
    assert backend.viewport == VIEWPORT


def test_screenshot_retries_then_succeeds(
    waa: MockWaa, backend: WindowsBackend
) -> None:
    waa.screenshot_failures = 2
    assert backend.screenshot() == waa.screens[0]


def test_screenshot_fails_after_retries(waa: MockWaa) -> None:
    backend = WindowsBackend(
        waa.url,
        screenshot_max_retries=2,
        screenshot_retry_delay_s=0.01,
        allow_legacy_exec=True,
    )
    waa.screenshot_failures = 5
    with pytest.raises(RuntimeError, match="screenshot failed after 2"):
        backend.screenshot()
    waa.screenshot_failures = 0


# -- click ----------------------------------------------------------------------


def test_click_command(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.click(10, 20)
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("click", (10, 20), {})]


def test_double_click_command(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.click(10, 20, double=True)
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("doubleClick", (10, 20), {})]


def test_execute_error_raises(waa: MockWaa) -> None:
    backend = WindowsBackend(
        waa.url + "/missing",
        screenshot_retry_delay_s=0.01,
        allow_legacy_exec=True,
    )
    with pytest.raises(RuntimeError):
        backend.click(1, 1)


# -- type_text -------------------------------------------------------------------


def test_type_text_ascii_with_hostile_quoting(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = 'O\'Brien "note" C:\\path\\to\\file'
    backend.type_text(text)
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("write", (text,), {"interval": 0.05})]


def test_type_text_empty_sends_nothing(waa: MockWaa, backend: WindowsBackend) -> None:
    backend.type_text("")
    assert waa.commands == []


def test_type_text_non_ascii_uses_clipboard(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Nöte — ünïcode"
    backend.type_text(text)

    runs: list[tuple] = []
    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = lambda *a, **k: runs.append((a, k))
    fake = exec_last_command(waa, monkeypatch, fake_subprocess=fake_subprocess)

    # pyautogui.write would silently drop these characters (a wrong-write
    # mode): the value must instead reach PowerShell Set-Clipboard base64
    # -intact and be pasted with Ctrl+V.
    assert fake.calls == [("hotkey", ("ctrl", "v"), {})]
    (argv,), _ = runs[0][0], runs[0][1]
    assert argv[0] == "powershell"
    ps = argv[-1]
    assert "Set-Clipboard" in ps
    b64 = re.search(r"FromBase64String\('([^']+)'\)", ps).group(1)
    assert base64.b64decode(b64).decode("utf-8") == text


# -- press -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        ("Enter", ["enter"]),
        ("Escape", ["esc"]),
        ("ArrowDown", ["down"]),
        ("ControlOrMeta+a", ["ctrl", "a"]),
        ("Meta+d", ["win", "d"]),
        ("Ctrl+Shift+Escape", ["ctrl", "shift", "esc"]),
    ],
)
def test_normalize_chord(chord: str, expected: list[str]) -> None:
    assert normalize_chord(chord) == expected


def test_press_single_key(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.press("Enter")
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("press", ("enter",), {})]


def test_press_chord(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.press("ControlOrMeta+a")
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("hotkey", ("ctrl", "a"), {})]


# -- scroll ----------------------------------------------------------------------


def test_scroll_down_converts_pixels_and_sign(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.scroll(0, 400)  # view down -> pyautogui negative notches
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("scroll", (-4,), {})]


def test_scroll_up(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.scroll(0, -250)
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("scroll", (2,), {})]


def test_scroll_small_delta_still_scrolls(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.scroll(0, 30)  # sub-notch pixel deltas round up to one notch
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("scroll", (-1,), {})]


def test_scroll_horizontal(
    waa: MockWaa, backend: WindowsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.scroll(120, 0)
    fake = exec_last_command(waa, monkeypatch)
    assert fake.calls == [("hscroll", (1,), {})]


def test_scroll_zero_sends_nothing(waa: MockWaa, backend: WindowsBackend) -> None:
    backend.scroll(0, 0)
    assert waa.commands == []


# -- record -> compile -> replay conformance (no compiler/replayer changes) ------


@pytest.mark.timeout(300)
def test_record_compile_replay_over_windows_backend(
    waa: MockWaa, backend: WindowsBackend, tmp_path
) -> None:
    """The unmodified Recorder, compiler and Replayer drive the
    WindowsBackend end to end against the mock WAA app."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    recording_dir = tmp_path / "recording"
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"

    # Record the demonstration through the WindowsBackend.
    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.02,
        settle_timeout_s=2.0,
    )
    recorder.click(*BUTTON_CENTER)
    recorder.type_text(NOTE_VALUE, param="note")
    recorder.press("Enter")
    recorder.finish()
    assert waa.state == 3  # the mock app reached its final state

    meta = json.loads((recording_dir / "meta.json").read_text())
    assert meta["viewport"] == list(VIEWPORT)
    assert meta["params"] == {"note": NOTE_VALUE}

    # Compile — unchanged compiler must accept the desktop recording.
    workflow = compile_recording(recording_dir, bundle_dir, name="win-smoke")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]

    # Replay against a fresh app state — unchanged replayer, real vision.
    waa.reset()
    report = Replayer(backend, poll_interval_s=0.02).run(
        workflow,
        params={"note": NOTE_VALUE},
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    assert report.success, [r.model_dump() for r in report.results]
    # Reference-bar invariant: a HEALTHY replay resolves from retained
    # evidence with ZERO model calls (docs/LIMITS.md "Healthy replay with
    # zero model calls"). The Windows/UIA substrate must meet the same bar
    # the browser reference path and the Linux qualification already assert;
    # a nonzero count here would mean the deterministic ladder silently fell
    # through to the optional grounder on a clean run.
    assert report.model_calls == 0, report.model_calls
    assert waa.state == 3
