"""Locale, date order and keyboard layout as checked replay invariants (#1897).

A worklist date like ``03/04/2026`` is ambiguous, and keycode replay on a
non-matching keyboard layout types the wrong punctuation. Recordings may
carry :class:`LocaleInvariants`; before replay the runtime compares them to
the current environment and refuses on a mismatch, unless every typed step
sets values semantically (DOM/accessibility value) rather than by keystrokes.

Observing the current environment uses only the Python ``locale`` module and
optional explicit overrides — no GUI is opened.
"""

from __future__ import annotations

import locale
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DateOrder = Literal["mdy", "dmy", "ymd"]

__all__ = [
    "DateOrder",
    "LocaleInvariantMismatch",
    "LocaleInvariantResult",
    "LocaleInvariants",
    "check_locale_invariants",
    "observe_locale_invariants",
    "worklist_date_needs_explicit_format",
]


class LocaleInvariants(BaseModel):
    """Environment facts recorded with a demonstration and checked at replay."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    locale: str = Field(
        min_length=2,
        max_length=32,
        description="BCP-47 / POSIX locale id, e.g. en_US, fr_CA, de_DE.",
    )
    date_order: DateOrder = Field(
        description="How numeric dates are ordered: mdy, dmy, or ymd.",
    )
    keyboard_layout: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Keyboard layout id as the OS reports it (e.g. us, fr, "
            "ca-multilingual, de)."
        ),
    )

    @field_validator("locale", "keyboard_layout")
    @classmethod
    def _normalize_token(cls, value: str) -> str:
        text = value.strip().replace("-", "_")
        if not text:
            raise ValueError("must not be empty")
        return text.lower()


class LocaleInvariantMismatch(RuntimeError):
    """Current locale/date-order/keyboard layout disagrees with the recording."""

    def __init__(self, reason: str, *, mismatched: list[str]):
        super().__init__(reason)
        self.reason = reason
        self.mismatched = list(mismatched)


@dataclass(frozen=True)
class LocaleInvariantResult:
    ok: bool
    mismatched: tuple[str, ...] = ()
    reason: str = ""

    def raise_if_refused(self) -> None:
        if not self.ok:
            raise LocaleInvariantMismatch(self.reason, mismatched=list(self.mismatched))


def _norm_locale(value: str) -> str:
    return value.strip().replace("-", "_").lower()


def _locale_family(value: str) -> str:
    """``en_us.utf-8`` -> ``en_us``; compare without encoding suffix."""

    base = _norm_locale(value).split(".", 1)[0]
    return base


def check_locale_invariants(
    recorded: LocaleInvariants,
    current: LocaleInvariants,
    *,
    keystroke_steps: bool = True,
) -> LocaleInvariantResult:
    """Compare recorded invariants to the current environment.

    When ``keystroke_steps`` is False (every input step sets values
    semantically), a keyboard-layout mismatch is allowed; locale and date
    order still must match because worklist parsing and masked date fields
    depend on them.
    """

    mismatched: list[str] = []
    if _locale_family(recorded.locale) != _locale_family(current.locale):
        mismatched.append("locale")
    if recorded.date_order != current.date_order:
        mismatched.append("date_order")
    if keystroke_steps and (
        _norm_locale(recorded.keyboard_layout) != _norm_locale(current.keyboard_layout)
    ):
        mismatched.append("keyboard_layout")
    if mismatched:
        return LocaleInvariantResult(
            ok=False,
            mismatched=tuple(mismatched),
            reason=(
                "recording environment does not match this computer: "
                + ", ".join(mismatched)
                + " (refuse rather than type the wrong punctuation or date)"
            ),
        )
    return LocaleInvariantResult(ok=True, reason="locale invariants match")


def observe_locale_invariants(
    *,
    date_order: Optional[DateOrder] = None,
    keyboard_layout: Optional[str] = None,
    locale_name: Optional[str] = None,
) -> LocaleInvariants:
    """Read the current environment without opening a GUI.

    ``date_order`` and ``keyboard_layout`` must be supplied by the caller or by
    the OS-specific capture layer when available; this helper only invents a
    date order from the locale's ``d_fmt`` when ``date_order`` is omitted, and
    defaults ``keyboard_layout`` to the locale language tag when unset (a
    conservative placeholder until capture fills the real layout id).
    """

    if locale_name is None:
        lang, _enc = locale.getlocale(locale.LC_TIME)
        if not lang:
            lang, _enc = locale.getlocale()
        locale_name = lang or "en_US"
    order = date_order
    if order is None:
        try:
            d_fmt = locale.nl_langinfo(locale.D_FMT)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            d_fmt = ""
        order = _date_order_from_d_fmt(d_fmt) or "mdy"
    layout = keyboard_layout or _locale_family(locale_name).split("_", 1)[0] or "us"
    return LocaleInvariants(
        locale=locale_name, date_order=order, keyboard_layout=layout
    )


def _date_order_from_d_fmt(d_fmt: str) -> Optional[DateOrder]:
    text = d_fmt.lower()
    if not text:
        return None
    # Prefer the first day/month/year marker that appears.
    positions = {
        "d": min((i for i, c in enumerate(text) if c == "d"), default=10**9),
        "m": min((i for i, c in enumerate(text) if c == "m"), default=10**9),
        "y": min((i for i, c in enumerate(text) if c == "y"), default=10**9),
    }
    order = "".join(sorted(positions, key=positions.get))  # type: ignore[arg-type]
    mapping = {"mdy": "mdy", "dmy": "dmy", "ymd": "ymd"}
    return mapping.get(order)  # type: ignore[return-value]


def worklist_date_needs_explicit_format(raw: str) -> bool:
    """True when a numeric date is ambiguous without a declared order.

    Worklist parsing must never guess ``03/04/2026``. Named-month and ISO
    dates are fine; equal day/month parts and day>12 clues are unambiguous.
    """

    import re

    value = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    if re.search(r"[A-Za-z]", value):
        return False
    m = re.fullmatch(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})", value)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(3))
    if a > 12 or b > 12 or a == b:
        return False
    return True


def workflow_uses_keystroke_typing(workflow: Any) -> bool:
    """True when any step may type by keycode rather than a semantic value set."""

    from openadapt_flow.ir import ActionKind

    semantic = False  # reserved for a future Step.input_mode flag
    _ = semantic
    for step in getattr(workflow, "steps", None) or ():
        kind = getattr(step, "action", None) or getattr(step, "kind", None)
        value = getattr(kind, "value", kind)
        if value in (
            ActionKind.TYPE.value,
            ActionKind.KEY.value,
            ActionKind.HOTKEY.value,
            "type",
            "key",
            "hotkey",
        ):
            # A step that sets a DOM/accessibility value is still typed as TYPE
            # today; until input_mode is recorded, treat TYPE as keystroke.
            return True
    return False


def enforce_workflow_locale_invariants(
    workflow: Any,
    *,
    current: Optional[LocaleInvariants] = None,
) -> None:
    """Raise :class:`LocaleInvariantMismatch` when a bound workflow disagrees."""

    recorded = getattr(workflow, "locale_invariants", None)
    if recorded is None:
        return
    current = current or observe_locale_invariants()
    keystroke = workflow_uses_keystroke_typing(workflow)
    check_locale_invariants(
        recorded, current, keystroke_steps=keystroke
    ).raise_if_refused()
