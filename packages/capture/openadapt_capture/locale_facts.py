"""Locale, date order and keyboard layout at recording time (#1897).

Read once when a recording starts and persisted in the recording's config
under ``capture_locale``, so a converter carries the recording computer's
facts instead of reading its own. A fact the OS doesn't report is ``None``;
it is never filled in with ``en_US``, ``mdy`` or ``us``.

The readers match ``openadapt_flow.locale_invariants``, which reads the same
facts at replay; Flow's tests check the two agree. Standard library only, so
capture gains no dependency.

* macOS: ``AppleLocale``, ``AppleICUDateFormatStrings``, and the current
  keyboard layout and input method from Text Input Sources.
* Windows: ``GetUserDefaultLocaleName``, ``GetLocaleInfoEx`` (short date), and
  the HKL of the foreground window's thread.
* Linux: the user locale via ``setlocale(LC_TIME, "")``, and the X server's
  active XKB group. Wayland is unknown.
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys
import threading
from typing import Callable, Literal, Optional

DateOrder = Literal["mdy", "dmy", "ymd"]

__all__ = ["DateOrder", "observe_locale_facts", "read_os_locale_facts"]


def _norm(value: str) -> str:
    return value.strip().replace("-", "_").lower()


def _locale_family(value: str) -> str:
    """``en_us.utf-8`` / ``de_de@rg=chzzzz`` -> ``en_us`` / ``de_de``."""

    return re.split(r"[.@]", _norm(value), maxsplit=1)[0]


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
# Kept in step with openadapt_flow.locale_invariants (the replay-time
# reader); Flow's tests/test_locale_invariants.py checks the two agree.

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


def observe_locale_facts() -> dict[str, Optional[str]]:
    """The facts to persist with a new recording: ``locale``, ``date_order``
    and ``keyboard_layout``, each a normalized string or ``None``."""

    return read_os_locale_facts()
