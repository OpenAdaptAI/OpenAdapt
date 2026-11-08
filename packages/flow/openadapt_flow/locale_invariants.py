"""Locale, date order and keyboard layout as checked replay invariants (#1897).

A worklist date like ``03/04/2026`` is ambiguous, and keycode replay on a
non-matching keyboard layout types the wrong punctuation. A recording carries
:class:`LocaleInvariants`; before replay the runtime compares them to this
computer and refuses on a mismatch.

The recording's facts are read once by the capture producer when recording
starts (``openadapt_capture.locale_facts``), persisted in the recording, and
carried unchanged through conversion and compile. A converter never puts its
own computer's facts on a recording.

Facts come from the OS, not from Python's start-up ``C`` locale:

* macOS: the global ``AppleLocale`` and ``AppleICUDateFormatStrings``, and the
  current keyboard layout and input method from Text Input Sources (in a
  child interpreter; HIToolbox's saved current layout when that can't run).
* Windows: ``GetUserDefaultLocaleName``, ``GetLocaleInfoEx`` (short date), and
  ``GetKeyboardLayout`` for the foreground window's thread, mapped to a KLID.
* Linux and other POSIX: the user locale via ``setlocale(LC_TIME, "")`` and
  ``nl_langinfo(D_FMT)``, and the layout of the X server's active XKB group
  (``XkbGetState`` with ``setxkbmap -query``).

Limits: the layout is that of whatever has keyboard focus when it is read.
At recording time that is the recorded application. At replay the runtime
checks once before any action and again after focus, immediately before
keyboard delivery, so a layout change caused by the focusing click is
caught. Wayland has no common API for the active layout, and
``XKB_DEFAULT_LAYOUT``/``localectl`` name configuration, not the active
group, so on Wayland, or with several X layouts and no readable group, the
layout is unknown.

A fact that can't be read is ``None`` (unknown). Unknown is never filled in
with ``en_US``, ``mdy`` or ``us``. A workflow with no binding at all is a
recording made before #1897 and is not checked. A bound workflow refuses
replay when any fact it needs is unknown, on either side. No GUI is opened.
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DateOrder = Literal["mdy", "dmy", "ymd"]

__all__ = [
    "DateOrder",
    "LocaleInvariantMismatch",
    "LocaleInvariantResult",
    "LocaleInvariants",
    "check_locale_invariants",
    "date_order_from_format",
    "enforce_workflow_locale_invariants",
    "klid_from_hkl",
    "observe_locale_invariants",
    "read_os_locale_facts",
    "workflow_uses_keystroke_typing",
    "worklist_date_needs_explicit_format",
    "worklist_number_needs_explicit_format",
]


class LocaleInvariants(BaseModel):
    """Environment facts recorded with a demonstration and checked at replay.

    ``None`` means the fact could not be read on that computer.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    locale: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=64,
        description="BCP-47 / POSIX locale id, e.g. en_US, fr_CA, de_DE.",
    )
    date_order: Optional[DateOrder] = Field(
        default=None,
        description="How the OS orders numeric dates: mdy, dmy, or ymd.",
    )
    keyboard_layout: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "Keyboard layout id as the OS reports it (macOS input source, "
            "Windows KLID, or XKB layout[+variant])."
        ),
    )

    @field_validator("locale", "keyboard_layout")
    @classmethod
    def _normalize_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip().replace("-", "_")
        if not text:
            raise ValueError("must not be empty")
        return text.lower()

    def unknown(self) -> list[str]:
        return [
            name
            for name in ("locale", "date_order", "keyboard_layout")
            if getattr(self, name) is None
        ]


class LocaleInvariantMismatch(RuntimeError):
    """This computer's locale, date order or layout disagrees with the recording,
    or can't be read."""

    def __init__(self, reason: str, *, mismatched: list[str]):
        super().__init__(reason)
        self.reason = reason
        self.mismatched = list(mismatched)


