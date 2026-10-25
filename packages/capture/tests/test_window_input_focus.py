"""Window-scoped recording keeps only input the target window received.

These tests drive the real ``record()`` pipeline (journal, input delivery,
screen reader, and the SQLite writer processes) with display-free fakes: the
window resolver, capturer and focus probe come from ``FakePlatform`` and the
native input observer is a ``ThreadedInputObserver`` the test feeds directly.
They run headless on Linux, macOS and Windows.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import openadapt_capture.recorder as recorder_module
from openadapt_capture import utils
from openadapt_capture.input_observer import (
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    ThreadedInputObserver,
)
from openadapt_capture.recorder import (
    WINDOW_INPUT_GAPS_FILENAME,
    WindowInputGapLog,
)
from openadapt_capture.window_capture import (
    FOCUS_EVIDENCE_PROCESS,
    FOCUS_EVIDENCE_WINDOW,
    INPUT_KEYBOARD_FOCUS_ELSEWHERE,
    INPUT_KEYBOARD_FOCUS_IN_DIALOG,
    INPUT_KEYBOARD_FOCUS_UNKNOWN,
    INPUT_POINTER_OVER_OTHER_WINDOW,
    INPUT_POINTER_TARGET_UNKNOWN,
    INPUT_TARGET_NOT_ON_SCREEN,
    WindowCaptureScope,
    WindowInputNotOnTarget,
    WindowTarget,
)
from openadapt_capture.window_input_focus import FocusOwner
from tests.test_window_capture import TARGET, FakePlatform

SLACK = FocusOwner(window_id=7, pid=555)
PASSWORD_DIALOG = FocusOwner(window_id=8, pid=666)
# The fake target is window 42 of process 1234.
SECOND_TARGET_APP_WINDOW = FocusOwner(window_id=43, pid=1234)
TARGET_APP_PASSWORD_WINDOW = FocusOwner(window_id=44, pid=1234)
TARGET_APP_PASSWORD_SHEET = FocusOwner(window_id=42, pid=1234, dialog=True)
# Inside the fake target's rectangle (300, 150, 800, 600).
INSIDE = (400.0, 300.0)


class FedObserver(ThreadedInputObserver):
    """A native observer whose events the test emits through the receipt path."""

    def __init__(self, callback):
        super().__init__(
            callback,
            observe_keyboard=True,
            observe_mouse=True,
            capture_mouse_moves=True,
            shutdown_timeout=2.0,
        )
        self._release_loop = threading.Event()

    def _setup(self):
        return

    def _run_loop(self):
        self._release_loop.wait()

    def _teardown(self):
        return

    def _wake(self):
        self._release_loop.set()

    def feed(self, event) -> None:
        """Emit one event as a native hook would and wait for its delivery."""
        receipt = self._reserve_receipt(event.timestamp)
        self._emit(event, receipt=receipt)
        deadline = time.monotonic() + 10
        while not bool(getattr(receipt, "finished", False)):
            if time.monotonic() >= deadline:
                raise AssertionError("native receipt was not completed")
            time.sleep(0.001)


def _now() -> float:
    return utils.get_timestamp()


def key(char: str, pressed: bool, *, name: str | None = None) -> ObservedKey:
    return ObservedKey(
        pressed=pressed,
        key_name=name,
        key_char=char,
        key_vk=name or char,
        canonical_key_name=name,
        canonical_key_char=char,
        canonical_key_vk=name or char,
        timestamp=_now(),
    )


def type_text(observer: FedObserver, text: str, *, canary: str | None = None) -> None:
    for index, char in enumerate(text):
        name = None if canary is None else f"{canary}-{index}"
        observer.feed(key(char, True, name=name))
        observer.feed(key(char, False, name=name))


def click(observer: FedObserver, point=INSIDE) -> None:
    for pressed in (True, False):
        observer.feed(
            ObservedMouseButton(
                x=point[0],
                y=point[1],
                button="left",
                pressed=pressed,
                timestamp=_now(),
            )
        )


class WindowRecording:
    """Run ``record()`` in window mode against ``FakePlatform``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakePlatform):
        self.fake = fake
        self.capture_dir = tmp_path / "capture"
        self.observers: list[FedObserver] = []
        self.error: BaseException | None = None
        self.terminate_processing = multiprocessing.Event()
        self.terminate_recording = multiprocessing.Event()
        self._status_recv, self._status_send = multiprocessing.Pipe(duplex=False)

        for name, value in {
            "RECORD_VIDEO": False,
            "RECORD_IMAGES": True,
            "RECORD_AUDIO": False,
            "RECORD_BROWSER_EVENTS": False,
            "RECORD_WINDOW_DATA": False,
            "RECORD_STRUCTURAL_OBSERVATIONS": False,
            "RECORD_READ_ACTIVE_ELEMENT_STATE": False,
            "PLOT_PERFORMANCE": False,
            "SCREEN_CAPTURE_FPS": 20.0,
        }.items():
            monkeypatch.setattr(recorder_module.config, name, value)

        def build_scope(owner, title, **_kwargs):
            return WindowCaptureScope(
                WindowTarget(owner=owner, title=title),
                resolver=fake.resolver,
                capturer=fake.capturer,
                focus_probe=fake,
            )

        def create_observer(callback, **_kwargs):
            observer = FedObserver(callback)
            self.observers.append(observer)
            return observer

        # Headless: no display metric or screen grab may be read.
        monkeypatch.setattr(recorder_module.utils, "get_monitor_dims", lambda: (1920, 1080))
        monkeypatch.setattr(recorder_module.utils, "get_double_click_distance_pixels", lambda: 5)
        monkeypatch.setattr(recorder_module.utils, "get_double_click_interval_seconds", lambda: 0.5)
        monkeypatch.setattr(recorder_module.platform, "get_display_pixel_ratio", lambda: 2.0)

        def no_display(*_args, **_kwargs):
            raise AssertionError("window-scoped recording read the physical display")

        monkeypatch.setattr(recorder_module.utils, "take_screenshot", no_display)
        monkeypatch.setattr(recorder_module.utils, "get_process_local_sct", no_display)
        monkeypatch.setattr(recorder_module, "build_window_scope", build_scope)
        monkeypatch.setattr(recorder_module, "create_input_observer", create_observer)
        monkeypatch.setattr(
            recorder_module.DesktopCaptureScope,
            "current",
            lambda: SimpleNamespace(
                snapshot=lambda: {
                    "schema_version": "openadapt.capture.display-topology/v1",
                    "topology_sha256": "a" * 64,
                },
                assert_current=lambda **_kwargs: None,
            ),
        )

    def __enter__(self) -> FedObserver:
        def run():
            try:
                recorder_module.record(
                    "window input focus",
                    capture_dir=str(self.capture_dir),
                    terminate_processing=self.terminate_processing,
                    terminate_recording=self.terminate_recording,
                    status_pipe=self._status_send,
                    window_owner="FakeApp",
                )
            except BaseException as exc:  # surfaced by __exit__
                self.error = exc

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.error is not None:
                raise self.error
            if self._status_recv.poll(0.05):
                if self._status_recv.recv().get("type") == "record.started":
                    break
        else:
            raise AssertionError("record() did not start")
        assert len(self.observers) == 1
        return self.observers[0]

    def __exit__(self, exc_type, exc, traceback) -> None:
        # Let the screen reader publish an after-frame for the last action.
        time.sleep(0.3)
        self.terminate_processing.set()
        self._thread.join(timeout=90)
        assert not self._thread.is_alive(), "record() did not stop"
        if exc_type is None and self.error is not None:
            raise self.error

    def actions(self) -> list[dict]:
        db_path = self.capture_dir / "recording.db"
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM action_event ORDER BY source_ordinal"
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def gaps(self) -> list[dict]:
        path = self.capture_dir / WINDOW_INPUT_GAPS_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_nowhere(self, *needles: str) -> None:
        """No file the recording wrote may contain any ``needles``."""
        files = [path for path in self.capture_dir.rglob("*") if path.is_file()]
        assert files, "the recording wrote no files"
        for path in files:
            data = path.read_bytes()
            for needle in needles:
                for encoded in (needle.encode("utf-8"), needle.encode("utf-16-le")):
                    assert encoded not in data, f"{needle!r} was stored in {path.name}"


