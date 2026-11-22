"""Detectors: should this run stop and ask a person?

Pure functions over what an agent can already see: the screen's text, an
accessibility snapshot, a DOM snapshot when there is one, and timing. Each
detector returns an exception kind (``openadapt_types.exception_card``) or
``None``. Nothing here reads an app, names an app, or drives anything; a
caller (a guard, a harness, the agent itself through ``raise_exception``)
decides what to do with the kind.

The signals are general:

* ``second_factor``: an input marked ``autocomplete=one-time-code``, or a
  short numeric input labelled with a word like *code* or *verification*.
* ``session_expired``: a password field after the run has reached a screen
  where it could save (the caller says when that happened).
* ``unexpected_dialog``: a modal on top of the page, covering the target.
* ``timeout_or_stall``: no action and no screen change for N seconds.
* ``possible_duplicate``: a row already read from the app matches the
  proposed record on every field of ``record.duplicate_key`` (verify.yaml).

Uncertain means ask, not go on. A detector that can't tell (a modal with no
geometry, a duplicate read that failed, a stall clock with no last event)
returns its kind.

Elements are plain dicts, so any snapshot can be mapped to them:
``tag``, ``type``, ``role``, ``name``, ``id``, ``label``, ``placeholder``,
``autocomplete``, ``inputmode``, ``pattern``, ``maxlength``, ``context``
(text just before the element), ``visible`` (default true), ``modal``,
``bbox`` (``[x, y, width, height]``) and ``z``. :func:`elements_from_html`
and :func:`elements_from_accessibility` build them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional

Element = Mapping[str, Any]

#: Words that make a short input a second-factor code (any language a deployer
#: adds goes in ``code_words``; these are the defaults).
CODE_WORDS = (
    "code",
    "verification",
    "verify",
    "passcode",
    "one-time",
    "one time",
    "otp",
    "2fa",
    "two-factor",
    "two factor",
    "authenticator",
    "security key",
)
#: Screen text that says a code was sent, whatever the field looks like.
CODE_PHRASES = (
    "verification code",
    "one-time code",
    "one-time password",
    "enter the code",
    "two-factor",
    "authentication code",
    "security code",
)
SESSION_PHRASES = (
    "session expired",
    "session has expired",
    "signed out",
    "sign in again",
    "log in again",
    "please log in",
    "please sign in",
)
DIALOG_ROLES = frozenset({"dialog", "alertdialog"})
_TEXT_INPUT_TYPES = frozenset({"", "text", "tel", "number", "search"})
_VOID = frozenset(
    {
        "input",
        "br",
        "img",
        "meta",
        "link",
        "hr",
        "area",
        "base",
        "col",
        "embed",
        "source",
        "wbr",
    }
)
DEFAULT_STALL_S = 120.0


def _s(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _visible(element: Element) -> bool:
    return element.get("visible", True) is not False


def _words(element: Element) -> str:
    return " ".join(
        _s(element.get(key))
        for key in ("label", "name", "id", "placeholder", "context", "aria_label")
    )


def _is_input(element: Element) -> bool:
    tag = _s(element.get("tag"))
    role = _s(element.get("role"))
    return tag in {"input", "textarea"} or role in {"textbox", "spinbutton"}


def _is_password(element: Element) -> bool:
    role = _s(element.get("role"))
    return (
        (_s(element.get("tag")) == "input" and _s(element.get("type")) == "password")
        or role in {"password", "securetextfield", "axsecuretextfield", "password text"}
        or element.get("protected") is True
    )


def _short_numeric(element: Element) -> bool:
    if _s(element.get("type")) not in _TEXT_INPUT_TYPES:
        return False
    inputmode = _s(element.get("inputmode"))
    pattern = str(element.get("pattern") or "")
    try:
        maxlength = int(element.get("maxlength") or 0)
    except (TypeError, ValueError):
        maxlength = 0
    numeric = (
        inputmode == "numeric"
        or _s(element.get("type")) in {"number", "tel"}
        or bool(re.search(r"\\d|\[0-9\]", pattern))
    )
    short = 3 <= maxlength <= 10
    return numeric or short


# -- detectors -----------------------------------------------------------------


def second_factor(
    elements: Sequence[Element] = (),
    screen_text: str = "",
    *,
    code_words: Iterable[str] = CODE_WORDS,
) -> Optional[str]:
    """``second_factor`` when the screen asks for a one-time code."""

    words = tuple(_s(w) for w in code_words)
    inputs = [e for e in elements if _visible(e) and _is_input(e)]
    for element in inputs:
        if _s(element.get("autocomplete")) == "one-time-code":
            return "second_factor"
        if _short_numeric(element) and any(w in _words(element) for w in words):
            return "second_factor"
    text = _s(screen_text)
    if any(phrase in text for phrase in CODE_PHRASES):
        # The screen says a code is wanted. A visible text input, or no
        # structure to look at, is enough: uncertain means ask.
        if not elements or any(not _is_password(e) for e in inputs):
            return "second_factor"
    return None


def session_expired(
    elements: Sequence[Element] = (),
    screen_text: str = "",
    *,
    save_eligible_seen: bool,
) -> Optional[str]:
    """``session_expired`` when a sign-in appears after a save-eligible screen.

    Before the run reached a screen where it could save, a password field is
    the ordinary start of a task, not an exception.
    """

    if not save_eligible_seen:
        return None
    if any(_visible(e) and _is_password(e) for e in elements):
        return "session_expired"
    text = _s(screen_text)
    if any(phrase in text for phrase in SESSION_PHRASES):
        return "session_expired"
    return None


def _covers(outer: Sequence[float], inner: Sequence[float]) -> bool:
    ox, oy, ow, oh = (float(v) for v in outer)
    ix, iy, iw, ih = (float(v) for v in inner)
    cx, cy = ix + iw / 2, iy + ih / 2
    return ox <= cx <= ox + ow and oy <= cy <= oy + oh


def _bbox(element: Element) -> Optional[Sequence[float]]:
    box = element.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            values = [float(v) for v in box]
        except (TypeError, ValueError):
            return None
        if values[2] > 0 and values[3] > 0:
            return values
    return None


def unexpected_dialog(
    elements: Sequence[Element] = (),
    *,
    target: Optional[Element] = None,
    expected_dialog: bool = False,
) -> Optional[str]:
    """``unexpected_dialog`` when a modal sits on top of the target.

    A modal (``aria-modal``, an open ``<dialog>``, a ``dialog`` or
    ``alertdialog`` role) blocks the page. With geometry for both, a modal
    that doesn't cover the target's centre is not in the way. Without
    geometry the detector can't tell, so it asks. ``expected_dialog`` is the
    caller saying this step opens one (a confirm box it means to answer).
    """

    if expected_dialog:
        return None
    dialogs = [
        e
        for e in elements
        if _visible(e)
        and (
            e.get("modal") is True
            or _s(e.get("role")) in DIALOG_ROLES
            or (_s(e.get("tag")) == "dialog" and e.get("open") is True)
        )
    ]
    if not dialogs:
        return None
    target_box = _bbox(target) if target is not None else None
    target_z = float((target or {}).get("z") or 0)
    for dialog in dialogs:
        if dialog.get("modal") is True:
            return "unexpected_dialog"  # a modal blocks everything under it
        box = _bbox(dialog)
        if box is None or target_box is None:
            return "unexpected_dialog"
        if _covers(box, target_box) and float(dialog.get("z") or 0) >= target_z:
            return "unexpected_dialog"
    return None


def timeout_or_stall(
    *,
    now: float,
    last_action_at: Optional[float],
    last_change_at: Optional[float],
    stall_s: float = DEFAULT_STALL_S,
) -> Optional[str]:
    """``timeout_or_stall`` when neither an action nor a screen change happened
    for ``stall_s`` seconds. A clock with no last event can't show progress."""

    events = [t for t in (last_action_at, last_change_at) if t is not None]
    if not events:
        return "timeout_or_stall"
    if now - max(events) >= stall_s:
        return "timeout_or_stall"
    return None


