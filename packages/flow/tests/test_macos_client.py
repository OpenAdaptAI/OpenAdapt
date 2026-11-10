"""Offline tests for native macOS AppKit/AX coordination.

All framework modules are fakes: these tests never activate an application,
post input, capture a screen, or require macOS privacy permissions.
"""

from __future__ import annotations

import sys
import types
from typing import Callable, Optional

import pytest

from openadapt_flow.backends.remote_display import MacWindowClient, WindowInfo


def _window(*, owner: str = "TextEdit", title: str = "oa-trial.txt") -> WindowInfo:
    return WindowInfo(41, owner, title, 9001, (0, 0, 400, 300))


def test_find_windows_requires_exact_case_insensitive_identity(monkeypatch) -> None:
    quartz = types.ModuleType("Quartz")
    quartz.kCGWindowListOptionAll = 1
    quartz.kCGNullWindowID = 0
    quartz.CGWindowListCopyWindowInfo = lambda _opts, _window_id: [
        {
            "kCGWindowNumber": 41,
            "kCGWindowOwnerPID": 9001,
            "kCGWindowOwnerName": "TEXTEDIT",
            "kCGWindowName": "OA-TRIAL.TXT",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300},
            "kCGWindowIsOnscreen": True,
        },
        {
            "kCGWindowNumber": 42,
            "kCGWindowOwnerPID": 9002,
            "kCGWindowOwnerName": "TextEdit Helper",
            "kCGWindowName": "oa-trial.txt",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300},
            "kCGWindowIsOnscreen": True,
        },
    ]
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    matches = MacWindowClient().find_windows("TextEdit", "oa-trial.txt")

    assert [window.window_id for window in matches] == [41]