def _key_rows(actions: list[dict]) -> list[dict]:
    return [row for row in actions if row["name"] in ("press", "release")]


def _click_rows(actions: list[dict]) -> list[dict]:
    return [row for row in actions if row["name"] == "click"]


@pytest.fixture
def fake():
    return FakePlatform()


def test_alt_tab_and_type_drops_keys_and_never_stores_them(tmp_path, monkeypatch, fake, capfd):
    canary = f"pw-canary-{uuid.uuid4().hex}"
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        type_text(observer, "ok")
        fake.keyboard_owner = SLACK  # the user switched to another app
        type_text(observer, "hunter2", canary=canary)
        fake.keyboard_owner = TARGET
        type_text(observer, "go")

    keys = _key_rows(recording.actions())
    assert "".join(row["key_char"] for row in keys if row["name"] == "press") == "okgo"
    assert all(row["key_name"] is None or canary not in row["key_name"] for row in keys)
    gaps = recording.gaps()
    left = [gap for gap in gaps if gap["event"] == "focus_left_target"]
    assert len(left) == 1
    assert left[0]["reason"] == INPUT_KEYBOARD_FOCUS_ELSEWHERE
    assert left[0]["input"] == "keyboard"
    assert isinstance(left[0]["timestamp"], float)
    assert set(left[0]) == {"schema_version", "event", "reason", "input", "timestamp"}
    assert [gap["event"] for gap in gaps] == ["focus_left_target", "focus_returned_to_target"]
    recording.assert_nowhere(canary, "hunter2")
    output = capfd.readouterr()
    assert canary not in output.out + output.err


