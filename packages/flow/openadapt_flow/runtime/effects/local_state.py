"""Read-only local-state :class:`EffectVerifier`: files and OS settings.

Some workflows change state that no application database holds: a document
saved to disk, or an operating-system setting such as the appearance mode.
The screen showing the change proves nothing. This verifier reads that state
back through the operating system's own supported, read-only interfaces, so
a run can be checked the same way a REST or SQL write is checked.

Each **source** is a typed, closed description of one piece of state. There is
no free-form command: the verifier decides how to read each type, and every
read is read-only.

``file``
    A path on this computer. Read with ordinary file I/O, never opened for
    writing.
``macos_defaults``
    A preference ``key`` in a ``domain`` (``NSGlobalDomain`` or a bundle id).
    Read with ``/usr/bin/defaults export <domain> -``, the supported reader of
    the preferences system. It goes through ``cfprefsd``, so it sees the
    current value, not a stale plist on disk.
``windows_registry``
    A value ``name`` under ``hive`` (``HKCU`` or ``HKLM``) and ``path``. Read
    with :mod:`winreg` and ``KEY_READ`` access only.

Each source flattens to one record, keyed by the source's ``id``:

* every source: ``id``, ``type`` and ``present`` (``True`` or ``False``);
* ``file`` adds ``size``, ``sha256`` and ``text`` (the contents decoded as
  UTF-8 when they are at most :data:`TEXT_LIMIT` bytes and valid UTF-8, else
  ``None``);
* ``macos_defaults`` and ``windows_registry`` add ``value`` (the value as
  text: see :func:`value_text`) and ``value_type``.

So an example declares its expected change with the shared effect kinds::

    effects:
      - kind: field_equals
        match: {id: note}
        field: text
        value: {param: note}

A missing file, preference key or registry value is a real, readable state
(``present: False``): an expected state of "this setting is off" can be the
key's absence. Absence counts only inside a container that exists: a file's
folder, a non-empty preferences domain (``defaults export`` prints an empty
dictionary for a domain that doesn't exist), a registry key. Otherwise, and
for any other failure (wrong operating system, permission denied,
``defaults`` exiting non-zero, output that does not parse), the whole read is
unreadable, so the verdict is INDETERMINATE and the run halts. The
verifier never guesses a value it could not read.

The tier is ``INDEPENDENT_SYSTEM``: the read goes to the store the state
lives in (the file system, the preferences system, the registry) through a
separate process or API, not through the screen that showed the change.

No model calls, no network, and nothing here writes.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import plistlib
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from sys import platform as _SYS_PLATFORM
from typing import Any, Callable, Optional

from openadapt_flow.local_state_sources import (  # noqa: F401 - re-exported
    FileSource,
    LocalStateSource,
    MacosCalendarEventSource,
    MacosDefaultsSource,
    MacosNightShiftSource,
    WindowsNightLightSource,
    WindowsRegistrySource,
)
from openadapt_flow.runtime.effects.adapter import (
    VerifierAdapterBase,
    poll_until_settled,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

#: A file larger than this many bytes is hashed, but its ``text`` is ``None``.
TEXT_LIMIT = 1 << 20  # 1 MiB
# A local store larger than this is not read: it is unreadable, not absent.
FILE_LIMIT = 256 * 1024 * 1024

#: The supported macOS preferences reader. An absolute path, so ``PATH``
#: cannot substitute another program.
DEFAULTS_BINARY = "/usr/bin/defaults"

#: The supported macOS AppleScript runner, used only with a fixed read-only
#: script and argv parameters (never interpolated user strings).
OSASCRIPT_BINARY = "/usr/bin/osascript"

#: Fixed HKCU registry path holding Windows 10/11 Night Light CloudStore state.
WINDOWS_NIGHT_LIGHT_REG_PATH = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate"
)
WINDOWS_NIGHT_LIGHT_VALUE_NAME = "Data"
_WINDOWS_NIGHT_LIGHT_STATE_BYTE_INDEX = 18
_WINDOWS_NIGHT_LIGHT_ENABLED_BYTE = 0x15
_WINDOWS_NIGHT_LIGHT_DISABLED_BYTE = 0x13

# Fixed read-only AppleScript: receives (calendar_name, title, year, month,
# day, hour, minute) via argv and never creates, modifies, or deletes events.
_MACOS_CALENDAR_READ_SCRIPT = """
on run argv
    set calName to item 1 of argv
    set targetTitle to item 2 of argv
    set targetYear to (item 3 of argv) as integer
    set targetMonth to (item 4 of argv) as integer
    set targetDay to (item 5 of argv) as integer
    set targetHour to (item 6 of argv) as integer
    set targetMinute to (item 7 of argv) as integer
    tell application "Calendar"
        if not (exists calendar calName) then
            return "NO_CALENDAR"
        end if
        set matchingEvents to (every event of calendar calName whose summary is targetTitle)
        repeat with ev in matchingEvents
            set sd to start date of ev
            if ((year of sd) is targetYear) and (((month of sd) as integer) is targetMonth) and ((day of sd) is targetDay) and ((hours of sd) is targetHour) and ((minutes of sd) is targetMinute) then
                return "FOUND"
            end if
        end repeat
        return "NOT_FOUND"
    end tell
