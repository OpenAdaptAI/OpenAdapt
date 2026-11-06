"""Locale, date order and keyboard layout as checked replay invariants (#1897).

A worklist date like ``03/04/2026`` is ambiguous, and keycode replay on a
non-matching keyboard layout types the wrong punctuation. A recording carries
:class:`LocaleInvariants`; before replay the runtime compares them to this
computer and refuses on a mismatch.

Facts come from the OS, not from Python's start-up ``C`` locale:

* macOS: the global ``AppleLocale``, ``AppleICUDateFormatStrings`` and the
  HIToolbox current keyboard layout, read with ``defaults``.
* Windows: ``GetUserDefaultLocaleName``, ``GetLocaleInfoEx`` (short date) and
  ``GetKeyboardLayoutNameW``.
* Linux and other POSIX: the user locale via ``setlocale(LC_TIME, "")`` and
  ``nl_langinfo(D_FMT)``, and the XKB layout from ``XKB_DEFAULT_LAYOUT``,
  ``setxkbmap -query`` or ``localectl status``.

A fact that can't be read is ``None`` (unknown). Unknown is never filled in
with ``en_US``, ``mdy`` or ``us``. At replay, an unknown current value for a
recorded fact refuses. No GUI is opened.
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
    "observe_locale_invariants",
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
    reason: str = ""

    def raise_if_refused(self) -> None:
        if not self.ok:
            raise LocaleInvariantMismatch(
                self.reason, mismatched=[*self.mismatched, *self.unknown]
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

    Each fact the recording holds must be readable here and equal. The
    keyboard layout only matters when a step types by keystroke
    (``keystroke_steps``); locale and date order always matter, because
    worklist parsing and masked date fields depend on them. A fact the
    recording could not read itself is not compared.
    """

    names = ["locale", "date_order"] + (["keyboard_layout"] if keystroke_steps else [])
    mismatched: list[str] = []
    unknown: list[str] = []
    for name in names:
        want = getattr(recorded, name)
        if want is None:
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
    if mismatched or unknown:
        parts = []
        if mismatched:
            parts.append("does not match the recording: " + ", ".join(mismatched))
        if unknown:
            parts.append("could not be read on this computer: " + ", ".join(unknown))
        return LocaleInvariantResult(
            ok=False,
            mismatched=tuple(mismatched),
            unknown=tuple(unknown),
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

_Run = Callable[[list[str]], Optional[str]]
_LOCALE_LOCK = threading.Lock()


def _run(cmd: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3, check=False
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


def _macos(run: _Run) -> LocaleInvariants:
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
    layout_raw = (
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
    layout = layout_raw.rsplit(".", 1)[-1] if layout_raw else None
    return LocaleInvariants(locale=name, date_order=order, keyboard_layout=layout)


def _windows() -> LocaleInvariants:  # pragma: no cover - exercised on Windows CI
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
    klid = ctypes.create_unicode_buffer(9)
    if user32.GetKeyboardLayoutNameW(klid):
        layout = klid.value or None
    return LocaleInvariants(locale=name, date_order=order, keyboard_layout=layout)


def _layout_from(text: str, layout_re: str, variant_re: str) -> Optional[str]:
    layout = re.search(layout_re, text, re.M)
    if layout is None:
        return None
    variant = re.search(variant_re, text, re.M)
    return layout.group(1) + (f"+{variant.group(1)}" if variant else "")


def _xkb_layout(run: _Run, env: dict[str, str]) -> Optional[str]:
    if env.get("XKB_DEFAULT_LAYOUT"):
        variant = env.get("XKB_DEFAULT_VARIANT")
        return env["XKB_DEFAULT_LAYOUT"] + (f"+{variant}" if variant else "")
    if env.get("DISPLAY"):
        found = _layout_from(
            run(["setxkbmap", "-query"]) or "",
            r"^layout:\s*(\S+)",
            r"^variant:\s*(\S+)",
        )
        if found:
            return found
    return _layout_from(
        run(["localectl", "status"]) or "",
        r"X11 Layout:\s*(\S+)",
        r"X11 Variant:\s*(\S+)",
    )


def _posix(run: _Run, env: dict[str, str]) -> LocaleInvariants:
    name, order = _posix_user_time_locale()
    return LocaleInvariants(
        locale=_locale_family(name) if name else None,
        date_order=order,
        keyboard_layout=_xkb_layout(run, env),
    )


def observe_locale_invariants(
    *,
    date_order: Optional[DateOrder] = None,
    keyboard_layout: Optional[str] = None,
    locale_name: Optional[str] = None,
    platform: Optional[str] = None,
    run: Optional[_Run] = None,
    env: Optional[dict[str, str]] = None,
) -> LocaleInvariants:
    """Read this computer's locale, date order and keyboard layout.

    Explicit arguments override what the OS reports. A fact the OS doesn't
    report stays ``None``.
    """

    plat = platform or sys.platform
    runner = run or _run
    environ = dict(os.environ if env is None else env)
    try:
        if plat == "darwin":
            seen = _macos(runner)
        elif plat == "win32":
            seen = _windows()
        else:
            seen = _posix(runner, environ)
    except Exception:  # noqa: BLE001 - an unreadable fact is unknown, not a guess
        seen = LocaleInvariants()
    return LocaleInvariants(
        locale=locale_name if locale_name is not None else seen.locale,
        date_order=date_order if date_order is not None else seen.date_order,
        keyboard_layout=(
            keyboard_layout if keyboard_layout is not None else seen.keyboard_layout
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
    invariants disagree with this computer or can't be checked here."""

    recorded = getattr(workflow, "locale_invariants", None)
    if recorded is None:
        return
    current = current or observe_locale_invariants()
    keystroke = workflow_uses_keystroke_typing(workflow)
    check_locale_invariants(
        recorded, current, keystroke_steps=keystroke
    ).raise_if_refused()