def test_activate_uses_source_aware_appkit_handoff(monkeypatch) -> None:
    calls: list[tuple] = []

    class RunningApp:
        def __init__(self, pid: int, *, hidden: bool = False) -> None:
            self.pid = pid
            self.hidden = hidden

        def processIdentifier(self) -> int:
            return self.pid

        def isHidden(self) -> bool:
            return self.hidden

        def unhide(self) -> None:
            calls.append(("unhide", self.pid))

        def activateFromApplication_options_(self, source, options) -> bool:
            calls.append(("transfer", source.pid, options))
            return True

        def activateWithOptions_(self, options) -> bool:
            calls.append(("activate", options))
            return True

    source = RunningApp(777)
    target = RunningApp(9001, hidden=True)
    frontmost = [source]

    def transfer(self, source_app, options):
        calls.append(("transfer", source_app.pid, options))
        frontmost[0] = self
        return True

    target.activateFromApplication_options_ = types.MethodType(transfer, target)
    appkit = types.ModuleType("AppKit")
    appkit.NSApplicationActivateAllWindows = 1
    appkit.NSApplicationActivateIgnoringOtherApps = 2
    appkit.NSRunningApplication = types.SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda pid: (
            target if pid == target.pid else None
        )
    )
    appkit.NSWorkspace = types.SimpleNamespace(
        sharedWorkspace=lambda: types.SimpleNamespace(
            frontmostApplication=lambda: frontmost[0]
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    MacWindowClient().activate(target.pid)

    assert calls == [("unhide", 9001), ("transfer", 777, 3)]


def test_activate_falls_back_to_exact_pid_system_events_when_appkit_denied(
    monkeypatch,
) -> None:
    calls: list[tuple] = []

    class RunningApp:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def processIdentifier(self) -> int:
            return self.pid

        def isHidden(self) -> bool:
            return False

        def activateFromApplication_options_(self, source, options) -> bool:
            calls.append(("transfer", source.pid, options))
            return False

        def activateWithOptions_(self, options) -> bool:
            calls.append(("activate", options))
            return True

    source = RunningApp(777)
    target = RunningApp(9001)
    appkit = types.ModuleType("AppKit")
    appkit.NSApplicationActivateAllWindows = 1
    appkit.NSApplicationActivateIgnoringOtherApps = 2
    appkit.NSRunningApplication = types.SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda _pid: target
    )
    appkit.NSWorkspace = types.SimpleNamespace(
        sharedWorkspace=lambda: types.SimpleNamespace(
            frontmostApplication=lambda: source
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    subprocess_calls = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    MacWindowClient().activate(target.pid)

    assert calls == [("transfer", 777, 3), ("activate", 3)]
    assert subprocess_calls[0][0][0:2] == ["osascript", "-e"]
    assert "unix id is 9001" in subprocess_calls[0][0][2]


def _focused_app_module(
    *,
    copy_error: int = 0,
    focused_app: object | None = None,
    pid_error: int = 0,
    pid=9001,
):
    module = types.ModuleType("ApplicationServices")
    module.kAXFocusedApplicationAttribute = "focused-application"
    system = object()
    app = object() if focused_app is None and copy_error == 0 else focused_app
    module.AXUIElementCreateSystemWide = lambda: system
    module.AXUIElementCopyAttributeValue = lambda element, attribute, _error: (
        (copy_error, app)
        if (element, attribute) == (system, "focused-application")
        else (1, None)
    )
    module.AXUIElementGetPid = lambda element, _error: (
        (pid_error, pid) if element is app else (1, None)
    )
    return module


@pytest.mark.parametrize("pid", [9001, 777])
def test_ax_focused_application_pid_returns_exact_framework_pid(
    monkeypatch, pid
) -> None:
    module = _focused_app_module(pid=pid)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().focused_application_pid() == pid


@pytest.mark.parametrize(
    "module",
    [
        _focused_app_module(copy_error=1),
        _focused_app_module(focused_app=object(), pid_error=1),
        _focused_app_module(pid=0),
    ],
)
def test_ax_focused_application_pid_errors_fail_closed(monkeypatch, module) -> None:
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().focused_application_pid() is None


def test_ax_focused_application_pid_unavailable_fails_closed(monkeypatch) -> None:
    module = _focused_app_module(copy_error=0, focused_app=None)
    # Explicitly override the helper's default successful app with unavailable.
    module.AXUIElementCopyAttributeValue = lambda _element, _attribute, _error: (
        0,
        None,
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().focused_application_pid() is None


def test_window_id_at_point_includes_nonzero_layer_overlay(monkeypatch) -> None:
    quartz = types.ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 2
    quartz.kCGNullWindowID = 0
    quartz.CGWindowListCopyWindowInfo = lambda _opts, _window_id: [
        {
            "kCGWindowNumber": 99,
            "kCGWindowLayer": 9,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 100, "Height": 100},
        },
        {
            "kCGWindowNumber": 41,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300},
        },
    ]
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    client = MacWindowClient()
    assert client.window_id_at_point(50, 50) == 99
    assert client.window_id_at_point(500, 500) is None


def _quartz_with(windows: list[dict]) -> types.ModuleType:
    quartz = types.ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 2
    quartz.kCGNullWindowID = 0
    quartz.CGWindowListCopyWindowInfo = lambda _opts, _window_id: windows
    return quartz


def _hit_module(pid_at: Callable[[float, float], Optional[int]]) -> types.ModuleType:
    module = types.ModuleType("ApplicationServices")
    module.AXUIElementCreateSystemWide = lambda: "system"

    def at_position(_system, x, y, _out):
        pid = pid_at(x, y)
        return (0, ("element", pid)) if pid is not None else (-25204, None)

    module.AXUIElementCopyElementAtPosition = at_position
    module.AXUIElementGetPid = lambda element, _out: (0, element[1])
    return module


#: macOS 15: the Dock's full-screen window (layer 20), a notification banner's
#: full-screen Notification Center window (layer 23), and the app's window.
_DOCK = {
    "kCGWindowNumber": 7,
    "kCGWindowOwnerPID": 70,
    "kCGWindowLayer": 20,
    "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1024, "Height": 768},
}
_BANNER = {
    "kCGWindowNumber": 15,
    "kCGWindowOwnerPID": 150,
    "kCGWindowLayer": 23,
    "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1024, "Height": 768},
}
_APP = {
    "kCGWindowNumber": 41,
    "kCGWindowOwnerPID": 410,
    "kCGWindowLayer": 0,
    "kCGWindowBounds": {"X": 160, "Y": 220, "Width": 560, "Height": 388},
}


def test_a_full_screen_pass_through_window_is_skipped_only_on_the_hit_test(
    monkeypatch,
) -> None:
    monkeypatch.setitem(sys.modules, "Quartz", _quartz_with([_BANNER, _DOCK, _APP]))
    client = MacWindowClient()

    # The hit test says the click goes to the app: the Dock and Notification
    # Center windows in front are pass-through there.
    monkeypatch.setitem(
        sys.modules, "ApplicationServices", _hit_module(lambda x, y: 410)
    )
    assert client.window_id_at_point(250, 350) == 41

    # Over the banner the hit test names Notification Center: the click goes
    # to the banner, so the app is covered and its input is refused.
    monkeypatch.setitem(
        sys.modules, "ApplicationServices", _hit_module(lambda x, y: 150)
    )
    assert client.window_id_at_point(250, 350) == 15

    # The hit test names a process with no window at the point: the frontmost
    # window answers, as without a hit test.
    monkeypatch.setitem(
        sys.modules, "ApplicationServices", _hit_module(lambda x, y: 999)
    )
    assert client.window_id_at_point(250, 350) == 15

    # No hit test answer: the frontmost window answers (fails closed).
    monkeypatch.setitem(
        sys.modules, "ApplicationServices", _hit_module(lambda x, y: None)
    )
    assert client.window_id_at_point(250, 350) == 15
    monkeypatch.setitem(
        sys.modules, "ApplicationServices", types.ModuleType("ApplicationServices")
    )
    assert client.window_id_at_point(250, 350) == 15


def _ax_module(
    *,
    focused_title: str = "oa-trial.txt",
    target_title: str = "oa-trial.txt",
    duplicate: bool = False,
    duplicate_title: str | None = None,
    focused_window_matches: bool = True,
    is_main: bool = True,
    selected_settable: bool = True,
    stale_top_level: bool = False,
    set_result: int = 0,
    value_settable: bool = False,
    value: str | None = None,
    selected_range: tuple[int, int] | None = None,
    readback: str | None = None,
    parent_walk: bool = False,
    walk_reaches_app: bool = False,
    value_drifts: bool = False,
):
    module = types.ModuleType("ApplicationServices")
    module.kAXWindowsAttribute = "windows"
    module.kAXTitleAttribute = "title"
    module.kAXFocusedWindowAttribute = "focused-window"
    module.kAXMainAttribute = "main"
    module.kAXFocusedAttribute = "focused"
    module.kAXRaiseAction = "raise"
    module.kAXFocusedUIElementAttribute = "focused-element"
    module.kAXTopLevelUIElementAttribute = "top-level"
    module.kAXSelectedTextAttribute = "selected-text"
    module.kAXValueAttribute = "value"
    module.kAXSelectedTextRangeAttribute = "selected-range"
    module.kAXValueCFRangeType = "cf-range"
    module.kAXParentAttribute = "parent"
    module.kAXRoleAttribute = "role"
    module.AXValueCreate = lambda kind, raw: (kind, tuple(raw))
    module.AXValueGetValue = lambda raw, kind, _out: (raw[0] == kind, raw[1])

    app = object()
    target = object()
    duplicate_target = object()
    focused = object()
    other = object()
    values = {
        (app, "windows"): [target, duplicate_target] if duplicate else [target],
        (target, "title"): target_title,
        (duplicate_target, "title"): duplicate_title or target_title,
        (app, "focused-window"): target if focused_window_matches else other,
        (target, "main"): is_main,
        (app, "focused-element"): focused,
        (focused, "top-level"): (
            other if stale_top_level or focused_title != "oa-trial.txt" else target
        ),
        (other, "title"): focused_title,
    }
    if value is not None:
        values[(focused, "value")] = value
    if selected_range is not None:
        values[(focused, "selected-range")] = ("cf-range", selected_range)
    if parent_walk:
        # Qt's item views: no AXTopLevelUIElement; the window is up the tree.
        del values[(focused, "top-level")]
        table = object()
        values[(focused, "parent")] = table
        values[(table, "role")] = "AXTable"
        values[(table, "parent")] = app if walk_reaches_app else target
        values[(target, "role")] = "AXWindow"
        values[(app, "role")] = "AXApplication"
    calls: list[tuple] = []

    module.AXUIElementCreateApplication = lambda pid: app if pid == 9001 else None

    reads = {"value": 0}

    def copy(element, attribute, _error):
        key = (element, attribute)
        if value_drifts and key == (focused, "value"):
            # Something else edits the field between two reads.
            reads["value"] += 1
            return 0, values[key] + "x" * (reads["value"] - 1)
        return (0, values[key]) if key in values else (1, None)

    def set_value(element, attribute, value):
        calls.append(("set", element, attribute, value))
        if set_result == 0:
            values[(element, attribute)] = (
                readback if readback is not None and attribute == "value" else value
            )
        return set_result

    module.AXUIElementCopyAttributeValue = copy
    module.AXUIElementSetAttributeValue = set_value

    def is_settable(element, attribute, _error):
        if element != focused:
            return 1, False
        if attribute == "selected-text":
            return 0, selected_settable
        if attribute == "value":
            return 0, value_settable
        return 1, False

    module.AXUIElementIsAttributeSettable = is_settable
    module.AXUIElementPerformAction = lambda element, action: (
        calls.append(("action", element, action)) or 0
    )
    return module, app, target, focused, calls


def test_raise_window_selects_exact_ax_document_and_requests_key_state(
    monkeypatch,
) -> None:
    module, app, target, _focused, calls = _ax_module()
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().raise_window(_window()) is True
    assert calls == [
        ("set", app, "focused-window", target),
        ("set", target, "main", True),
        ("set", target, "focused", True),
        ("action", target, "raise"),
    ]


@pytest.mark.parametrize(
    ("owner", "suffix"),
    [
        ("Google Chrome", "Google Chrome"),
        ("Google Chrome for Testing", "Google Chrome for Testing"),
    ],
)
def test_raise_window_accepts_only_corresponding_known_chrome_ax_suffix(
    monkeypatch, owner: str, suffix: str
) -> None:
    cg_title = "Example Order Management"
    module, app, target, _focused, calls = _ax_module(
        target_title=f"{cg_title} - {suffix}"
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().raise_window(_window(owner=owner, title=cg_title)) is True
    assert calls == [
        ("set", app, "focused-window", target),
        ("set", target, "main", True),
        ("set", target, "focused", True),
        ("action", target, "raise"),
    ]


@pytest.mark.parametrize(
    ("owner", "cg_title", "ax_title", "expected"),
    [
        (
            "Google Chrome",
            "Example Order Management",
            "Example Order Management",
            True,
        ),
        (
            "Google Chrome",
            "Example Order Management",
            "Example Order Management - Google Chrome",
            True,
        ),
        (
            "Google Chrome for Testing",
            "Example Order Management",
            "Example Order Management - Google Chrome for Testing",
            True,
        ),
        (
            "Google Chrome",
            "Example Order Management",
            "Different Application",
            False,
        ),
        (
            "Google Chrome",
            "Example Order Management",
            "Example - Google Chrome Order Management",
            False,
        ),
        (
            "TextEdit",
            "Example Order Management",
            "Example Order Management - Google Chrome",
            False,
        ),
    ],
)
def test_exact_window_focused_main_applies_only_known_chrome_suffixes(
    monkeypatch,
    owner: str,
    cg_title: str,
    ax_title: str,
    expected: bool,
) -> None:
    module, _app, _target, _focused, _calls = _ax_module(target_title=ax_title)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert (
        MacWindowClient().exact_window_focused_main(
            _window(owner=owner, title=cg_title)
        )
        is expected
    )


def test_chrome_suffix_matching_preserves_ax_ambiguity(monkeypatch) -> None:
    cg_title = "Example Order Management"
    module, _app, _target, _focused, calls = _ax_module(
        target_title=cg_title,
        duplicate=True,
        duplicate_title=f"{cg_title} - Google Chrome",
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)
    window = _window(owner="Google Chrome", title=cg_title)

    assert MacWindowClient().raise_window(window) is False
    assert MacWindowClient().exact_window_focused_main(window) is False
    assert calls == []


def test_raise_window_refuses_duplicate_exact_ax_titles(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(duplicate=True)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().raise_window(_window()) is False
    assert calls == []


def test_replace_selected_text_is_bound_to_unique_focused_target(monkeypatch) -> None:
    module, _app, _target, focused, calls = _ax_module()
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "Résumé — 東京") is True
    assert calls == [("set", focused, "selected-text", "Résumé — 東京")]


def test_replace_selected_text_refuses_focus_in_another_window(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(
        focused_title="other-document.txt"
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_unfocused_exact_ax_window(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(focused_window_matches=False)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_non_main_exact_ax_window(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(is_main=False)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_stale_top_level_handle(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(stale_top_level=True)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_unwritable_element_without_fallback(
    monkeypatch,
) -> None:
    module, _app, _target, _focused, calls = _ax_module(
        selected_settable=False,
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_ax_delivery_failure_without_fallback(
    monkeypatch,
) -> None:
    module, _app, _target, focused, calls = _ax_module(set_result=7)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not retry") is False
    assert calls == [("set", focused, "selected-text", "must not retry")]


def test_replace_selected_text_refuses_wrong_pid_and_title(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module()
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)
    client = MacWindowClient()

    wrong_pid = WindowInfo(41, "TextEdit", "oa-trial.txt", 9999, (0, 0, 400, 300))
    wrong_title = WindowInfo(41, "TextEdit", "wrong.txt", 9001, (0, 0, 400, 300))
    assert client.replace_selected_text(wrong_pid, "must not land") is False
    assert client.replace_selected_text(wrong_title, "must not land") is False
    assert calls == []


def test_replace_selected_text_refuses_duplicate_ax_target_titles(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(duplicate=True)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "must not land") is False
    assert calls == []


# -- toolkits that allow only AXValue (Qt line edits) --------------------------


def test_value_only_field_gets_the_text_spliced_over_the_selection(
    monkeypatch,
) -> None:
    module, _app, _target, focused, calls = _ax_module(
        selected_settable=False,
        value_settable=True,
        value="endocrine",
        selected_range=(0, 9),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "review-q4") is True
    assert calls == [
        ("set", focused, "value", "review-q4"),
        ("set", focused, "selected-range", ("cf-range", (9, 0))),
    ]


def test_value_only_field_inserts_at_the_caret(monkeypatch) -> None:
    module, _app, _target, focused, calls = _ax_module(
        selected_settable=False,
        value_settable=True,
        value="abc",
        selected_range=(1, 0),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "XY") is True
    assert calls[0] == ("set", focused, "value", "aXYbc")


@pytest.mark.parametrize(
    "case",
    [
        # The field reads back something else: not delivered.
        {"value": "", "selected_range": (0, 0), "readback": "autocorrected"},
        # A non-empty field whose selection can't be read.
        {"value": "endocrine", "selected_range": None},
        # A selection past the end of the value.
        {"value": "abc", "selected_range": (2, 5)},
        # The value write itself fails.
        {"value": "", "selected_range": (0, 0), "set_result": 7},
        # The field changes between the read and the write.
        {"value": "ab", "selected_range": (2, 0), "value_drifts": True},
        # AX ranges count UTF-16 units: a field outside the BMP is refused.
        {"value": "\U0001f600ab", "selected_range": (3, 0)},
    ],
)
def test_value_only_field_fails_closed(monkeypatch, case) -> None:
    module, *_rest = _ax_module(selected_settable=False, value_settable=True, **case)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "review-q4") is False


def test_focus_without_a_top_level_is_scoped_by_its_parent_window(
    monkeypatch,
) -> None:
    module, _app, _target, focused, calls = _ax_module(parent_walk=True)
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "text") is True
    assert calls == [("set", focused, "selected-text", "text")]


def test_focus_whose_parents_reach_the_application_is_refused(monkeypatch) -> None:
    module, _app, _target, _focused, calls = _ax_module(
        parent_walk=True, walk_reaches_app=True
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", module)

    assert MacWindowClient().replace_selected_text(_window(), "text") is False
    assert calls == []