end run
""".strip()

#: Registry hives a source may read. Per-user settings and machine settings;
#: nothing else is needed for read-back.
REGISTRY_HIVES = ("HKCU", "HKLM")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]
NightShiftReader = Callable[[], bool]


def parse_windows_night_light_bytes(raw: Any) -> bool:
    """Decode Windows Night Light ``Data`` bytes into ``True``/``False``.

    Windows 10/11 stores the Night Light state in byte 18 of the CloudStore
    ``Data`` blob: ``0x15`` when enabled, ``0x13`` when disabled. Any missing,
    truncated, non-bytes, or unexpected byte pattern raises ``_Unreadable``
    (fail-closed).
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _Unreadable("windows_night_light Data value is not binary bytes")
    if len(raw) <= _WINDOWS_NIGHT_LIGHT_STATE_BYTE_INDEX:
        raise _Unreadable(f"windows_night_light Data blob too short ({len(raw)} bytes)")
    state_byte = raw[_WINDOWS_NIGHT_LIGHT_STATE_BYTE_INDEX]
    if state_byte == _WINDOWS_NIGHT_LIGHT_ENABLED_BYTE:
        return True
    if state_byte == _WINDOWS_NIGHT_LIGHT_DISABLED_BYTE:
        return False
    raise _Unreadable(f"windows_night_light unexpected state byte 0x{state_byte:02x}")


def _read_core_brightness_night_shift() -> bool:
    """Read macOS Night Shift enabled state via CoreBrightness (read-only).

    Uses ``CBBlueLightClient.getBlueLightStatus:`` through ``libobjc.A.dylib``
    with zero writes and zero third-party dependencies. Raises ``_Unreadable``
    if the framework, class, or status query is unavailable.
    """

    class _CBTime(ctypes.Structure):
        _fields_ = [("hour", ctypes.c_int), ("minute", ctypes.c_int)]

    class _CBSchedule(ctypes.Structure):
        _fields_ = [("fromTime", _CBTime), ("toTime", _CBTime)]

    class _CBStatusData(ctypes.Structure):
        _fields_ = [
            ("active", ctypes.c_bool),
            ("enabled", ctypes.c_bool),
            ("sunSchedulePermitted", ctypes.c_bool),
            ("mode", ctypes.c_int),
            ("schedule", _CBSchedule),
            ("disableFlags", ctypes.c_ulonglong),
            ("available", ctypes.c_bool),
        ]

    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary(
            "/System/Library/PrivateFrameworks/CoreBrightness.framework/CoreBrightness"
        )
    except OSError as exc:
        raise _Unreadable(f"CoreBrightness unavailable: {exc}") from exc

    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p

    cls = objc.objc_getClass(b"CBBlueLightClient")
    if not cls:
        raise _Unreadable("CBBlueLightClient class not found")

    msg_send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        ("objc_msgSend", objc)
    )
    msg_send_status = ctypes.CFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_CBStatusData),
    )(("objc_msgSend", objc))
    msg_send_void = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(
        ("objc_msgSend", objc)
    )

    alloc_sel = objc.sel_registerName(b"alloc")
    init_sel = objc.sel_registerName(b"init")
    get_sel = objc.sel_registerName(b"getBlueLightStatus:")
    release_sel = objc.sel_registerName(b"release")

    allocated = msg_send_id(cls, alloc_sel)
    if not allocated:
        raise _Unreadable("CBBlueLightClient alloc returned null")
    client = msg_send_id(allocated, init_sel)
    if not client:
        raise _Unreadable("CBBlueLightClient init returned null")
    try:
        status = _CBStatusData()
        ok = bool(msg_send_status(client, get_sel, ctypes.byref(status)))
        if not ok:
            raise _Unreadable("CBBlueLightClient.getBlueLightStatus: returned false")
        if not status.available:
            raise _Unreadable("Night Shift is not available on this display")
        return bool(status.enabled)
    finally:
        msg_send_void(client, release_sel)