def _key_fields(duplicate_key: Any) -> Optional[list[str]]:
    if duplicate_key is None:
        return None
    if isinstance(duplicate_key, str):
        return [duplicate_key] if duplicate_key.strip() else []
    if isinstance(duplicate_key, (list, tuple)):
        return [str(name) for name in duplicate_key]
    return []  # a key spec of another shape can't be checked


def duplicate_key_from_config(config: Mapping[str, Any]) -> Any:
    """``record.duplicate_key`` from a loaded verify.yaml, or ``None``."""

    record = config.get("record") if isinstance(config, Mapping) else None
    return record.get("duplicate_key") if isinstance(record, Mapping) else None


def possible_duplicate(
    duplicate_key: Any,
    proposed: Mapping[str, Any],
    existing: Optional[Sequence[Mapping[str, Any]]],
) -> Optional[str]:
    """``possible_duplicate`` when a row already in the app has the same key.

    ``duplicate_key`` is ``record.duplicate_key`` (one field or a list).
    ``existing`` is what the caller already read (``None`` when the read
    failed). This function reads nothing. A key with no fields, a proposed
    record without a key value, a failed read, or a row missing a key field
    can't rule a duplicate out, so it asks.
    """

    fields = _key_fields(duplicate_key)
    if fields is None:
        return None
    if not fields or existing is None:
        return "possible_duplicate"
    wanted = {}
    for name in fields:
        value = _s(proposed.get(name))
        if not value:
            return "possible_duplicate"
        wanted[name] = value
    for row in existing:
        if not isinstance(row, Mapping):
            return "possible_duplicate"
        if any(name not in row for name in fields):
            return "possible_duplicate"
        if all(_s(row.get(name)) == wanted[name] for name in fields):
            return "possible_duplicate"
    return None


