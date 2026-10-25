"""Row census against real native list apps (opt-in, one per platform).

Each test starts a small native app showing a worklist, reads it through the
backend's real accessibility code, and checks the census: identical rows are
ambiguous, distinct record ids pick the recorded row, a unique row runs.

* Windows: a WPF ``ListBox`` whose items carry ``AutomationProperties
  .AutomationId``, read through the guest agent's UI Automation census.
  Set ``OPENADAPT_TEST_WINDOWS_UIA=1``; needs the ``uiautomation`` package.
* macOS: a view-based ``NSTableView`` (tests/native_apps/Worklist.swift, built
  with ``swiftc``) whose cells carry an identifier, read through
  ``QuartzMacAXClient.row_census``. Set ``OPENADAPT_TEST_MACOS_AX=1``; needs
  PyObjC and Accessibility access for the test process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.runtime import identity_template as itmpl
from openadapt_flow.runtime.identity_census import CensusDecision, decide

pytestmark = pytest.mark.timeout(180)

TITLE = "OA Census Worklist"
SAME = "John Smith Follow-up Dr Lee"
OTHER = "Maria Garcia Intake Dr Lee"


def _decide(census: Optional[dict], recorded_key: Optional[str] = None):
    tmpl = itmpl.build_identity_template(
        None, structured_identity="x", record_key=recorded_key
    )
    assert tmpl is not None
    return decide(
        census,
        has_recorded_key=tmpl.record_key is not None,
        key_matches=lambda key: itmpl.record_key_matches(tmpl, key),
    )


def _wait(predicate, timeout_s: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("the native test app did not appear")


# -- Windows (WPF + UI Automation) ---------------------------------------------

_WPF_APP = r"""
param([string]$RowsJson)
Add-Type -AssemblyName PresentationFramework
$rows = $RowsJson | ConvertFrom-Json
$window = New-Object System.Windows.Window
$window.Title = '__TITLE__'
$window.Width = 520
$window.Height = 360
$window.Left = 40
$window.Top = 40
$list = New-Object System.Windows.Controls.ListBox
foreach ($row in $rows) {
    $item = New-Object System.Windows.Controls.ListBoxItem
    $panel = New-Object System.Windows.Controls.StackPanel
    $panel.Orientation = 'Horizontal'
    foreach ($cell in $row.cells) {
        $text = New-Object System.Windows.Controls.TextBlock
        $text.Text = $cell
        $text.Margin = '0,0,24,0'
        [void]$panel.Children.Add($text)
    }
    $item.Content = $panel
    if ($row.id) {
        [System.Windows.Automation.AutomationProperties]::SetAutomationId($item, $row.id)
    }
    [void]$list.Items.Add($item)
}
$window.Content = $list
[void]$window.ShowDialog()
""".replace("__TITLE__", TITLE)


@pytest.fixture
def wpf_worklist(tmp_path: Path):
    if sys.platform != "win32" or os.environ.get("OPENADAPT_TEST_WINDOWS_UIA") != "1":
        pytest.skip("set OPENADAPT_TEST_WINDOWS_UIA=1 on Windows")
    auto = pytest.importorskip("uiautomation")
    script = tmp_path / "worklist.ps1"
    script.write_text(_WPF_APP, encoding="utf-8")
    procs: list[subprocess.Popen] = []

    def start(rows: list[dict[str, Any]]) -> list[Any]:
        proc = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-RowsJson",
                json.dumps(rows),
            ]
        )
        procs.append(proc)
        window = _wait(
            lambda: (
                auto.WindowControl(searchDepth=1, Name=TITLE)
                if auto.WindowControl(searchDepth=1, Name=TITLE).Exists(0, 0)
                else None
            )
        )
        listing = window.ListControl(searchDepth=3)
        return _wait(
            lambda: (
                [
                    c
                    for c in listing.GetChildren()
                    if c.ControlTypeName == "ListItemControl"
                ]
                if listing.Exists(0, 0)
                else None
            )
        )

    yield auto, start
    for proc in procs:
        proc.kill()


def _first_cell_center(item: Any) -> tuple[int, int]:
    cell = item.GetChildren()[0]
    rect = cell.BoundingRectangle
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


def test_windows_wpf_identical_rows_are_ambiguous(wpf_worklist) -> None:
    from openadapt_flow.backends.win_agent import server

    auto, start = wpf_worklist
    items = start(
        [
            {"cells": SAME.split(" ", 2), "id": ""},
            {"cells": SAME.split(" ", 2), "id": ""},
            {"cells": OTHER.split(" ", 2), "id": ""},
        ]
    )
    with auto.UIAutomationInitializerInThread():
        census = server._identity_candidates_at(auto, *_first_cell_center(items[1]))
    assert _decide(census) == CensusDecision("ambiguous", 2), census


def test_windows_wpf_record_ids_pick_the_recorded_row(wpf_worklist) -> None:
    from openadapt_flow.backends.win_agent import server

    auto, start = wpf_worklist
    items = start(
        [
            {"cells": SAME.split(" ", 2), "id": "chart-1002"},
            {"cells": SAME.split(" ", 2), "id": "chart-1001"},
        ]
    )
    with auto.UIAutomationInitializerInThread():
        census = server._identity_candidates_at(auto, *_first_cell_center(items[0]))
        unique = server._identity_candidates_at(auto, *_first_cell_center(items[1]))
    decision = _decide(census, "automation-id=chart-1001")
    assert decision.verdict == "retarget", census
    assert decision.offset is not None and decision.offset[1] > 0
    assert _decide(unique, "automation-id=chart-1001").verdict == "unique"


def test_windows_wpf_unique_row_runs(wpf_worklist) -> None:
    from openadapt_flow.backends.win_agent import server

    auto, start = wpf_worklist
    items = start(
        [
            {"cells": SAME.split(" ", 2), "id": ""},
            {"cells": OTHER.split(" ", 2), "id": ""},
        ]
    )
    with auto.UIAutomationInitializerInThread():
        point = _first_cell_center(items[1])
        census = server._identity_candidates_at(auto, *point)
        text = server._structured_text_at(auto, *point)
    assert text, "the identity check reads no row text from this list"
    assert _decide(census) == CensusDecision("unique", 1), census


# -- macOS (NSTableView + AX) --------------------------------------------------

_SWIFT_APP = Path(__file__).parent / "native_apps" / "Worklist.swift"


def _on_screen(pid: int) -> bool:
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return any(
        w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowName") == TITLE
        for w in windows
    )


@pytest.fixture
def cocoa_worklist(tmp_path: Path):
    if sys.platform != "darwin" or os.environ.get("OPENADAPT_TEST_MACOS_AX") != "1":
        pytest.skip("set OPENADAPT_TEST_MACOS_AX=1 on macOS")
    services = pytest.importorskip("ApplicationServices")
    pytest.importorskip("Quartz")
    if not services.AXIsProcessTrusted():
        pytest.skip("this process has no Accessibility access")
    from openadapt_flow.backends.macos_backend import QuartzMacAXClient, _AXTreeView

    binary = tmp_path / "worklist"
    built = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(_SWIFT_APP)],
        capture_output=True,
        text=True,
        timeout=170,
    )
    if built.returncode != 0:
        pytest.skip(f"swiftc could not build the table app: {built.stderr[-400:]}")
    procs: list[subprocess.Popen] = []
    client = QuartzMacAXClient()

    def start(rows: list[dict[str, str]]) -> tuple[int, list[tuple[float, float]]]:
        proc = subprocess.Popen([str(binary), json.dumps(rows), TITLE])
        procs.append(proc)
        view = _AXTreeView(client)

        def row_centers() -> Optional[list[tuple[float, float]]]:
            window = client._window_element(proc.pid, TITLE)
            if window is None:
                return None
            stack, found = [window], []
            while stack:
                node = stack.pop()
                if view.role(node) == "AXRow":
                    bounds = client._bounds(node)
                    if bounds is not None:
                        found.append((bounds[0] + 20, bounds[1] + bounds[3] / 2))
                    continue
                stack.extend(view.children(node))
            return (
                sorted(found, key=lambda p: p[1]) if len(found) == len(rows) else None
            )

        try:
            return proc.pid, _wait(row_centers)
        except AssertionError:
            if _on_screen(proc.pid):
                # The window is on screen but AX lists no window for it: this
                # session's Accessibility access is degraded (AX answers with
                # the application element), so nothing can be read.
                pytest.skip("AX lists no windows in this session; cannot read the app")
            raise

    yield client, start
    for proc in procs:
        proc.kill()


def test_macos_table_identical_rows_are_ambiguous(cocoa_worklist) -> None:
    client, start = cocoa_worklist
    pid, centers = start(
        [{"text": SAME, "id": ""}, {"text": SAME, "id": ""}, {"text": OTHER, "id": ""}]
    )
    census = client.row_census(pid, TITLE, *centers[1], limit=4000)
    assert _decide(census) == CensusDecision("ambiguous", 2), census


def test_macos_table_record_ids_pick_the_recorded_row(cocoa_worklist) -> None:
    client, start = cocoa_worklist
    pid, centers = start(
        [{"text": SAME, "id": "chart-1002"}, {"text": SAME, "id": "chart-1001"}]
    )
    census = client.row_census(pid, TITLE, *centers[0], limit=4000)
    decision = _decide(census, "ax-identifier=chart-1001")
    assert decision.verdict == "retarget", census
    assert decision.offset is not None and decision.offset[1] > 0


def test_macos_table_unique_row_runs(cocoa_worklist) -> None:
    client, start = cocoa_worklist
    pid, centers = start([{"text": SAME, "id": ""}, {"text": OTHER, "id": ""}])
    census = client.row_census(pid, TITLE, *centers[1], limit=4000)
    assert _decide(census) == CensusDecision("unique", 1), census