def test_click_on_overlapping_window_inside_target_rectangle_is_dropped(
    tmp_path, monkeypatch, fake
):
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        click(observer)
        fake.pointer_owner = SLACK  # another window covers this point
        click(observer)
        observer.feed(
            ObservedMouseScroll(x=INSIDE[0], y=INSIDE[1], dx=0.0, dy=-3.0, timestamp=_now())
        )
        fake.pointer_owner = TARGET

    actions = recording.actions()
    clicks = _click_rows(actions)
    assert [row["mouse_pressed"] for row in clicks] == [1, 0]
    assert not [row for row in actions if row["name"] == "scroll"]
    (left,) = [gap for gap in recording.gaps() if gap["event"] == "focus_left_target"]
    assert left["reason"] == INPUT_POINTER_OVER_OTHER_WINDOW
    assert left["input"] == "pointer"
    assert left["coarse_point"] == [400.0, 300.0]


def test_password_dialog_over_target_drops_keys_and_clicks(tmp_path, monkeypatch, fake):
    canary = f"pw-canary-{uuid.uuid4().hex}"
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        click(observer)
        # A different process's password prompt is topmost and focused.
        fake.keyboard_owner = PASSWORD_DIALOG
        fake.pointer_owner = PASSWORD_DIALOG
        click(observer)
        type_text(observer, "s3cr3t!", canary=canary)
        observer.feed(key("\r", True, name="enter"))
        # The dialog closes before the Enter release is delivered.
        fake.keyboard_owner = TARGET
        fake.pointer_owner = TARGET
        observer.feed(key("\r", False, name="enter"))
        click(observer)

    actions = recording.actions()
    assert _key_rows(actions) == []  # the release of a dropped press is dropped too
    assert len(_click_rows(actions)) == 4
    reasons = [gap["reason"] for gap in recording.gaps() if gap["event"] == "focus_left_target"]
    assert reasons == [INPUT_POINTER_OVER_OTHER_WINDOW, INPUT_KEYBOARD_FOCUS_ELSEWHERE]
    recording.assert_nowhere(canary, "s3cr3t!")