@dataclass(frozen=True)
class LocaleInvariantResult:
    ok: bool
    mismatched: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    unrecorded: tuple[str, ...] = ()
    reason: str = ""

    def raise_if_refused(self) -> None:
        if not self.ok:
            raise LocaleInvariantMismatch(
                self.reason,
                mismatched=[*self.mismatched, *self.unknown, *self.unrecorded],
            )


def _norm(value: str) -> str:
    return value.strip().replace("-", "_").lower()


def _locale_family(value: str) -> str:
    """``en_us.utf-8`` / ``de_de@rg=chzzzz`` -> ``en_us`` / ``de_de``."""

    return re.split(r"[.@]", _norm(value), maxsplit=1)[0]


def check_locale_invariants(
    recorded: LocaleInvariants,
    current: LocaleInvariants,
    *,
    keystroke_steps: bool = True,
) -> LocaleInvariantResult:
    """Compare recorded invariants to this computer. Fails closed.

    Each fact that matters must have been read when recording, be readable
    here, and be equal. The keyboard layout only matters when a step types by
    keystroke (``keystroke_steps``); locale and date order always matter,
    because worklist parsing and masked date fields depend on them. A fact
    the recording could not read refuses: there is nothing to check it
    against, and an incomplete observation is not an older, unbound one.
    """

    names = ["locale", "date_order"] + (["keyboard_layout"] if keystroke_steps else [])
    mismatched: list[str] = []
    unknown: list[str] = []
    unrecorded: list[str] = []
    for name in names:
        want = getattr(recorded, name)
        if want is None:
            unrecorded.append(name)
            continue
        have = getattr(current, name)
        if have is None:
            unknown.append(name)
            continue
        if name == "locale":
            same = _locale_family(want) == _locale_family(have)
        else:
            same = _norm(str(want)) == _norm(str(have))
        if not same:
            mismatched.append(name)
    if mismatched or unknown or unrecorded:
        parts = []
        if mismatched:
            parts.append("does not match the recording: " + ", ".join(mismatched))
        if unknown:
            parts.append("could not be read on this computer: " + ", ".join(unknown))
        if unrecorded:
            parts.append("could not be read when recording: " + ", ".join(unrecorded))
        return LocaleInvariantResult(
            ok=False,
            mismatched=tuple(mismatched),
            unknown=tuple(unknown),
            unrecorded=tuple(unrecorded),
            reason="replay refused: "
            + "; ".join(parts)
            + " (refuse rather than type the wrong punctuation or date)",
        )
    return LocaleInvariantResult(ok=True, reason="locale invariants match")


# -- date format parsing ----------------------------------------------------------

_STRFTIME = re.compile(r"%[-_0^#]?([a-zA-Z])")
_STRFTIME_KIND = {
    "d": "d",
    "e": "d",
    "m": "m",
    "b": "m",
    "B": "m",
    "h": "m",
    "y": "y",
    "Y": "y",
    "C": "y",
}


def date_order_from_format(fmt: str) -> Optional[DateOrder]:
    """The day/month/year order of a strftime (``%d.%m.%Y``, ``%e/%m/%y``,
    ``%D``) or ICU/Windows (``M/d/yyyy``, ``dd.MM.yyyy``) date pattern.

    None when the pattern doesn't name all three parts.
    """

    text = (fmt or "").strip()
    if not text:
        return None
    seen: list[str] = []
    if "%" in text:
        text = text.replace("%D", "%m/%d/%y").replace("%F", "%Y-%m-%d")
        for m in _STRFTIME.finditer(text):
            kind = _STRFTIME_KIND.get(m.group(1))
            if kind and kind not in seen:
                seen.append(kind)
    else:
        # Drop quoted literals, then read runs of pattern letters. A run of
        # three or more d/E is a weekday name, not the day of the month.
        text = re.sub(r"'[^']*'", "", text)
        for m in re.finditer(r"([dMLyuE])\1*", text):
            letter, size = m.group(1), len(m.group(0))
            if letter == "E" or (letter == "d" and size >= 3):
                continue
            kind = {"d": "d", "M": "m", "L": "m", "y": "y", "u": "y"}[letter]
            if kind not in seen:
                seen.append(kind)
    order = "".join(seen)
    if order in ("mdy", "dmy", "ymd"):
        return order  # type: ignore[return-value]
    return None


