"""Measure fixture storage, bounded Win32 text and the UIA wire independently.

This disposable diagnostic collects measurements. It does not qualify an
editable-value provider and does not change the canonical reader or runtime.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_windows_hosted_replay import RUN_E2E, _Host

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E, reason="opt-in Windows diagnostic; no GUI on this Mac"
    ),
    pytest.mark.timeout(240),
]
EVIDENCE_ENV = "OAFLOW_NATIVE_VALUE_EXTENT_EVIDENCE"
REPOSITORY = Path(__file__).resolve().parents[4]
SIZES = (0, 20, 4095, 4096, 4097, 8196, 65537)
MAX_NATIVE_READ = 65536

# A copied synthetic fixture owns all fields. The snapshot reads live managed
# state at Shown and again on request; it does not copy setter intentions into
# the observation. The timer only writes diagnostic files, never field values.
HOOK = r"""
$form.WindowState = 'Normal'
$form.Size = New-Object System.Drawing.Size(960, 560)
$extentFields = @{}
$i = 0
foreach ($length in @(0, 20, 4095, 4096, 4097, 8196, 65537)) {
    $box = New-Object System.Windows.Forms.TextBox
    $box.Name = 'extent' + $length
    $box.AccessibleName = $box.Name
    $box.Location = New-Object System.Drawing.Point(650, (50 + $i * 48))
    $box.Size = New-Object System.Drawing.Size(180, 24)
    $box.MaxLength = 100000
    if ($length -eq 0) { $box.Text = '' }
    elseif ($length -eq 20) { $box.Text = (([string][char]0x00e9) * 19) + 'Z' }
    else { $box.Text = ('x' * ($length - 4)) + '-END' }
    $form.Controls.Add($box)
    $extentFields[[string]$length] = $box
    $i++
}
function Save-ExtentSnapshot($filename) {
    $rows = @()
    foreach ($size in @(0, 20, 4095, 4096, 4097, 8196, 65537)) {
        $control = $script:extentFields[[string]$size]
        $text = [string]$control.Text
        $bytes = [Text.Encoding]::UTF8.GetBytes($text)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $hash = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        $point = $control.PointToScreen([Drawing.Point]::new([int]($control.Width / 2), [int]($control.Height / 2)))
        $rows += @{ intended_length=$size; live_length=$text.Length;
                    suffix=$text.Substring([Math]::Max(0, $text.Length - 16)); sha256=$hash;
                    hwnd=$control.Handle.ToInt64(); pid=$PID; point=@($point.X, $point.Y);
                    automation_id=$control.Name }
    }
    $temporary = Join-Path $PSScriptRoot ($filename + '.tmp')
    [IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $rows -Depth 5))
    [IO.File]::Move($temporary, (Join-Path $PSScriptRoot $filename))
}
$form.Add_Shown({ Save-ExtentSnapshot 'managed-initial.json' })
$extentTimer = New-Object System.Windows.Forms.Timer
$extentTimer.Interval = 50
$extentTimer.Add_Tick({
    $request = Join-Path $PSScriptRoot 'extent-snapshot-request'
    if (Test-Path $request) {
        Remove-Item $request
        Save-ExtentSnapshot 'managed-final.json'
    }
})
$extentTimer.Start()
"""


def _metadata(value: str) -> dict[str, Any]:
    return {
        "length": len(value),
        "utf16_units": len(value.encode("utf-16-le")) // 2,
        "suffix": value[-16:],
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _intended(size: int) -> str:
    return (
        "" if size == 0 else "é" * 19 + "Z" if size == 20 else "x" * (size - 4) + "-END"
    )


def _wait_json(path: Path) -> Any:
    deadline = time.monotonic() + 20
    while not path.is_file():
        assert time.monotonic() < deadline, f"fixture measurement missing: {path.name}"
        time.sleep(0.05)
    return json.loads(path.read_text(encoding="utf-8-sig"))


class _NativeText:
    """Read-only Unicode window messages with bounded buffers and deadlines."""

    def __init__(self):
        from ctypes import wintypes

        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    def identity(self, hwnd: int) -> dict[str, int]:
        from ctypes import wintypes

        assert self.user.IsWindow(hwnd), "fixture HWND no longer exists"
        pid = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        assert thread and pid.value, "fixture HWND has no owner"
        return {"hwnd": hwnd, "pid": pid.value, "thread": thread}

    def message(self, hwnd: int, message: int, count=0, pointer=0) -> int:
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        accepted = self.user.SendMessageTimeoutW(
            hwnd, message, count, pointer, 0x23, 1500, ctypes.byref(result)
        )
        if not accepted:
            raise RuntimeError(
                f"bounded native text message failed: {message:#x}, error={ctypes.get_last_error()}"
            )
        return result.value

    def observe(self, hwnd: int, expected_pid: int) -> dict[str, Any]:
        before = self.identity(hwnd)
        assert before["pid"] == expected_pid
        rounds = []
        for _ in range(2):
            extent = self.message(hwnd, 0x000E)  # WM_GETTEXTLENGTH: no text buffer
            row: dict[str, Any] = {"extent_utf16_units": extent}
            if extent <= MAX_NATIVE_READ:
                buffer = ctypes.create_unicode_buffer(extent + 1)
                copied = self.message(
                    hwnd, 0x000D, extent + 1, ctypes.addressof(buffer)
                )
                value = buffer.value
                row.update({"copied_utf16_units": copied, "value": _metadata(value)})
                assert copied == extent == row["value"]["utf16_units"], row
            else:
                row["read_skipped"] = "extent_exceeds_65536_read_cap"
            rounds.append(row)
        after = self.identity(hwnd)
        assert before == after and rounds[0] == rounds[1], "native observation changed"
        return {"before": before, "after": after, "rounds": rounds, "stable": True}


def _managed_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "length": row["live_length"],
        "utf16_units": row["live_length"],
        "suffix": row["suffix"],
        "sha256": row["sha256"],
    }


def test_native_value_extent_diagnostic(tmp_path):
    evidence: dict[str, Any] = {
        "schema": "openadapt.native-value-extent-diagnostic/v1",
        "diagnostic": True,
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True
        ).strip(),
        "source_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=REPOSITORY, text=True
        ).strip(),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "measurements": [],
        "collected": False,
    }
    host = _Host(tmp_path)
    app = tmp_path / "patient_notes.ps1"
    source = app.read_text()
    assert source.count("[void]$form.ShowDialog()") == 1
    app.write_text(
        source.replace("[void]$form.ShowDialog()", HOOK + "\n[void]$form.ShowDialog()")
    )
    evidence["fixture_sha256"] = hashlib.sha256(app.read_bytes()).hexdigest()
    try:
        backend = host.start_agent()
        host.launch_app()
        initial = _wait_json(tmp_path / "managed-initial.json")
        assert isinstance(initial, list) and len(initial) == len(SIZES)
        evidence["managed_initial"] = initial
        evidence["rows_before"] = host.rows()
        native = _NativeText()
        for size in SIZES:
            managed = next(row for row in initial if row["intended_length"] == size)
            measurement: dict[str, Any] = {
                "intended_size": size,
                "intended": _metadata(_intended(size)),
                "managed_initial": managed,
            }
            evidence["measurements"].append(measurement)
            assert host.app is not None and managed["pid"] == host.app.pid
            backend.click(*managed["point"])  # sole input: focus owned fixture field
            before = native.observe(managed["hwnd"], managed["pid"])
            measurement["native_before_uia"] = before
            response = backend._session.post(
                backend.server_url + "/uia/editable-value",
                json={"x": managed["point"][0], "y": managed["point"][1]},
                **backend._request_kwargs(),
            )
            raw = response.json()
            measurement["uia_wire"] = {
                "http_status": response.status_code,
                "response": raw,
            }
            assert response.status_code == 200 and isinstance(raw, dict), measurement[
                "uia_wire"
            ]
            assert raw.get("status") in {"ok", "unavailable"}, raw
            if raw["status"] == "ok":
                assert isinstance(raw.get("value"), str), raw
                measurement["uia_value"] = _metadata(raw["value"])
            else:
                assert raw.get("complete") is False and isinstance(
                    raw.get("reason"), str
                ), raw
            after = native.observe(managed["hwnd"], managed["pid"])
            measurement["native_after_uia"] = after
            assert before == after, "native value/owner changed around UIA request"
        (tmp_path / "extent-snapshot-request").write_text(
            "read-only final managed snapshot"
        )
        final = _wait_json(tmp_path / "managed-final.json")
        evidence["managed_final"] = final
        assert isinstance(final, list) and len(final) == len(SIZES)
        for measurement in evidence["measurements"]:
            size = measurement["intended_size"]
            managed = measurement["managed_initial"]
            observed_final = next(
                row for row in final if row["intended_length"] == size
            )
            assert managed == observed_final, (
                "live managed state/owner changed during measurement"
            )
            measurement["managed_final"] = observed_final
            actual = _managed_metadata(managed)
            first_native = measurement["native_before_uia"]["rounds"][0]
            full = first_native.get("value")
            measurement["native_managed_agree"] = (
                full == actual if full is not None else None
            )
            if actual != measurement["intended"]:
                classification = "fixture_storage_mismatch"
            elif full is None:
                classification = "oversized_extent_only"
            elif full != actual:
                classification = "native_managed_mismatch"
            elif measurement["uia_wire"]["response"]["status"] == "unavailable":
                classification = "uia_unavailable"
            elif measurement["uia_value"] != actual:
                classification = "provider_mismatch"
            else:
                classification = "observations_agree"
            measurement["classification"] = classification
            # A provider conclusion requires an independently stable full read.
            if full is not None:
                assert full == actual, "native and managed measurements disagree"
            else:
                assert first_native["extent_utf16_units"] == managed["live_length"]
        evidence["rows_after"] = host.rows()
        assert evidence["rows_before"] == evidence["rows_after"]
        evidence["collected"] = True
    except BaseException as exc:
        evidence["measurement_error"] = type(exc).__name__
        raise
    finally:
        fixture_process = host.app
        try:
            if (tmp_path / "patients.db").exists():
                evidence["rows_after"] = host.rows()
        finally:
            try:
                host.close()
            finally:
                evidence["agent_terminated"] = (
                    host.agent is None or host.agent.poll() is not None
                )
                evidence["fixture_terminated"] = (
                    fixture_process is None or fixture_process.poll() is not None
                )
                for destination in (
                    tmp_path / "extent-diagnostic.json",
                    Path(
                        os.environ.get(
                            EVIDENCE_ENV, tmp_path / "published-extent-diagnostic.json"
                        )
                    ),
                ):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(evidence, indent=2) + "\n")