@pytest.mark.parametrize("failure", [None, RuntimeError("probe failed")], ids=["none", "raises"])
def test_unknown_focus_fails_closed(tmp_path, monkeypatch, fake, failure):
    canary = f"pw-canary-{uuid.uuid4().hex}"
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        fake.keyboard_owner = failure
        fake.pointer_owner = failure
        type_text(observer, "abc", canary=canary)
        click(observer)
        fake.keyboard_owner = TARGET
        fake.pointer_owner = TARGET

    actions = recording.actions()
    assert _key_rows(actions) == []
    assert _click_rows(actions) == []
    reasons = [gap["reason"] for gap in recording.gaps() if gap["event"] == "focus_left_target"]
    assert reasons == [INPUT_KEYBOARD_FOCUS_UNKNOWN, INPUT_POINTER_TARGET_UNKNOWN]
    recording.assert_nowhere(canary)


def test_input_on_target_is_recorded_as_before(tmp_path, monkeypatch, fake):
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        observer.feed(ObservedMouseMove(x=310.0, y=170.0, timestamp=_now()))
        click(observer, (310.0, 170.0))
        type_text(observer, "hi")

    actions = recording.actions()
    assert [row["name"] for row in actions] == [
        "move",
        "click",
        "click",
        "press",
        "release",
        "press",
        "release",
    ]
    first_click = _click_rows(actions)[0]
    assert (first_click["mouse_x"], first_click["mouse_y"]) == (20.0, 40.0)
    assert all(row["window_geometry_generation"] is not None for row in actions)
    assert recording.gaps() == []
    assert not (recording.capture_dir / WINDOW_INPUT_GAPS_FILENAME).exists()


def test_focus_change_between_receipt_and_delivery_is_dropped(tmp_path, monkeypatch, fake):
    """Focus read inside the native hook is binding even if it returns later."""
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        fake.receipt_keyboard_owner = PASSWORD_DIALOG
        observer.feed(key("x", True))
        observer.feed(key("x", False))
        fake.receipt_keyboard_owner = None

    assert _key_rows(recording.actions()) == []


def test_release_of_an_accepted_press_survives_a_focus_change(tmp_path, monkeypatch, fake):
    """Cmd+Tab: the press reached the target, so the release is kept."""
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        observer.feed(key("", True, name="cmd"))
        observer.feed(key("\t", True, name="tab"))
        fake.keyboard_owner = SLACK
        observer.feed(key("\t", False, name="tab"))
        observer.feed(key("", False, name="cmd"))

    keys = _key_rows(recording.actions())
    assert [(row["name"], row["key_name"]) for row in keys] == [
        ("press", "cmd"),
        ("press", "tab"),
        ("release", "tab"),
        ("release", "cmd"),
    ]
    assert recording.gaps() == []


def test_second_window_of_the_target_app_drops_keys_and_never_stores_them(
    tmp_path, monkeypatch, fake
):
    """A second browser window shares the target's process but is not it."""
    canary = f"pw-canary-{uuid.uuid4().hex}"
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        type_text(observer, "ok")
        fake.keyboard_owner = SECOND_TARGET_APP_WINDOW
        type_text(observer, "hunter2", canary=canary)
        fake.keyboard_owner = TARGET
        type_text(observer, "go")

    keys = _key_rows(recording.actions())
    assert "".join(row["key_char"] for row in keys if row["name"] == "press") == "okgo"
    reasons = [gap["reason"] for gap in recording.gaps() if gap["event"] == "focus_left_target"]
    assert reasons == [INPUT_KEYBOARD_FOCUS_ELSEWHERE]
    recording.assert_nowhere(canary, "hunter2")