# -- OS readers (no GUI) ------------------------------------------------------------
#
# Kept in step with openadapt_capture.locale_facts (the recording-time
# producer); tests/test_locale_invariants.py checks the two agree.

_Run = Callable[[list[str]], Optional[str]]
_GroupReader = Callable[[str], Optional[int]]
_LOCALE_LOCK = threading.Lock()


def _run(cmd: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _posix_user_time_locale() -> tuple[Optional[str], Optional[DateOrder]]:
    """The user's LC_TIME locale and its date order, read after
    ``setlocale(LC_TIME, "")`` and restored afterwards.

    ``C``/``POSIX`` is "not configured", so both come back unknown.
    """

    with _LOCALE_LOCK:
        saved = locale.setlocale(locale.LC_TIME)
        try:
            try:
                name = locale.setlocale(locale.LC_TIME, "")
            except locale.Error:
                return None, None
            if not name or name in ("C", "POSIX") or name.startswith("C."):
                return None, None
            try:
                d_fmt = locale.nl_langinfo(locale.D_FMT)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                d_fmt = ""
        finally:
            locale.setlocale(locale.LC_TIME, saved)
    return name, date_order_from_format(d_fmt)


def _posix_date_order_for(name: str) -> Optional[DateOrder]:
    with _LOCALE_LOCK:
        saved = locale.setlocale(locale.LC_TIME)
        try:
            for candidate in (f"{name}.UTF-8", name):
                try:
                    locale.setlocale(locale.LC_TIME, candidate)
                except locale.Error:
                    continue
                try:
                    fmt = locale.nl_langinfo(locale.D_FMT)  # type: ignore[attr-defined]
                except (AttributeError, ValueError):
                    return None
                return date_order_from_format(fmt)
            return None
        finally:
            locale.setlocale(locale.LC_TIME, saved)


# The current keyboard layout and input source from Text Input Sources, the
# API the focused application's key events go through. It runs in a child
# interpreter: HIToolbox may assert (and kill the process) when called off
# the main thread, and the replay gate can run on a worker thread.
_TIS_SCRIPT = """
import ctypes
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
hi = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
hi.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
hi.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
hi.TISGetInputSourceProperty.restype = ctypes.c_void_p
hi.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cf.CFStringGetCString.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
]
cf.CFStringGetCString.restype = ctypes.c_bool
cf.CFRelease.argtypes = [ctypes.c_void_p]
key = ctypes.c_void_p.in_dll(hi, "kTISPropertyInputSourceID")
def ident(src):
    if not src:
        return ""
    value = hi.TISGetInputSourceProperty(src, key)
    buf = ctypes.create_string_buffer(512)
    if not value or not cf.CFStringGetCString(value, buf, 512, 0x08000100):
        return ""
    return buf.value.decode()
layout = hi.TISCopyCurrentKeyboardLayoutInputSource()
source = hi.TISCopyCurrentKeyboardInputSource()
try:
    print(ident(layout))
    print(ident(source))
finally:
    for ref in (layout, source):
        if ref:
            cf.CFRelease(ref)
"""


def _macos_layout(run: _Run) -> Optional[str]:
    """The layout the focused application receives keys through.

    Text Input Sources first: the current keyboard layout, plus the current
    input method when one (an IME) sits on top of it. A frozen app can't run
    a child interpreter, so it falls back to HIToolbox's saved current layout.
    With per-document input sources turned on, both name the source of the
    application that has focus at the time of the check.
    """

    lines: list[str] = []
    if not getattr(sys, "frozen", False):
        out = run([sys.executable, "-I", "-c", _TIS_SCRIPT]) or ""
        lines = [line.strip() for line in out.splitlines()]
    layout_id = lines[0] if lines else ""
    if layout_id:
        source_id = lines[1] if len(lines) > 1 else ""
        layout = layout_id.rsplit(".", 1)[-1]
        if source_id and source_id != layout_id:
            layout += f"+{source_id}"
        return layout
    saved = (
        run(
            [
                "defaults",
                "read",
                "com.apple.HIToolbox",
                "AppleCurrentKeyboardLayoutInputSourceID",
            ]
        )
        or ""
    ).strip()
    return saved.rsplit(".", 1)[-1] if saved else None


def _macos(run: _Run) -> tuple[Optional[str], Optional[DateOrder], Optional[str]]:
    raw_locale = (run(["defaults", "read", "-g", "AppleLocale"]) or "").strip()
    name = _locale_family(raw_locale) if raw_locale else None
    order: Optional[DateOrder] = None
    custom = run(["defaults", "read", "-g", "AppleICUDateFormatStrings"]) or ""
    # A user-set short date format: {1 = "dd.MM.yy"; ...}
    m = re.search(r'\b1\s*=\s*"([^"]+)"', custom)
    if m:
        order = date_order_from_format(m.group(1))
    if order is None and name:
        lang, _, region = name.partition("_")
        order = _posix_date_order_for(f"{lang}_{region.upper()}" if region else lang)
    return name, order, _macos_layout(run)


def klid_from_hkl(
    hkl: int, layout_id_to_klid: Callable[[int], Optional[str]]
) -> Optional[str]:
    """The KLID (``00000407``, ``00010409``, ``e0010411``) behind a Windows
    HKL, the per-thread handle ``GetKeyboardLayout`` returns.

    The high word is the layout: ``Fxxx`` is a ``Layout Id`` that the
    registry maps to its KLID, ``Exxx`` is an IME, and anything else is the
    layout's language id. None when the handle is empty or unmappable.
    """

    hkl &= 0xFFFFFFFF
    lang = hkl & 0xFFFF
    device = (hkl >> 16) & 0xFFFF
    if not lang:
        return None
    if device & 0xF000 == 0xF000:
        return layout_id_to_klid(device & 0x0FFF)
    if device & 0xF000 == 0xE000:
        return f"{device:04X}{lang:04X}"
    return f"{(device or lang):08X}"


def _windows_klid_for_layout_id(
    layout_id: int,
) -> Optional[str]:  # pragma: no cover - Windows only
    import winreg

    base = r"SYSTEM\CurrentControlSet\Control\Keyboard Layouts"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:  # type: ignore[attr-defined]
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)  # type: ignore[attr-defined]
            except OSError:
                return None
            index += 1
            try:
                with winreg.OpenKey(root, name) as key:  # type: ignore[attr-defined]
                    value, _ = winreg.QueryValueEx(key, "Layout Id")  # type: ignore[attr-defined]
                if int(str(value), 16) == layout_id:
                    return str(name)
            except (OSError, ValueError):
                continue


