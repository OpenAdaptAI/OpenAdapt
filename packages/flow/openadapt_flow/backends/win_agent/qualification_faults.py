"""Opt-in native qualification overlays, with read-back and bounded lifetime.

The helper only paints test controls. It never sends input to the application
or changes what UI Automation returns. Every mutation must be observed through
the ordinary UIA and screenshot paths before a receipt is returned.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import uuid
from typing import Any, Callable

# Fixed program, supplied with JSON through stdin. No client string is compiled
# as PowerShell. The form does not activate: the application/session observation
# stays the one being tested. Its buttons have no input handlers or save path.
_OVERLAY_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System.Windows.Forms;
public class QualificationOverlay : Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get { var cp = base.CreateParams; cp.ExStyle |= 0x08000000; return cp; }
    }
}
'@
[Console]::InputEncoding = [Text.Encoding]::UTF8
$c = [Console]::In.ReadToEnd() | ConvertFrom-Json
$f = New-Object QualificationOverlay
$f.FormBorderStyle = 'None'
$f.ShowInTaskbar = $false
$f.StartPosition = 'Manual'
$f.TopMost = $true
$f.Text = [string]$c.window_name
$f.BackColor = [Drawing.Color]::White
$x = [int]$c.bounds[0]; $y = [int]$c.bounds[1]
$w = [Math]::Max(120, [int]$c.bounds[2] - $x)
$h = [Math]::Max(40, [int]$c.bounds[3] - $y)
if ($c.kind -eq 'stale_identity') {
    $f.Bounds = [Windows.Forms.SystemInformation]::VirtualScreen
    $l = New-Object Windows.Forms.Label
    $l.Name = 'qualification-stale-surface'
    $l.AccessibleName = 'Qualification stale surface'
    $l.Dock = 'Fill'
    $l.BackColor = [Drawing.Color]::White
    $f.Controls.Add($l)
} else {
    $f.Location = New-Object Drawing.Point($x, $y)
    $f.Size = New-Object Drawing.Size($w, ($h * 2))
    if ($c.kind -eq 'ambiguity') {
        foreach ($i in 0, 1) {
            $b = New-Object Windows.Forms.Button
            $b.Name = [string]$c.automation_id
            $b.AccessibleName = [string]$c.name
            $b.Text = [string]$c.name
            $b.Location = New-Object Drawing.Point(0, ($i * $h))
            $b.Size = New-Object Drawing.Size($w, $h)
            $f.Controls.Add($b)
        }
    } else {
        $l = New-Object Windows.Forms.Label
        $l.Name = 'qualification-altered-action'
        $l.Text = 'Merge Into Existing Record'
        $l.AccessibleName = $l.Text
        $l.Dock = 'Fill'
        $l.BackColor = [Drawing.Color]::White
        $l.ForeColor = [Drawing.Color]::Red
        $f.Controls.Add($l)
    }
}
$t = New-Object Windows.Forms.Timer
$t.Interval = 180000
$t.Add_Tick({ $f.Close() })
$t.Start()
[Windows.Forms.Application]::Run($f)
$t.Dispose()
$f.Dispose()
"""


class FaultStagingError(RuntimeError):
    """No mutation receipt may be accepted for this operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _launch_overlay(payload: dict[str, Any]) -> Any:
    if os.name != "nt":
        raise FaultStagingError("qualification_windows_unavailable")
    encoded = base64.b64encode(_OVERLAY_PS.encode("utf-16le")).decode("ascii")
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-EncodedCommand",
            encoded,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,  # CREATE_NO_WINDOW; the form is still visible.
    )
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload).encode("utf-8"))
        process.stdin.close()
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise
    return process


class QualificationFaultController:
    """One owned overlay per agent; cleanup never accepts a client process id."""

    def __init__(
        self,
        uia: Callable[[str, dict[str, Any]], dict[str, Any]],
        capture: Callable[[], bytes],
        *,
        launch: Callable[[dict[str, Any]], Any] = _launch_overlay,
        timeout_s: float = 8.0,
    ) -> None:
        self._uia, self._capture, self._launch = uia, capture, launch
        self._timeout_s = timeout_s
        self._active: tuple[str, Any] | None = None

    def close(self) -> None:
        """Remove this controller's overlay when its server stops."""
        if self._active is not None:
            self.clear(self._active[0])

    def clear(self, mutation_id: str) -> dict[str, Any]:
        active = self._active
        if active is None or active[0] != mutation_id:
            raise FaultStagingError("qualification_mutation_not_owned")
        process = active[1]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._active = None
        return {"status": "ok", "cleared": True}

    def stage(self, kind: str, locator: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in {
            "ambiguity",
            "wrong_identity",
            "stale_identity",
        }:
            raise FaultStagingError("qualification_unknown_fault")
        if self._active is not None:
            raise FaultStagingError("qualification_mutation_active")
        before = self._uia("find", {"locator": locator})
        candidates = before.get("candidates")
        if (
            before.get("status") != "ok"
            or before.get("match") != "unique"
            or before.get("candidate_count") != 1
            or before.get("truncated") is not False
            or not isinstance(candidates, list)
            or len(candidates) != 1
        ):
            raise FaultStagingError("qualification_target_not_unique")
        candidate = candidates[0]
        if not isinstance(candidate, dict) or candidate.get("role") != "button":
            raise FaultStagingError("qualification_target_role_unsupported")
        bounds = candidate.get("bounds")
        point = candidate.get("point")
        if (
            not isinstance(bounds, list)
            or len(bounds) != 4
            or any(
                isinstance(v, bool) or not isinstance(v, int) or abs(v) > 32768
                for v in bounds
            )
            or bounds[2] <= bounds[0]
            or bounds[3] <= bounds[1]
            or not isinstance(point, list)
            or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in point)
            or not (
                bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]
            )
        ):
            raise FaultStagingError("qualification_target_geometry_unavailable")
        target_point = {"x": point[0], "y": point[1]}
        identity = self._uia("element-identity-at", target_point).get("identity")
        if identity is None:
            raise FaultStagingError("qualification_identity_unavailable")
        png = self._capture()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise FaultStagingError("qualification_capture_unavailable")
        before_sha = hashlib.sha256(png).digest()
        data = {
            key: candidate.get(key) for key in ("automation_id", "name", "window_name")
        }
        for value in data.values():
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise FaultStagingError("qualification_target_identity_unavailable")
        data.update(kind=kind, bounds=bounds)
        token = uuid.uuid4().hex
        process = self._launch(data)
        self._active = (token, process)
        try:
            deadline = time.monotonic() + self._timeout_s
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise FaultStagingError("qualification_overlay_exited")
                if kind == "ambiguity":
                    observed = self._uia("find", {"locator": locator})
                    changed = (
                        observed.get("status") == "ok"
                        and observed.get("match") == "ambiguous"
                        and observed.get("truncated") is False
                        and isinstance(observed.get("candidate_count"), int)
                        and observed["candidate_count"] >= 2
                    )
                else:
                    observed = self._uia("element-identity-at", target_point)
                    changed = (
                        observed.get("status") == "ok"
                        and observed.get("identity") is not None
                        and observed["identity"] != identity
                    )
                frame = self._capture()
                if (
                    changed
                    and frame.startswith(b"\x89PNG\r\n\x1a\n")
                    and hashlib.sha256(frame).digest() != before_sha
                ):
                    return {"status": "ok", "mutation_id": token, "kind": kind}
                time.sleep(0.1)
            raise FaultStagingError("qualification_mutation_not_observed")
        except BaseException:
            self.clear(token)
            raise