@pytest.mark.parametrize(
    ("dialog", "reason"),
    [
        (TARGET_APP_PASSWORD_WINDOW, INPUT_KEYBOARD_FOCUS_ELSEWHERE),
        (TARGET_APP_PASSWORD_SHEET, INPUT_KEYBOARD_FOCUS_IN_DIALOG),
    ],
    ids=["own-window", "sheet-on-target"],
)
def test_password_dialog_of_the_target_app_drops_keys(tmp_path, monkeypatch, fake, dialog, reason):
    """The target app's own password prompt never counts as the target.

    A prompt in its own window has another window id. A sheet attached to the
    target reports the target's window id and is flagged as a dialog.
    """
    canary = f"pw-canary-{uuid.uuid4().hex}"
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        fake.keyboard_owner = dialog
        type_text(observer, "s3cr3t!", canary=canary)
        fake.keyboard_owner = TARGET
        type_text(observer, "x")

    keys = _key_rows(recording.actions())
    assert [row["key_char"] for row in keys if row["name"] == "press"] == ["x"]
    reasons = [gap["reason"] for gap in recording.gaps() if gap["event"] == "focus_left_target"]
    assert reasons == [reason]
    recording.assert_nowhere(canary, "s3cr3t!")


def test_process_only_focus_is_kept_and_marked_as_weaker_evidence(tmp_path, monkeypatch, fake):
    """Where the OS names no window, a process match is kept and marked."""
    recording = WindowRecording(tmp_path, monkeypatch, fake)
    with recording as observer:
        type_text(observer, "a")
        fake.keyboard_owner = FocusOwner(window_id=None, pid=fake.pid)
        type_text(observer, "bc")
        fake.keyboard_owner = TARGET
        type_text(observer, "d")

    keys = _key_rows(recording.actions())
    assert "".join(row["key_char"] for row in keys if row["name"] == "press") == "abcd"
    evidence = [gap for gap in recording.gaps() if gap["event"] == "keyboard_focus_evidence"]
    assert [gap["evidence"] for gap in evidence] == [FOCUS_EVIDENCE_PROCESS, FOCUS_EVIDENCE_WINDOW]
    assert all(
        set(gap) == {"schema_version", "event", "evidence", "timestamp"} for gap in evidence
    )
    recording.assert_nowhere("bc")


# ---------------------------------------------------------------------------
# Scope-level decisions (no pipeline)
# ---------------------------------------------------------------------------


@pytest.fixture
def scope(fake):
    result = WindowCaptureScope(
        WindowTarget(owner="FakeApp"),
        resolver=fake.resolver,
        capturer=fake.capturer,
        focus_probe=fake,
    )
    result.bind_display_topology(
        {
            "schema_version": "openadapt.capture.display-topology/v1",
            "topology_sha256": "a" * 64,
        },
        lambda **_kwargs: None,
    )
    result.capture_frame()
    return result


def test_offscreen_visibility_independent_target_refuses_clicks(fake):
    fake.on_screen = False
    fake.visibility_independent = True
    window = fake.resolver(WindowTarget(owner="FakeApp"))
    scope = WindowCaptureScope(
        WindowTarget(owner="FakeApp"),
        resolver=fake.resolver,
        capturer=fake.capturer,
        focus_probe=fake,
    )
    with pytest.raises(WindowInputNotOnTarget) as excinfo:
        scope.assert_input_on_target(window, point=INSIDE)
    assert excinfo.value.reason == INPUT_TARGET_NOT_ON_SCREEN


def test_keyboard_focus_on_target_process_is_accepted(scope, fake):
    fake.keyboard_owner = FocusOwner(window_id=None, pid=fake.pid)
    assert isinstance(scope.generation_for_action(), int)


def test_keyboard_focus_evidence_names_window_or_process(scope, fake):
    window = scope.reserve_action_geometry()[0]
    assert scope.assert_input_on_target(window) == FOCUS_EVIDENCE_WINDOW
    fake.keyboard_owner = FocusOwner(window_id=None, pid=fake.pid)
    assert scope.assert_input_on_target(window) == FOCUS_EVIDENCE_PROCESS
    # A pid-only receipt reading plus a window reading at delivery is
    # window evidence (the macOS event tap reads only the process).
    fake.keyboard_owner = TARGET
    assert (
        scope.assert_input_on_target(
            window, keyboard_focus_at_receipt=FocusOwner(pid=fake.pid)
        )
        == FOCUS_EVIDENCE_WINDOW
    )