def _windows() -> tuple[
    Optional[str], Optional[DateOrder], Optional[str]
]:  # pragma: no cover - exercised on Windows CI
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    name: Optional[str] = None
    order: Optional[DateOrder] = None
    layout: Optional[str] = None
    buf = ctypes.create_unicode_buffer(85)
    if kernel32.GetUserDefaultLocaleName(buf, 85):
        name = buf.value or None
    if name:
        fmt = ctypes.create_unicode_buffer(80)
        LOCALE_SSHORTDATE = 0x1F
        if kernel32.GetLocaleInfoEx(name, LOCALE_SSHORTDATE, fmt, 80):
            order = date_order_from_format(fmt.value)
    # The layout is per thread. Read the foreground window's thread, the one
    # that receives keystrokes, not this process's own thread. No foreground
    # window (a service or locked session) leaves the layout unknown.
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(thread_id) if thread_id else None
        if hkl:
            layout = klid_from_hkl(int(hkl), _windows_klid_for_layout_id)
    return name, order, layout


def _xkb_active_group(display: str) -> Optional[int]:
    """The X server's active (effective) XKB group, via ``XkbGetState``."""

    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("X11")
    if not path:
        return None
    x11 = ctypes.CDLL(path)
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    x11.XkbGetState.restype = ctypes.c_int
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x11.XOpenDisplay(display.encode())
    if not dpy:
        return None
    try:
        # XkbStateRec starts with ``unsigned char group``; 64 bytes covers it.
        state = (ctypes.c_ubyte * 64)()
        XkbUseCoreKbd = 0x0100
        if x11.XkbGetState(dpy, XkbUseCoreKbd, ctypes.byref(state)) != 0:
            return None
        return int(state[0])
    finally:
        x11.XCloseDisplay(dpy)