class _Unreadable(Exception):
    """A source could not be read. The whole read is then unreadable."""


def value_text(value: Any) -> Optional[str]:
    """A setting's value as the text an effect compares against.

    Text stays as it is, a boolean is ``"true"`` or ``"false"``, a number is
    its decimal form, bytes are lowercase hex, and anything else (a list or a
    dictionary from a plist, a multi-string from the registry) is compact JSON
    with sorted keys. ``None`` stays ``None``.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise _Unreadable(
            f"value of type {type(value).__name__} has no text form"
        ) from exc


def _plist_type(value: Any) -> str:
    for kind, name in (
        (bool, "boolean"),
        (int, "integer"),
        (float, "float"),
        (str, "string"),
        (bytes, "data"),
        (list, "array"),
        (dict, "dictionary"),
    ):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _run(argv: list[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(argv, capture_output=True, timeout=10, check=False)


class LocalStateVerifier(VerifierAdapterBase):
    """Verify effects against files and OS settings on this computer.

    Args:
        sources: The typed sources to read, each with a distinct ``id``.
        poll_interval_s: Re-read interval while an effect settles (the OS
            may apply a setting a moment after the click).
        runner: Runs the fixed ``defaults`` argv; injectable for tests.
        registry: The :mod:`winreg` module (or a stand-in for tests);
            ``None`` imports it, and its absence makes registry sources
            unreadable.
        platform: ``sys.platform`` override for tests.
        session: Unused; present for protocol symmetry.
    """

    substrate = "local_state"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        sources: list[LocalStateSource],
        *,
        poll_interval_s: float = 0.25,
        runner: Optional[Runner] = None,
        registry: Any = None,
        platform: Optional[str] = None,
        night_shift_reader: Optional[NightShiftReader] = None,
        session: Any = None,
    ) -> None:
        if not sources:
            raise ValueError("effects.kind 'local-state' needs at least one source")
        ids = [s.id for s in sources]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                "local-state source ids must be distinct: " + ", ".join(duplicates)
            )
        self.sources = list(sources)
        self.poll_interval_s = poll_interval_s
        self._runner = runner or _run
        self._registry = registry
        self._platform = platform or _SYS_PLATFORM
        self._night_shift_reader = (
            night_shift_reader or _read_core_brightness_night_shift
        )
        #: Why the last read was unreadable, for the verdict's detail.
        self.last_error: Optional[str] = None

    # -- reads ---------------------------------------------------------------

    def _read_file(self, source: FileSource) -> dict[str, Any]:
        path = Path(os.path.expandvars(os.path.expanduser(source.path)))
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        try:
            # Only a regular file is a store. A device, FIFO or socket (or a
            # link to one) could block the open or never reach EOF, so it is
            # unreadable. O_NONBLOCK keeps a FIFO swapped in after the stat
            # from blocking the open; fstat then checks the opened file.
            if not stat.S_ISREG(path.stat().st_mode):
                raise _Unreadable(f"{source.id}: not a regular file")
            # O_RDONLY only: the read never creates or truncates the file.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as fh:
                opened = os.fstat(fh.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise _Unreadable(f"{source.id}: not a regular file")
                if opened.st_size > FILE_LIMIT:
                    raise _Unreadable(f"{source.id}: larger than {FILE_LIMIT} bytes")
                data = fh.read(TEXT_LIMIT + 1)
                digest = hashlib.sha256(data)
                size = len(data)
                for block in iter(lambda: fh.read(1 << 16), b""):
                    digest.update(block)
                    size += len(block)
                    if size > FILE_LIMIT:  # grew while being read
                        raise _Unreadable(
                            f"{source.id}: larger than {FILE_LIMIT} bytes"
                        )
        except FileNotFoundError:
            if not path.parent.is_dir():
                # The folder that should hold the file is gone: that is not
                # "the file was not saved", it is a store we can't see.
                raise _Unreadable(f"{source.id}: folder {path.parent} does not exist")
            record.update(present=False, size=None, sha256=None, text=None)
            return record
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        text: Optional[str] = None
        if size <= TEXT_LIMIT:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        record.update(present=True, size=size, sha256=digest.hexdigest(), text=text)
        return record

    def _read_defaults(
        self, source: MacosDefaultsSource, cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if self._platform != "darwin":
            raise _Unreadable(f"{source.id}: macos_defaults needs macOS")
        domain = cache.get(source.domain)
        if domain is None:
            try:
                done = self._runner([DEFAULTS_BINARY, "export", source.domain, "-"])
            except (OSError, subprocess.SubprocessError) as exc:
                raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
            if done.returncode != 0:
                raise _Unreadable(f"{source.id}: defaults exited {done.returncode}")
            try:
                parsed = plistlib.loads(done.stdout)
            except Exception as exc:  # noqa: BLE001 - any parse failure is unreadable
                raise _Unreadable(
                    f"{source.id}: defaults output is not a plist"
                ) from exc
            if not isinstance(parsed, dict):
                raise _Unreadable(f"{source.id}: defaults output is not a dictionary")
            if not parsed:
                # `defaults export` prints an empty dictionary, exit 0, for a
                # domain that doesn't exist, so a mistyped domain would
                # confirm "absent". An empty domain is unreadable instead.
                raise _Unreadable(f"{source.id}: preferences domain is empty")
            domain = parsed
            cache[source.domain] = domain
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        if source.key not in domain:
            record.update(present=False, value=None, value_type=None)
            return record
        value = domain[source.key]
        record.update(
            present=True, value=value_text(value), value_type=_plist_type(value)
        )
        return record

    def _winreg(self) -> Any:
        if self._registry is not None:
            return self._registry
        if self._platform != "win32":
            raise _Unreadable("windows_registry needs Windows")
        try:
            import winreg  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:
            raise _Unreadable("windows_registry needs Windows") from exc
        return winreg

    def _read_registry(self, source: WindowsRegistrySource) -> dict[str, Any]:
        try:
            reg = self._winreg()
        except _Unreadable as exc:
            raise _Unreadable(f"{source.id}: {exc}") from exc
        hive = getattr(
            reg, "HKEY_CURRENT_USER" if source.hive == "HKCU" else "HKEY_LOCAL_MACHINE"
        )
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        try:
            key = reg.OpenKey(hive, source.path, 0, reg.KEY_READ)
        except FileNotFoundError as exc:
            # A missing key is a mistyped path as often as a real state, so
            # it can't confirm "absent". Only a missing value under a key
            # that exists is a readable absence.
            raise _Unreadable(f"{source.id}: registry key does not exist") from exc
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        try:
            with key:
                value, kind = reg.QueryValueEx(key, source.name)
        except FileNotFoundError:
            record.update(present=False, value=None, value_type=None)
            return record
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        names = {
            getattr(reg, n): n
            for n in (
                "REG_SZ",
                "REG_EXPAND_SZ",
                "REG_MULTI_SZ",
                "REG_DWORD",
                "REG_QWORD",
                "REG_BINARY",
            )
            if hasattr(reg, n)
        }
        record.update(
            present=True, value=value_text(value), value_type=names.get(kind, str(kind))
        )
        return record

    def _read_macos_night_shift(self, source: MacosNightShiftSource) -> dict[str, Any]:
        if self._platform != "darwin":
            raise _Unreadable(f"{source.id}: macos_night_shift needs macOS")
        try:
            enabled = self._night_shift_reader()
        except _Unreadable as exc:
            raise _Unreadable(f"{source.id}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - fail closed on any probe error
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        if not isinstance(enabled, bool):
            raise _Unreadable(f"{source.id}: night shift reader did not return a bool")
        return {
            "id": source.id,
            "type": source.type,
            "present": True,
            "enabled": enabled,
            "value": value_text(enabled),
            "value_type": "boolean",
        }

    def _read_windows_night_light(
        self, source: WindowsNightLightSource
    ) -> dict[str, Any]:
        try:
            reg = self._winreg()
        except _Unreadable as exc:
            raise _Unreadable(f"{source.id}: {exc}") from exc
        hive = reg.HKEY_CURRENT_USER
        try:
            key = reg.OpenKey(hive, WINDOWS_NIGHT_LIGHT_REG_PATH, 0, reg.KEY_READ)
        except FileNotFoundError as exc:
            raise _Unreadable(
                f"{source.id}: Windows Night Light registry key does not exist"
            ) from exc
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        try:
            with key:
                value, _kind = reg.QueryValueEx(key, WINDOWS_NIGHT_LIGHT_VALUE_NAME)
        except FileNotFoundError as exc:
            raise _Unreadable(
                f"{source.id}: Windows Night Light Data value missing"
            ) from exc
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        try:
            enabled = parse_windows_night_light_bytes(value)
        except _Unreadable as exc:
            raise _Unreadable(f"{source.id}: {exc}") from exc
        return {
            "id": source.id,
            "type": source.type,
            "present": True,
            "enabled": enabled,
            "value": value_text(enabled),
            "value_type": "boolean",
        }

    def _read_macos_calendar_event(
        self, source: MacosCalendarEventSource
    ) -> dict[str, Any]:
        if self._platform != "darwin":
            raise _Unreadable(f"{source.id}: macos_calendar_event needs macOS")
        normalized = (
            source.start_iso[:-1] + "+00:00"
            if source.start_iso.endswith("Z")
            else source.start_iso
        )
        try:
            start_dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise _Unreadable(f"{source.id}: invalid start_iso") from exc
        argv = [
            OSASCRIPT_BINARY,
            "-e",
            _MACOS_CALENDAR_READ_SCRIPT,
            "--",
            source.calendar_name,
            source.title,
            str(start_dt.year),
            str(start_dt.month),
            str(start_dt.day),
            str(start_dt.hour),
            str(start_dt.minute),
        ]
        try:
            done = self._runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        if done.returncode != 0:
            raise _Unreadable(f"{source.id}: osascript exited {done.returncode}")
        output = done.stdout.decode("utf-8", errors="replace").strip()
        if output == "NO_CALENDAR":
            raise _Unreadable(
                f"{source.id}: calendar {source.calendar_name!r} does not exist"
            )
        if output == "FOUND":
            exists = True
        elif output == "NOT_FOUND":
            exists = False
        else:
            raise _Unreadable(f"{source.id}: unexpected osascript output {output!r}")
        return {
            "id": source.id,
            "type": source.type,
            "present": exists,
            "exists": exists,
            "calendar_name": source.calendar_name,
            "title": source.title,
            "start_iso": source.start_iso,
            "value": value_text(exists),
            "value_type": "boolean",
        }

    def read(self) -> Optional[list[dict[str, Any]]]:
        """One read of every source, or ``None`` when any source is unreadable."""
        cache: dict[str, dict[str, Any]] = {}
        records: list[dict[str, Any]] = []
        try:
            for source in self.sources:
                if isinstance(source, FileSource):
                    records.append(self._read_file(source))
                elif isinstance(source, MacosDefaultsSource):
                    records.append(self._read_defaults(source, cache))
                elif isinstance(source, WindowsRegistrySource):
                    records.append(self._read_registry(source))
                elif isinstance(source, MacosNightShiftSource):
                    records.append(self._read_macos_night_shift(source))
                elif isinstance(source, WindowsNightLightSource):
                    records.append(self._read_windows_night_light(source))
                else:
                    records.append(self._read_macos_calendar_event(source))
        except _Unreadable as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return records

    def _detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "sources": [s.model_dump() for s in self.sources],
        }
        if self.last_error:
            detail["unreadable"] = self.last_error
        return detail

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self.read()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail=self._detail(),
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = poll_until_settled(
            self.read,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=self.poll_interval_s,
        )
        if self.last_error and not verdict.confirmed:
            verdict = verdict.model_copy(
                update={"reason": f"{verdict.reason} ({self.last_error})"}
            )
        return verdict


__all__ = [
    "DEFAULTS_BINARY",
    "FileSource",
    "LocalStateSource",
    "LocalStateVerifier",
    "MacosCalendarEventSource",
    "MacosDefaultsSource",
    "MacosNightShiftSource",
    "OSASCRIPT_BINARY",
    "REGISTRY_HIVES",
    "TEXT_LIMIT",
    "WINDOWS_NIGHT_LIGHT_REG_PATH",
    "WINDOWS_NIGHT_LIGHT_VALUE_NAME",
    "WindowsNightLightSource",
    "WindowsRegistrySource",
    "parse_windows_night_light_bytes",
    "value_text",
]
