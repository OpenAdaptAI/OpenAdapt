"""Windows-only smoke test for the real pywinauto UIA boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from openadapt_capture.structural import (
    StructuralObservation,
    StructuralObservationRequest,
)
from openadapt_capture.structural_observer.windows import (
    WindowsUIAStructuralObserver,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires Windows UI Automation",
)

_WINFORMS_FIXTURE = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = $env:OPENADAPT_UIA_SMOKE_TITLE
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.Location = [System.Drawing.Point]::new(180, 140)
$form.Size = [System.Drawing.Size]::new(520, 260)
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Text = "Member ID"
$label.Location = [System.Drawing.Point]::new(24, 30)
$label.AutoSize = $true

$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Name = "MemberIdField"
$textBox.Location = [System.Drawing.Point]::new(24, 58)
$textBox.Size = [System.Drawing.Size]::new(300, 28)

$submitOne = New-Object System.Windows.Forms.Button
$submitOne.Name = "SubmitAction"
$submitOne.Text = "Submit"
$submitOne.Location = [System.Drawing.Point]::new(24, 116)
$submitOne.Size = [System.Drawing.Size]::new(120, 36)

$submitTwo = New-Object System.Windows.Forms.Button
$submitTwo.Name = "SubmitAction"
$submitTwo.Text = "Submit"
$submitTwo.Location = [System.Drawing.Point]::new(164, 116)
$submitTwo.Size = [System.Drawing.Size]::new(120, 36)

$form.Controls.AddRange(@($label, $textBox, $submitOne, $submitTwo))
$form.Add_Shown({
    $form.Activate()
    $textBox.Select()
    [void]$textBox.Focus()
    $form.BeginInvoke([Action]{
        $form.Activate()
        $textBox.Select()
        [void]$textBox.Focus()
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 250
        $buttonPoint = $form.PointToScreen(
            ([System.Drawing.Point]::new(
                ($submitOne.Left + [int]($submitOne.Width / 2)),
                ($submitOne.Top + [int]($submitOne.Height / 2))
            ))
        )
        $payload = @{
            button_x = $buttonPoint.X
            button_y = $buttonPoint.Y
        } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText(
            $env:OPENADAPT_UIA_SMOKE_READY,
            $payload,
            [System.Text.Encoding]::ASCII
        )
    })
})

[System.Windows.Forms.Application]::Run($form)
"""


def _wait_for_fixture(path: Path, process: subprocess.Popen[str]) -> dict[str, int]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="ascii"))
            except json.JSONDecodeError:
                pass
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"WinForms fixture exited early ({process.returncode}): "
                f"{stdout}\n{stderr}"
            )
        time.sleep(0.1)
    raise AssertionError("WinForms fixture did not become ready within 30 seconds")


def _observe_on_owned_thread(
    requests: list[StructuralObservationRequest],
) -> list[StructuralObservation | None]:
    observations: list[StructuralObservation | None] = []
    errors: list[BaseException] = []

    def run() -> None:
        observer = WindowsUIAStructuralObserver()
        try:
            observer.open_current_thread()
            observations.extend(observer.observe(request) for request in requests)
        except BaseException as exc:
            errors.append(exc)
        finally:
            observer.close_current_thread()

    thread = threading.Thread(target=run, name="capture-uia-smoke")
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "UIA observation thread did not terminate"
    if errors:
        raise errors[0]
    return observations


def test_real_pywinauto_observes_point_focus_ambiguity_and_reopens(
    tmp_path: Path,
) -> None:
    """Exercise exact pywinauto APIs and COM ownership without injecting input."""

    title = f"OpenAdapt UIA smoke {uuid.uuid4()}"
    script_path = tmp_path / "uia_fixture.ps1"
    ready_path = tmp_path / "ready.json"
    script_path.write_text(_WINFORMS_FIXTURE, encoding="utf-8")
    env = {
        **os.environ,
        "OPENADAPT_UIA_SMOKE_TITLE": title,
        "OPENADAPT_UIA_SMOKE_READY": str(ready_path),
    }
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        point = _wait_for_fixture(ready_path, process)
        requests = [
            StructuralObservationRequest(
                event_timestamp=time.time(),
                action_name="click",
                x=float(point["button_x"]),
                y=float(point["button_y"]),
            ),
            StructuralObservationRequest(
                event_timestamp=time.time(),
                action_name="press",
            ),
        ]
        clicked, focused = _observe_on_owned_thread(requests)

        assert clicked is not None
        assert clicked.query_kind == "point"
        assert clicked.element.control_type == "Button"
        assert clicked.element.name == "Submit"
        assert clicked.element.automation_id == "SubmitAction"
        assert clicked.candidate_count == 2
        assert clicked.window is not None
        assert clicked.window.title == title

        assert focused is not None
        assert focused.query_kind == "focused"
        assert focused.element.control_type == "Edit"
        assert focused.element.automation_id == "MemberIdField"
        assert focused.window is not None
        assert focused.window.title == title

        # A second service lifecycle in the same process proves the pywinauto
        # singleton and COM apartment were released on their owning thread.
        reopened = _observe_on_owned_thread(requests[:1])
        assert reopened[0] is not None
        assert reopened[0].element.automation_id == "SubmitAction"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