def _xkb_layout(
    run: _Run, env: dict[str, str], group: _GroupReader = _xkb_active_group
) -> Optional[str]:
    """The XKB layout of the X server's active group.

    The X server's keymap (``setxkbmap -query``) lists the configured
    layouts; ``XkbGetState`` says which group is active. When the group
    can't be read, only a single-layout keymap is unambiguous. Wayland has no
    common API for the compositor's active layout, and ``XKB_DEFAULT_LAYOUT``
    and ``localectl`` describe configuration, not the active group, so those
    cases are unknown.
    """

    if env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE", "").lower() == (
        "wayland"
    ):
        return None
    display = env.get("DISPLAY")
    if not display:
        return None
    query = run(["setxkbmap", "-query"]) or ""
    found = re.search(r"^layout:\s*(\S+)", query, re.M)
    if found is None:
        return None
    layouts = found.group(1).split(",")
    variant = re.search(r"^variant:\s*(\S*)", query, re.M)
    variants = variant.group(1).split(",") if variant else []
    try:
        active = group(display)
    except Exception:  # noqa: BLE001 - an unreadable group is unknown
        active = None
    if active is None:
        if len(layouts) != 1:
            return None
        active = 0
    if active >= len(layouts) or not layouts[active]:
        return None
    chosen = variants[active] if active < len(variants) else ""
    return layouts[active] + (f"+{chosen}" if chosen else "")


def _posix(
    run: _Run, env: dict[str, str], group: _GroupReader
) -> tuple[Optional[str], Optional[DateOrder], Optional[str]]:
    name, order = _posix_user_time_locale()
    return (
        _locale_family(name) if name else None,
        order,
        _xkb_layout(run, env, group),
    )


def read_os_locale_facts(
    *,
    platform: Optional[str] = None,
    run: Optional[_Run] = None,
    env: Optional[dict[str, str]] = None,
    xkb_group: Optional[_GroupReader] = None,
) -> dict[str, Optional[str]]:
    """This computer's locale, date order and the keyboard layout of the
    input target, as plain normalized strings. Never raises; a fact that
    can't be read is ``None``."""

    plat = platform or sys.platform
    runner = run or _run
    environ = dict(os.environ if env is None else env)
    try:
        if plat == "darwin":
            name, order, layout = _macos(runner)
        elif plat == "win32":
            name, order, layout = _windows()
        else:
            name, order, layout = _posix(
                runner, environ, xkb_group or _xkb_active_group
            )
    except Exception:  # noqa: BLE001 - an unreadable fact is unknown, not a guess
        name, order, layout = None, None, None
    return {
        "locale": _norm(name) if name else None,
        "date_order": order,
        "keyboard_layout": _norm(layout) if layout else None,
    }