def test_a_named_window_never_falls_back_to_the_process(scope, fake):
    window = scope.reserve_action_geometry()[0]
    fake.keyboard_owner = SECOND_TARGET_APP_WINDOW
    with pytest.raises(WindowInputNotOnTarget) as excinfo:
        scope.assert_input_on_target(window)
    assert excinfo.value.reason == INPUT_KEYBOARD_FOCUS_ELSEWHERE
    fake.keyboard_owner = TARGET
    with pytest.raises(WindowInputNotOnTarget) as excinfo:
        scope.assert_input_on_target(
            window, keyboard_focus_at_receipt=SECOND_TARGET_APP_WINDOW
        )
    assert excinfo.value.reason == INPUT_KEYBOARD_FOCUS_ELSEWHERE


def test_legacy_translation_refuses_other_window(scope, fake):
    fake.pointer_owner = SLACK
    with pytest.raises(WindowInputNotOnTarget):
        scope.translate(*INSIDE)
    fake.pointer_owner = TARGET
    assert scope.translate(310.0, 170.0) == (20.0, 40.0)


def test_assert_current_does_not_need_focus(scope, fake):
    """Stopping from another app's UI must not fail the recording."""
    fake.keyboard_owner = SLACK
    fake.pointer_owner = SLACK
    scope.assert_current()


def test_gap_log_never_records_more_than_reason_kind_time_and_coarse_point(tmp_path):
    log = WindowInputGapLog(tmp_path)
    log.left_target(INPUT_POINTER_OVER_OTHER_WINDOW, "pointer", 1.5, (123.4, 456.7))
    log.left_target(INPUT_POINTER_OVER_OTHER_WINDOW, "pointer", 1.6, (999.0, 999.0))
    log.returned(2.0)
    lines = (tmp_path / WINDOW_INPUT_GAPS_FILENAME).read_text().splitlines()
    assert [json.loads(line) for line in lines] == log.records
    assert log.records[0]["coarse_point"] == [120.0, 460.0]
    assert len(log.records) == 2
    if os.name == "posix":
        assert (tmp_path / WINDOW_INPUT_GAPS_FILENAME).stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# Platform probes with faked native bindings
# ---------------------------------------------------------------------------


def _quartz_window(window_id, pid, bounds, *, layer=0, alpha=1.0):
    x, y, w, h = bounds
    return {
        "kCGWindowNumber": window_id,
        "kCGWindowOwnerPID": pid,
        "kCGWindowLayer": layer,
        "kCGWindowAlpha": alpha,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
    }


@pytest.fixture
def fake_macos(monkeypatch):
    import sys

    state = SimpleNamespace(frontmost_pid=1234, windows=[])
    quartz = SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=16,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda _options, _window: list(state.windows),
    )
    app = SimpleNamespace(processIdentifier=lambda: state.frontmost_pid)
    workspace = SimpleNamespace(frontmostApplication=lambda: app)
    appkit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
    )
    # Accessibility is unavailable unless a test names a focused window.
    state.ax_window = None  # (window number, subrole, focused element role)
    success = 0

    def copy_attribute(element, name, _out):
        if state.ax_window is None:
            return (-25211, None)  # kAXErrorAPIDisabled
        number, subrole, focused_role = state.ax_window
        values = {
            ("app", "AXFocusedWindow"): "window",
            ("app", "AXFocusedUIElement"): "focused",
            ("window", "AXRole"): "AXWindow",
            ("window", "AXSubrole"): subrole,
            ("focused", "AXRole"): focused_role,
            ("focused", "AXParent"): "window",
        }
        value = values.get((element, name))
        return (success, value) if value is not None else (-25212, None)

    applicationservices = SimpleNamespace(
        kAXErrorSuccess=success,
        AXUIElementCreateApplication=lambda _pid: "app",
        AXUIElementSetMessagingTimeout=lambda _element, _seconds: success,
        AXUIElementCopyAttributeValue=copy_attribute,
    )
    import openadapt_capture.window_input_focus as focus_module

    monkeypatch.setattr(
        focus_module,
        "_macos_ax_window_number",
        lambda element: state.ax_window[0] if element == "window" else None,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "ApplicationServices", applicationservices)
    return state


