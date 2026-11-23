"""Unit tests for read-only everyday OS local-state probes.

Covers:
1. ``macos_night_shift`` (CoreBrightness ``CBBlueLightClient`` read-only probe)
2. ``windows_night_light`` (HKCU CloudStore ``Data`` bytes ``0x15`` vs ``0x13``)
3. ``macos_calendar_event`` (fixed read-only AppleScript probe via argv)

Every probe is verified for valid states, malformed/missing inputs, and wrong-OS
fail-closed ``INDETERMINATE`` behavior.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.local_state_sources import (
    MacosCalendarEventSource,
    MacosNightShiftSource,
    WindowsNightLightSource,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.local_state import (
    OSASCRIPT_BINARY,
    WINDOWS_NIGHT_LIGHT_REG_PATH,
    WINDOWS_NIGHT_LIGHT_VALUE_NAME,
    LocalStateVerifier,
    _Unreadable,
    parse_windows_night_light_bytes,
)
from openadapt_flow.verify_run import VerifyConfig


class _FakeKey:
    def __init__(self, values: dict[str, tuple[Any, int]]) -> None:
        self._values = values

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeWinReg:
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    KEY_READ = 0x20019
    REG_BINARY = 3

    def __init__(self, tree: dict[tuple[int, str], dict[str, tuple[Any, int]]]) -> None:
        self._tree = tree

    def OpenKey(self, hive: int, path: str, _reserved: int, access: int) -> _FakeKey:
        assert access == self.KEY_READ, "registry reads must use KEY_READ only"
        key = (hive, path)
        if key not in self._tree:
            raise FileNotFoundError(path)
        return _FakeKey(self._tree[key])

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[Any, int]:
        if name not in key._values:
            raise FileNotFoundError(name)
        return key._values[name]


def _make_cloudstore_blob(state_byte: int, length: int = 43) -> bytes:
    buf = bytearray(length)
    buf[0:4] = b"\x43\x42\x01\x00"
    buf[18] = state_byte
    if state_byte == 0x15 and length >= 25:
        buf[23:25] = b"\x10\x00"
    return bytes(buf)


# --- macos_night_shift ------------------------------------------------------


def test_macos_night_shift_reads_enabled_and_disabled() -> None:
    src = MacosNightShiftSource(id="ns")
    v_on = LocalStateVerifier([src], platform="darwin", night_shift_reader=lambda: True)
    rec_on = v_on.read()
    assert rec_on == [
        {
            "id": "ns",
            "type": "macos_night_shift",
            "present": True,
            "enabled": True,
            "value": "true",
            "value_type": "boolean",
        }
    ]

    v_off = LocalStateVerifier(
        [src], platform="darwin", night_shift_reader=lambda: False
    )
    rec_off = v_off.read()
    assert rec_off is not None
    assert rec_off[0]["enabled"] is False
    assert rec_off[0]["value"] == "false"


@pytest.mark.parametrize("bad_platform", ["linux", "win32"])
def test_macos_night_shift_fails_closed_on_wrong_os(bad_platform: str) -> None:
    src = MacosNightShiftSource(id="ns")
    v = LocalStateVerifier(
        [src], platform=bad_platform, night_shift_reader=lambda: True
    )
    before = v.capture_pre_state()
    assert before.reachable is False
    assert v.read() is None
    assert v.last_error is not None and "needs macOS" in v.last_error


def test_macos_night_shift_fails_closed_when_probe_unavailable() -> None:
    src = MacosNightShiftSource(id="ns")

    def _boom() -> bool:
        raise _Unreadable("CoreBrightness unavailable")

    v = LocalStateVerifier([src], platform="darwin", night_shift_reader=_boom)
    before = v.capture_pre_state()
    assert before.reachable is False
    verdict = v.verify(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"id": "ns"},
            field="value",
            value="true",
            settle_timeout_s=0.0,
        ),
        before,
    )
    assert verdict.confirmed is False
    assert verdict.verdict == Verdict.INDETERMINATE


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only live read-only probe")
def test_macos_night_shift_live_readonly_probe_on_mac() -> None:
    """Exercise the real CoreBrightness ctypes reader read-only on macOS."""
    src = MacosNightShiftSource(id="ns")
    v = LocalStateVerifier([src], platform="darwin")
    records = v.read()
    if records is None:
        # On headless Macs without Night Shift hardware, probe must fail closed cleanly.
        assert v.last_error is not None
    else:
        assert len(records) == 1
        assert isinstance(records[0]["enabled"], bool)
        assert records[0]["value"] in ("true", "false")


# --- windows_night_light ----------------------------------------------------


def test_parse_windows_night_light_bytes_valid_and_invalid() -> None:
    assert parse_windows_night_light_bytes(_make_cloudstore_blob(0x15, 43)) is True
    assert parse_windows_night_light_bytes(_make_cloudstore_blob(0x13, 41)) is False

    for bad in (b"", b"\x15" * 18, _make_cloudstore_blob(0x00, 41), "not-bytes", 123):
        with pytest.raises(_Unreadable):
            parse_windows_night_light_bytes(bad)


def test_windows_night_light_reads_enabled_and_disabled() -> None:
    src = WindowsNightLightSource(id="nl")
    reg_on = _FakeWinReg(
        {
            (_FakeWinReg.HKEY_CURRENT_USER, WINDOWS_NIGHT_LIGHT_REG_PATH): {
                WINDOWS_NIGHT_LIGHT_VALUE_NAME: (
                    _make_cloudstore_blob(0x15, 43),
                    _FakeWinReg.REG_BINARY,
                )
            }
        }
    )
    v_on = LocalStateVerifier([src], platform="win32", registry=reg_on)
    rec_on = v_on.read()
    assert rec_on == [
        {
            "id": "nl",
            "type": "windows_night_light",
            "present": True,
            "enabled": True,
            "value": "true",
            "value_type": "boolean",
        }
    ]

    reg_off = _FakeWinReg(
        {
            (_FakeWinReg.HKEY_CURRENT_USER, WINDOWS_NIGHT_LIGHT_REG_PATH): {
                WINDOWS_NIGHT_LIGHT_VALUE_NAME: (
                    _make_cloudstore_blob(0x13, 41),
                    _FakeWinReg.REG_BINARY,
                )
            }
        }
    )
    v_off = LocalStateVerifier([src], platform="win32", registry=reg_off)
    rec_off = v_off.read()
    assert rec_off is not None
    assert rec_off[0]["enabled"] is False
    assert rec_off[0]["value"] == "false"


def test_windows_night_light_fails_closed_on_missing_key_value_malformed_and_wrong_os() -> (
    None
):
    src = WindowsNightLightSource(id="nl")

    # Wrong OS without injected registry
    v_wrong_os = LocalStateVerifier([src], platform="darwin")
    assert v_wrong_os.read() is None

    # Missing registry key
    v_no_key = LocalStateVerifier([src], platform="win32", registry=_FakeWinReg({}))
    assert v_no_key.read() is None
    assert v_no_key.last_error is not None and "does not exist" in v_no_key.last_error

    # Missing Data value
    v_no_val = LocalStateVerifier(
        [src],
        platform="win32",
        registry=_FakeWinReg(
            {(_FakeWinReg.HKEY_CURRENT_USER, WINDOWS_NIGHT_LIGHT_REG_PATH): {}}
        ),
    )
    assert v_no_val.read() is None
    assert v_no_val.last_error is not None and "missing" in v_no_val.last_error

    # Malformed bytes -> INDETERMINATE
    v_bad_bytes = LocalStateVerifier(
        [src],
        platform="win32",
        registry=_FakeWinReg(
            {
                (_FakeWinReg.HKEY_CURRENT_USER, WINDOWS_NIGHT_LIGHT_REG_PATH): {
                    WINDOWS_NIGHT_LIGHT_VALUE_NAME: (
                        _make_cloudstore_blob(0x99, 41),
                        _FakeWinReg.REG_BINARY,
                    )
                }
            }
        ),
    )
    before = v_bad_bytes.capture_pre_state()
    assert before.reachable is False
    verdict = v_bad_bytes.verify(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"id": "nl"},
            field="value",
            value="true",
            settle_timeout_s=0.0,
        ),
        before,
    )
    assert verdict.confirmed is False
    assert verdict.verdict == Verdict.INDETERMINATE


# --- macos_calendar_event ---------------------------------------------------


def test_macos_calendar_event_found_and_not_found_and_argv_safety() -> None:
    captured_argv: list[list[str]] = []

    def _runner_found(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        captured_argv.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"FOUND\n", b"")

    src = MacosCalendarEventSource(
        id="cal",
        calendar_name="Work",
        title='Focus "Block" & Sync',
        start_iso="2026-11-24T10:30:00",
    )
    v_found = LocalStateVerifier([src], platform="darwin", runner=_runner_found)
    rec = v_found.read()
    assert rec == [
        {
            "id": "cal",
            "type": "macos_calendar_event",
            "present": True,
            "exists": True,
            "calendar_name": "Work",
            "title": 'Focus "Block" & Sync',
            "start_iso": "2026-11-24T10:30:00",
            "value": "true",
            "value_type": "boolean",
        }
    ]
    # Verify absolute osascript path and argv isolation (no string interpolation)
    assert captured_argv[0][0] == OSASCRIPT_BINARY
    assert captured_argv[0][3:] == [
        "--",
        "Work",
        'Focus "Block" & Sync',
        "2026",
        "11",
        "24",
        "10",
        "30",
    ]
    assert 'Focus "Block" & Sync' not in captured_argv[0][2]

    def _runner_not_found(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"NOT_FOUND\n", b"")

    v_missing = LocalStateVerifier([src], platform="darwin", runner=_runner_not_found)
    rec_missing = v_missing.read()
    assert rec_missing is not None
    assert rec_missing[0]["present"] is False
    assert rec_missing[0]["exists"] is False
    assert rec_missing[0]["value"] == "false"


def test_macos_calendar_event_fails_closed_on_errors_and_wrong_os() -> None:
    src = MacosCalendarEventSource(
        id="cal",
        calendar_name="Calendar",
        title="Standup",
        start_iso="2026-11-24T09:00:00",
    )

    # Wrong OS
    v_wrong = LocalStateVerifier(
        [src],
        platform="win32",
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, b"FOUND\n", b""),
    )
    assert v_wrong.read() is None

    # Missing calendar container
    v_no_cal = LocalStateVerifier(
        [src],
        platform="darwin",
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, b"NO_CALENDAR\n", b""),
    )
    assert v_no_cal.read() is None
    assert v_no_cal.last_error is not None and "does not exist" in v_no_cal.last_error

    # osascript error (e.g. Automation permission denied)
    v_err = LocalStateVerifier(
        [src],
        platform="darwin",
        runner=lambda argv: subprocess.CompletedProcess(argv, 1, b"", b"denied"),
    )
    assert v_err.read() is None

    # Unexpected stdout
    v_weird = LocalStateVerifier(
        [src],
        platform="darwin",
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, b"MAYBE\n", b""),
    )
    assert v_weird.read() is None


def test_macos_calendar_event_validation_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="control characters"):
        MacosCalendarEventSource(
            id="cal",
            calendar_name="Calendar\nBad",
            title="Ok",
            start_iso="2026-11-24T09:00:00",
        )
    with pytest.raises(ValueError, match="ISO-8601"):
        MacosCalendarEventSource(
            id="cal",
            calendar_name="Calendar",
            title="Ok",
            start_iso="not-a-timestamp",
        )


def test_verify_config_accepts_all_three_new_source_types(tmp_path: Path) -> None:
    for doc in (
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "local-state",
                "local_state": [{"type": "macos_night_shift", "id": "ns"}],
            },
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": "ns"},
                    "field": "value",
                    "value": "true",
                }
            ],
        },
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "local-state",
                "local_state": [{"type": "windows_night_light", "id": "nl"}],
            },
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": "nl"},
                    "field": "value",
                    "value": "false",
                }
            ],
        },
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "local-state",
                "local_state": [
                    {
                        "type": "macos_calendar_event",
                        "id": "cal",
                        "calendar_name": "Calendar",
                        "title": "Deep Work",
                        "start_iso": "2026-11-24T10:00:00",
                    }
                ],
            },
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": "cal"},
                    "field": "value",
                    "value": "true",
                }
            ],
        },
    ):
        cfg = VerifyConfig.model_validate(doc)
        assert cfg.verifier.kind == "local-state"