def observe_locale_invariants(
    *,
    date_order: Optional[DateOrder] = None,
    keyboard_layout: Optional[str] = None,
    locale_name: Optional[str] = None,
    platform: Optional[str] = None,
    run: Optional[_Run] = None,
    env: Optional[dict[str, str]] = None,
    xkb_group: Optional[_GroupReader] = None,
) -> LocaleInvariants:
    """Read this computer's locale, date order and the input target's
    keyboard layout.

    Explicit arguments override what the OS reports. A fact the OS doesn't
    report stays ``None``.
    """

    seen = read_os_locale_facts(
        platform=platform, run=run, env=env, xkb_group=xkb_group
    )
    try:
        observed = LocaleInvariants.model_validate(seen)
    except ValueError:
        observed = LocaleInvariants()
    return LocaleInvariants(
        locale=locale_name if locale_name is not None else observed.locale,
        date_order=date_order if date_order is not None else observed.date_order,
        keyboard_layout=(
            keyboard_layout if keyboard_layout is not None else observed.keyboard_layout
        ),
    )


# -- worklist values -------------------------------------------------------------


def worklist_date_needs_explicit_format(raw: str) -> bool:
    """True unless a worklist date can be read without a declared order.

    Fails closed: ISO ``YYYY-MM-DD`` and dates with a month name are fine, and
    so is a numeric date with a four-digit year whose order is forced (a part
    above 12, or day equal to month). Anything else that looks numeric, such
    as ``03/04/2026``, ``03/04/26``, ``03.04.2026`` or ``3-4-26``, needs a
    declared format.
    """

    value = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    if re.search(r"[A-Za-z]", value):
        return False
    m = re.fullmatch(r"(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})", value)
    if m:
        a, b = int(m.group(1)), int(m.group(3))
        return not (a > 12 or b > 12 or a == b)
    return bool(re.search(r"\d", value))


def worklist_number_needs_explicit_format(raw: str) -> bool:
    """True when a number's decimal separator can't be told from grouping.

    ``1.234,56`` and ``1,234.56`` use both separators and are unambiguous. A
    single separator followed by exactly three digits (``1.234``, ``1,234``)
    reads as a thousand or as one-point-something, so it needs a declared
    format.
    """

    value = raw.strip().lstrip("+-")
    if "," in value and "." in value:
        return False
    return bool(re.fullmatch(r"\d{1,3}[.,]\d{3}", value))


# -- workflow gate ----------------------------------------------------------------


def workflow_uses_keystroke_typing(workflow: Any) -> bool:
    """True when any step may type by keycode.

    There is no recorded semantic-input mode yet, so every TYPE, KEY and
    HOTKEY step counts as keystroke input. That is the conservative reading.
    """

    from openadapt_flow.ir import ActionKind

    for step in getattr(workflow, "steps", None) or ():
        kind = getattr(step, "action", None) or getattr(step, "kind", None)
        value = getattr(kind, "value", kind)
        if value in (
            ActionKind.TYPE.value,
            ActionKind.KEY.value,
            ActionKind.HOTKEY.value,
        ):
            return True
    return False


def enforce_workflow_locale_invariants(
    workflow: Any,
    *,
    current: Optional[LocaleInvariants] = None,
) -> None:
    """Raise :class:`LocaleInvariantMismatch` when a workflow's recorded
    invariants disagree with this computer or can't be checked.

    ``None`` means a recording from before #1897, which carries no binding.
    A binding whose facts are unknown (all of them, even) is a new recording
    whose observation was incomplete, and it refuses.
    """

    recorded = getattr(workflow, "locale_invariants", None)
    if recorded is None:
        return
    current = current or observe_locale_invariants()
    keystroke = workflow_uses_keystroke_typing(workflow)
    check_locale_invariants(
        recorded, current, keystroke_steps=keystroke
    ).raise_if_refused()