def test_macos_probe_requires_window_list_to_agree_with_frontmost_app(fake_macos):
    from openadapt_capture.window_input_focus import MacOSInputFocusProbe

    probe = MacOSInputFocusProbe()
    fake_macos.windows = [
        _quartz_window(1, 99, (0, 0, 2000, 25), layer=25),  # menu bar extra
        _quartz_window(42, 1234, (300, 150, 800, 600)),
    ]
    assert probe.keyboard_focus() == FocusOwner(window_id=42, pid=1234)
    # NSWorkspace can be stale; the front-most app window belongs to Slack.
    fake_macos.windows.insert(1, _quartz_window(7, 555, (0, 0, 500, 500)))
    assert probe.keyboard_focus() is None
    # Inside the native hook only the cheap read runs.
    assert probe.keyboard_focus(at_receipt=True) == FocusOwner(pid=1234)


def test_macos_probe_names_the_front_window_and_checks_accessibility(fake_macos):
    from openadapt_capture.window_input_focus import MacOSInputFocusProbe

    probe = MacOSInputFocusProbe()
    fake_macos.windows = [
        _quartz_window(43, 1234, (0, 0, 500, 500)),  # second window, in front
        _quartz_window(42, 1234, (300, 150, 800, 600)),
    ]
    # Without Accessibility the front-most window of the app is focused.
    assert probe.keyboard_focus() == FocusOwner(window_id=43, pid=1234)
    # Accessibility agrees.
    fake_macos.ax_window = (43, "AXStandardWindow", "AXTextField")
    assert probe.keyboard_focus() == FocusOwner(window_id=43, pid=1234)
    # Accessibility names another window: unknown, so input is refused.
    fake_macos.ax_window = (42, "AXStandardWindow", "AXTextField")
    assert probe.keyboard_focus() is None
    # A dialog window, or a sheet under the focused element, is a dialog.
    fake_macos.ax_window = (43, "AXDialog", "AXSecureTextField")
    assert probe.keyboard_focus() == FocusOwner(window_id=43, pid=1234, dialog=True)
    fake_macos.ax_window = (43, "AXStandardWindow", "AXSheet")
    assert probe.keyboard_focus() == FocusOwner(window_id=43, pid=1234, dialog=True)


def test_macos_probe_window_at_is_front_to_back_layer_zero(fake_macos):
    from openadapt_capture.window_input_focus import MacOSInputFocusProbe

    probe = MacOSInputFocusProbe()
    fake_macos.windows = [
        _quartz_window(3, 77, (0, 0, 2000, 2000), layer=101),  # overlay
        _quartz_window(4, 78, (0, 0, 2000, 2000), alpha=0.0),  # invisible
        _quartz_window(7, 555, (350, 250, 200, 200)),  # Slack over the target
        _quartz_window(42, 1234, (300, 150, 800, 600)),
    ]
    assert probe.window_at(400.0, 300.0) == FocusOwner(window_id=7, pid=555)
    assert probe.window_at(310.0, 170.0) == FocusOwner(window_id=42, pid=1234)
    assert probe.window_at(5000.0, 5000.0) is None


