"""macOS: input to the pinned window's own AppKit sheet (#1711).

A sheet (an NSAlert attached to the window) is a separate CoreGraphics window
that becomes frontmost, key and the AX focused window. ``MacOSBackend``
accepts it in place of the pinned window only when the client proves it is
that window's one attached sheet (``MacWindowClient.attached_sheet``); every
other window stays refused. These are rule tests over a fake client; the
live run is ``benchmark/repair_every_backend/qualify_macos.py --sheet``.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backends import macos_backend
from openadapt_flow.backends.macos_backend import MacOSBackendError
from openadapt_flow.backends.remote_display import WindowInfo
from tests.test_macos_structural import TARGET_WINDOW, FakeMacClient, backend

SHEET_ID = 77
SHEET_BOUNDS = (110.0, 60.0, 200.0, 150.0)
IN_SHEET = (150.0, 100.0)
OUTSIDE_SHEET = (20.0, 300.0)


class SheetClient(FakeMacClient):
    def __init__(self, *, sheet: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sheet = sheet
        self.sheet_probes = 0
        self.sheet_focused = sheet
        self.owners = {SHEET_ID: TARGET_WINDOW.pid, 99: 4242}
        self.at_point = TARGET_WINDOW.window_id

    def attached_sheet(self, window: WindowInfo):
        self.sheet_probes += 1
        return (SHEET_ID, SHEET_BOUNDS) if self.sheet else None

    def window_owner_pid(self, window_id: int):
        return self.owners.get(window_id)

    def exact_window_or_sheet_focused(self, window: WindowInfo) -> bool:
        return self.sheet_focused

    def capture_stack(self, window_ids, bounds):
        self.calls.append(("capture_stack", tuple(window_ids), tuple(bounds)))
        return self.capture(window_ids[-1])

    def window_at_point(self, x, y):
        return self.at_point


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(macos_backend, "_SHEET_SETTLE_STEP_S", 0.0)


def _focus(client: SheetClient, front: int) -> None:
    client._frontmost_window_id = front
    client._exact_ax_focus = front == TARGET_WINDOW.window_id


def test_without_a_sheet_only_the_pinned_window_may_be_frontmost() -> None:
    client = SheetClient(sheet=False)
    target = backend(client)
    _focus(client, TARGET_WINDOW.window_id)
    assert target._window_focus_matches(TARGET_WINDOW)
    _focus(client, 99)
    assert not target._window_focus_matches(TARGET_WINDOW)


def test_the_windows_own_sheet_may_hold_focus() -> None:
    client = SheetClient()
    target = backend(client)
    _focus(client, SHEET_ID)
    assert target._window_focus_matches(TARGET_WINDOW)
    client.sheet_focused = False  # AX says focus left the sheet
    assert not target._window_focus_matches(TARGET_WINDOW)


def test_an_unproved_window_of_the_same_app_is_refused_after_a_bounded_wait() -> None:
    client = SheetClient(sheet=False)
    target = backend(client)
    _focus(client, SHEET_ID)  # frontmost, same pid, but not proved a sheet
    assert not target._window_focus_matches(TARGET_WINDOW)
    assert client.sheet_probes == macos_backend._SHEET_SETTLE_POLLS


def test_another_apps_window_is_refused_without_waiting() -> None:
    client = SheetClient(sheet=False)
    target = backend(client)
    _focus(client, 99)  # another process's window
    assert not target._window_focus_matches(TARGET_WINDOW)
    assert client.sheet_probes == 1


@pytest.mark.parametrize(
    "point, hit, holds",
    [
        (IN_SHEET, SHEET_ID, True),
        (IN_SHEET, TARGET_WINDOW.window_id, False),  # the sheet must be on top
        (OUTSIDE_SHEET, TARGET_WINDOW.window_id, True),
        (OUTSIDE_SHEET, SHEET_ID, False),
        (IN_SHEET, 99, False),
    ],
)
def test_a_point_hits_the_sheet_only_inside_it(point, hit, holds) -> None:
    client = SheetClient()
    target = backend(client)
    _focus(client, SHEET_ID)
    client._point_window_id = hit
    sheet = target._attached_sheet(TARGET_WINDOW)
    assert target._bound_target_holds(TARGET_WINDOW, sheet, point) is holds


def test_the_physical_target_check_refuses_a_foreign_window() -> None:
    client = SheetClient(sheet=False)
    target = backend(client)
    target.screenshot()
    bound = target._resolve_window(refresh=True)
    _focus(client, 99)
    with pytest.raises(MacOSBackendError, match="changed after foreground proof"):
        target._assert_bound_physical_target(bound, point=OUTSIDE_SHEET)


def test_capture_includes_the_sheet_and_nothing_else() -> None:
    client = SheetClient()
    target = backend(client)
    target.screenshot()
    assert (
        "capture_stack",
        (SHEET_ID, TARGET_WINDOW.window_id),
        TARGET_WINDOW.bounds,
    ) in client.calls
    plain = SheetClient(sheet=False)
    backend(plain).screenshot()
    assert not [c for c in plain.calls if c[0] == "capture_stack"]


@pytest.mark.parametrize(
    "point, hit, ok",
    [
        (IN_SHEET, SHEET_ID, True),
        (OUTSIDE_SHEET, TARGET_WINDOW.window_id, True),
        (OUTSIDE_SHEET, SHEET_ID, False),
        (IN_SHEET, TARGET_WINDOW.window_id, False),
        (IN_SHEET, 99, False),
    ],
)
def test_click_target_accepts_the_sheet_inside_its_bounds_only(point, hit, ok) -> None:
    client = SheetClient()
    target = backend(client)
    target.screenshot()
    target._frame_window = TARGET_WINDOW
    client.at_point = hit
    if ok:
        target._assert_click_target(*point)
    else:
        with pytest.raises(MacOSBackendError, match="covered by window"):
            target._assert_click_target(*point)


def test_a_client_without_sheet_support_never_accepts_one() -> None:
    client = FakeMacClient()
    target = backend(client)
    client._frontmost_window_id = SHEET_ID
    assert target._attached_sheet(TARGET_WINDOW) is None
    assert not target._window_focus_matches(TARGET_WINDOW)


def test_click_target_probes_once_and_reads_the_hit_last() -> None:
    order: list[str] = []

    class Ordered(SheetClient):
        def attached_sheet(self, window):
            order.append("sheet")
            return None  # a same-app window is frontmost but not a sheet

        def window_at_point(self, x, y):
            order.append("hit")
            return TARGET_WINDOW.window_id

    client = Ordered(sheet=False)
    target = backend(client)
    target.screenshot()
    target._frame_window = TARGET_WINDOW
    client._frontmost_window_id = SHEET_ID  # would make a waiting probe loop
    order.clear()
    target._assert_click_target(*OUTSIDE_SHEET)
    assert order == ["sheet", "hit"]


# -- #1724: the pinned window's own sheet over a band ----------------------------


def test_owned_window_over_needs_the_verified_window_and_an_overlap() -> None:
    client = SheetClient()
    target = backend(client)
    target.screenshot()
    # No capture for the step: no answer.
    assert target.owned_window_over((0, 0, 800, 600), pinned_step_id="s1") is None
    assert target.record_verified_window("s1", 10, 10)
    # The sheet's screen rect (110, 60, 200, 150) is window pixels
    # ((110-10)*2, (60-20)*2, 400, 300) = (200, 80, 400, 300).
    hit = target.owned_window_over((300, 100, 50, 20), pinned_step_id="s1")
    assert hit == {
        "kind": "sheet",
        "window_id": SHEET_ID,
        "pid": TARGET_WINDOW.pid,
        "region": [200, 80, 400, 300],
    }
    assert target.owned_window_over((0, 0, 50, 20), pinned_step_id="s1") is None
    assert target.owned_window_over((300, 100, 50, 20), pinned_step_id="s2") is None


def test_owned_window_over_refuses_without_a_proved_sheet() -> None:
    client = SheetClient(sheet=False)
    target = backend(client)
    target.screenshot()
    target.record_verified_window("s1", 10, 10)
    assert target.owned_window_over((0, 0, 800, 600), pinned_step_id="s1") is None