# -- all of them -----------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One look at the run: what's on screen, and when things last happened."""

    screen_text: str = ""
    elements: Sequence[Element] = ()
    target: Optional[Element] = None
    save_eligible_seen: bool = False
    expected_dialog: bool = False
    #: Seconds on one clock. ``now`` None means "don't check for a stall".
    now: Optional[float] = None
    last_action_at: Optional[float] = None
    last_change_at: Optional[float] = None
    stall_s: float = DEFAULT_STALL_S
    #: The duplicate check, when the caller has one: key spec, the proposed
    #: record, and the rows it read (None when the read failed).
    duplicate_key: Any = None
    proposed: Mapping[str, Any] = field(default_factory=dict)
    existing: Optional[Sequence[Mapping[str, Any]]] = ()


def detect_all(observation: Observation) -> list[str]:
    """Every kind the observation shows, most urgent first."""

    found: list[str] = []
    o = observation
    for kind in (
        second_factor(o.elements, o.screen_text),
        session_expired(
            o.elements, o.screen_text, save_eligible_seen=o.save_eligible_seen
        ),
        unexpected_dialog(
            o.elements, target=o.target, expected_dialog=o.expected_dialog
        ),
        None
        if o.now is None
        else timeout_or_stall(
            now=o.now,
            last_action_at=o.last_action_at,
            last_change_at=o.last_change_at,
            stall_s=o.stall_s,
        ),
        possible_duplicate(o.duplicate_key, o.proposed, o.existing)
        if o.duplicate_key is not None
        else None,
    ):
        if kind is not None and kind not in found:
            found.append(kind)
    return found


def detect(observation: Observation) -> Optional[str]:
    """The most urgent kind, or ``None`` when nothing calls for a person."""

    found = detect_all(observation)
    return found[0] if found else None


# -- snapshots ---------------------------------------------------------------------


class _Snapshot(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        self._label_for: Optional[str] = None
        self._label_text: list[str] = []
        self._hidden: list[bool] = []
        self._text: list[str] = []

    def _hidden_now(self) -> bool:
        return any(self._hidden)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        style = _s(a.get("style")).replace(" ", "")
        hidden = (
            "hidden" in a
            or a.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        visible = not (hidden or self._hidden_now())
        if tag == "label":
            self._label_for = a.get("for") or ""
            self._label_text = []
        role = a.get("role", "")
        if tag in {"input", "textarea", "select", "button", "dialog"} or role:
            context = " ".join(self._text)[-160:]
            element = {
                "tag": tag,
                "type": a.get("type", "text" if tag == "input" else ""),
                "role": role,
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "placeholder": a.get("placeholder", ""),
                "autocomplete": a.get("autocomplete", ""),
                "inputmode": a.get("inputmode", ""),
                "pattern": a.get("pattern", ""),
                "maxlength": a.get("maxlength", ""),
                "aria_label": a.get("aria-label", ""),
                "context": context,
                "visible": visible and a.get("type") != "hidden",
                "modal": a.get("aria-modal") == "true",
                "open": "open" in a,
            }
            self.elements.append(element)
        if tag not in _VOID:
            self._hidden.append(hidden)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_for is not None:
            if self._label_for:
                self.labels[self._label_for] = " ".join(self._label_text)
            self._label_for = None
        if tag not in _VOID and self._hidden:
            self._hidden.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._label_for is not None:
            self._label_text.append(text)
        if not self._hidden_now():
            self._text.append(text)


def elements_from_html(html: str) -> tuple[list[dict[str, Any]], str]:
    """A DOM snapshot's elements and its visible text, from HTML.

    No geometry: an HTML string has none, so a non-modal dialog found this
    way counts as covering the target (uncertain means ask).
    """

    parser = _Snapshot()
    parser.feed(html)
    parser.close()
    for element in parser.elements:
        element["label"] = parser.labels.get(str(element.get("id") or ""), "")
    return parser.elements, " ".join(parser._text)


def elements_from_accessibility(
    nodes: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Accessibility nodes (``role``, ``name``, optional ``bbox``, ``modal``,
    ``protected``, ``autocomplete``) as detector elements."""

    out = []
    for node in nodes:
        role = _s(node.get("role"))
        out.append(
            {
                "tag": "input"
                if role in {"textbox", "textfield", "axtextfield"}
                else "",
                "type": "password" if node.get("protected") is True else "text",
                "role": "textbox" if role in {"textfield", "axtextfield"} else role,
                "name": node.get("name", ""),
                "label": node.get("name", ""),
                "autocomplete": node.get("autocomplete", ""),
                "maxlength": node.get("maxlength", ""),
                "inputmode": node.get("inputmode", ""),
                "visible": node.get("visible", True),
                "modal": node.get("modal") is True,
                "bbox": node.get("bbox"),
                "z": node.get("z", 0),
                "protected": node.get("protected") is True,
            }
        )
    return out


__all__ = [
    "CODE_WORDS",
    "DEFAULT_STALL_S",
    "Observation",
    "detect",
    "detect_all",
    "duplicate_key_from_config",
    "elements_from_accessibility",
    "elements_from_html",
    "possible_duplicate",
    "second_factor",
    "session_expired",
    "timeout_or_stall",
    "unexpected_dialog",
]