def test_windows_probe_uses_root_ancestor_of_foreground_and_point(monkeypatch):
    import ctypes

    try:
        import ctypes.wintypes  # noqa: F401
    except (ImportError, ValueError):
        pytest.skip("ctypes.wintypes is unavailable on this interpreter")
    from openadapt_capture.window_input_focus import WindowsInputFocusProbe

    class Function:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    roots = {101: 42, 202: 7}
    pids = {42: 1234, 7: 555}
    points = []

    def thread_process_id(hwnd, pid_ref):
        pid_ref._obj.value = pids[int(hwnd)]
        return 1

    def window_from_point(point):
        points.append((point.x, point.y))
        return 202

    user32 = SimpleNamespace(
        GetForegroundWindow=Function(lambda: 101),
        WindowFromPoint=Function(window_from_point),
        GetAncestor=Function(lambda hwnd, flag: roots[int(hwnd)] if flag == 2 else None),
        GetWindowThreadProcessId=Function(thread_process_id),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda _name: user32, raising=False)

    probe = WindowsInputFocusProbe()
    assert probe.keyboard_focus() == FocusOwner(window_id=42, pid=1234)
    assert probe.window_at(400.7, 300.2) == FocusOwner(window_id=7, pid=555)
    assert points == [(400, 300)]
    user32.GetForegroundWindow = Function(lambda: None)
    assert probe.keyboard_focus() is None


class _X11Reply(SimpleNamespace):
    def reply(self):
        return self


class _FakeX11Core:
    """QueryTree/GetWindowAttributes/GetGeometry over a fake window tree."""

    VIEWABLE = 2
    INPUT_OUTPUT = 1

    def __init__(self, tree, geometry, unmapped=()):
        self.tree = tree
        self.geometry = geometry
        self.unmapped = set(unmapped)

    def QueryTree(self, window):  # noqa: N802 - X11 request name
        return _X11Reply(children=list(self.tree.get(window, [])))

    def GetWindowAttributes(self, window):  # noqa: N802
        return _X11Reply(
            map_state=0 if window in self.unmapped else self.VIEWABLE,
            _class=self.INPUT_OUTPUT,
        )

    def GetGeometry(self, window):  # noqa: N802
        x, y, w, h = self.geometry[window]
        return _X11Reply(x=x, y=y, width=w, height=h, border_width=0)


def test_x11_client_window_at_maps_topmost_frame_to_its_client():
    from openadapt_capture.window_capture_linux import X11CompositeClient

    root = 1
    # Root children bottom-to-top: target frame 10 (client 42), Slack frame
    # 20 (client 7), then an unmapped frame 30 that covers everything.
    core = _FakeX11Core(
        tree={root: [10, 20, 30], 10: [11], 11: [42], 20: [7]},
        geometry={
            10: (300, 150, 800, 600),
            20: (350, 250, 200, 200),
            30: (0, 0, 1920, 1080),
        },
        unmapped={30},
    )
    client = object.__new__(X11CompositeClient)
    client._conn = SimpleNamespace(core=core)
    client._root = root
    client._xproto = SimpleNamespace(
        MapState=SimpleNamespace(Viewable=_FakeX11Core.VIEWABLE),
        WindowClass=SimpleNamespace(InputOutput=_FakeX11Core.INPUT_OUTPUT),
    )
    client.window_ids = lambda: [42, 7]

    assert client.client_window_at(400, 300) == 7
    assert client.client_window_at(310, 170) == 42
    assert client.client_window_at(1500, 900) is None


def test_x11_probe_reports_active_window_and_point_owner():
    from contextlib import nullcontext

    from openadapt_capture.window_input_focus import X11InputFocusProbe

    client = SimpleNamespace(
        active_window=lambda: 42,
        client_window_at=lambda x, y: 7 if (x, y) == (400, 300) else None,
        window_pid=lambda window_id: {42: 1234, 7: 555}.get(window_id),
    )
    probe = X11InputFocusProbe(client_factory=lambda: nullcontext(client))
    assert probe.keyboard_focus() == FocusOwner(window_id=42, pid=1234)
    assert probe.window_at(400.4, 300.9) == FocusOwner(window_id=7, pid=555)
    assert probe.window_at(0, 0) is None
