"""Turn shadow events into value-free symbols.

A symbol says *what kind of thing* happened, in which application, on which
kind of control. It never says what was typed. Typed text becomes a keyed
digest (or nothing, for a secure field), labels on data controls become keyed
digests, URLs become a coarse pattern, and window titles are never read.

The digest key comes from :mod:`openadapt_capture.shadow.keys` and lives for
one 24-hour epoch at most; then it is destroyed with the buffer. Without the
key, a stored digest can't be checked against a guessed value, which matters
for short values such as a member ID, a record number, or an SSN.

URLs and short chrome labels are scrubbed here, before a symbol exists: see
:mod:`openadapt_capture.shadow.scrub`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from openadapt_capture.shadow.config import ShadowConfig
from openadapt_capture.shadow.events import ShadowEvent
from openadapt_capture.shadow.scrub import (
    NON_WORK_BROWSER,
    allowlisted_domain,
    looks_secret,
    scrub_host,
    scrub_label,
    scrub_path_segment,
)

SYMBOL_SCHEMA = "openadapt.shadow.symbol/v1"
_DIGEST_HEX = 16
_TYPE_MERGE_GAP_S = 2.0

_ROLE_ALIASES = {
    "pushbutton": "button",
    "push button": "button",
    "togglebutton": "button",
    "menu item": "menuitem",
    "menubar item": "menubaritem",
    "tabitem": "tab",
    "tab item": "tab",
    "page tab": "tab",
    "check box": "checkbox",
    "radio button": "radiobutton",
    "edit": "textfield",
    "textbox": "textfield",
    "text field": "textfield",
    "entry": "textfield",
    "text": "statictext",
    "static text": "statictext",
    "label": "statictext",
    "text area": "textarea",
    "document": "textarea",
    "combo box": "combobox",
    "search field": "searchfield",
    "securetextfield": "textfield",
    "password text": "textfield",
    "passwordbox": "textfield",
    "list item": "listitem",
    "table cell": "cell",
    "dataitem": "cell",
    "data item": "cell",
    "gridcell": "cell",
    "table row": "row",
    "hyperlink": "link",
}

#: Roles that take typed text. A field among them whose label names a
#: password, PIN, or similar is treated as secure even when the platform
#: didn't say so.
_TEXT_ENTRY_ROLES = frozenset({"textfield", "textarea", "combobox", "searchfield", "unknown"})
_DATE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DECIMAL = re.compile(r"^[-+]?\d+[.,]\d+$")
_NAMED_KEYS = frozenset(
    {
        "enter",
        "return",
        "tab",
        "escape",
        "esc",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
        "pageup",
        "pagedown",
        "space",
    }
    | {f"f{i}" for i in range(1, 25)}
)
#: Modifiers that make a key press a command rather than typing. Shift and
#: Alt (Option, AltGr) alone produce characters, so a chord of only those is
#: typing and its character must not be kept.
_COMMAND_MODIFIERS = frozenset({"ctrl", "control", "cmd", "command", "meta", "super", "win"})
_SHORTCUT_CHAR = re.compile(r"^[a-z0-9]$")


class ShadowTokenizeError(RuntimeError):
    """Tokenizing failed. Carries no event content, and no chained exception,
    so a log or crash report of it can't show a typed value or a label."""


def shortcut_key(key: str | None, modifiers: tuple[str, ...], *, secure: bool) -> str:
    """The key part of a chord that is safe to keep in plain text.

    A named key (Tab, F5) or one ASCII letter or digit is kept, and only for a
    chord with Ctrl, Cmd, or Meta held. Anything else, and anything in a
    secure field, becomes ``other``: Option and AltGr chords type characters
    (``@``, ``é``), and a character in a password box is part of the password.
    """

    name = (key or "").casefold()
    if secure:
        return "other"
    if name in _NAMED_KEYS:
        return name
    mods = {m.casefold() for m in modifiers}
    if mods & _COMMAND_MODIFIERS and _SHORTCUT_CHAR.match(name):
        return name
    return "other"


def normalize_role(role: str | None) -> str:
    """Return a lowercase, platform-neutral control role, or ``"unknown"``."""

    if not role:
        return "unknown"
    text = " ".join(str(role).replace("_", " ").split()).casefold()
    if text.startswith("ax") and len(text) > 2 and " " not in text:
        text = text[2:]
    if text.startswith("role "):
        text = text[5:]
    text = _ROLE_ALIASES.get(text, text)
    return _ROLE_ALIASES.get(text.replace(" ", ""), text.replace(" ", ""))


def url_pattern(
    url: str | None,
    work_domains: frozenset[str] = frozenset(),
    extra_words: frozenset[str] = frozenset(),
) -> str | None:
    """Reduce a URL to ``scheme://host/first-segment/*``, or a non-work marker.

    Only a host covered by ``work_domains`` is kept; any other URL becomes
    ``non-work-browser``, with no host or path. For a work host, the query
    string, fragment, credentials, and port are dropped, subdomain labels and
    the first path segment are scrubbed (see
    :func:`~openadapt_capture.shadow.scrub.scrub_path_segment`), and every
    later segment becomes ``*``. So ``/patient-987654`` becomes ``/<id>``.
    """

    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
    except ValueError:
        return NON_WORK_BROWSER
    scheme = (parts.scheme or "").casefold()
    if scheme == "file":
        return "file:*"
    domain = allowlisted_domain(host, work_domains)
    if domain is None or scheme not in ("http", "https"):
        return NON_WORK_BROWSER
    segments = [s for s in parts.path.split("/") if s]
    shape = []
    for index, segment in enumerate(segments):
        if index == 0:
            shape.append(scrub_path_segment(segment, extra_words))
        else:
            shape.append("*")
    path = "/" + "/".join(shape) if shape else ""
    return f"{scheme}://{scrub_host(host, domain, extra_words)}{path}"


def value_shape(text: str) -> str:
    """Coarse shape of a typed value (never its length or content)."""

    value = text.strip()
    if not value:
        return "empty"
    if value.isdigit():
        return "digits"
    if _DECIMAL.match(value):
        return "decimal"
    if _DATE.match(value):
        return "date"
    if _EMAIL.match(value):
        return "email"
    if value.isalpha():
        return "alpha"
    if value.replace(" ", "").isalnum():
        return "alnum"
    return "text"


@dataclass(frozen=True)
class ShadowSymbol:
    """One value-free symbol. This is the only thing the ring buffer stores.

    Attributes:
        t: Unix time in seconds.
        token: The structural symbol the miner compares.
        kind: The event kind.
        app: Application name.
        window_class: Window class, when the platform reports one.
        role: Normalized control role.
        label_digest: Keyed digest of the control label.
        display_label: Plain-text label, kept only for short chrome labels.
        value_digest: Keyed digest of the per-run value (typed text, or the
            label of a data control such as a list row).
        value_shape: Coarse value shape, for example ``digits`` or ``date``.
        url_pattern: Coarse URL pattern for browser events.
        key: Named key, for ``key`` and ``shortcut`` symbols.
        modifiers: Sorted modifier keys.
        secure: True when the target was a password or secure field.
    """

    t: float
    token: str
    kind: str
    app: str | None = None
    window_class: str | None = None
    role: str | None = None
    label_digest: str | None = None
    display_label: str | None = None
    value_digest: str | None = None
    value_shape: str | None = None
    url_pattern: str | None = None
    key: str | None = None
    modifiers: tuple[str, ...] = field(default_factory=tuple)
    secure: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["modifiers"] = list(self.modifiers)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShadowSymbol:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        known["modifiers"] = tuple(known.get("modifiers") or ())
        return cls(**known)


class Tokenizer:
    """Convert :class:`ShadowEvent` objects into :class:`ShadowSymbol` objects.

    Consecutive ``type`` events into the same control are merged into one
    symbol, so a value typed in several bursts gets one digest. The merged
    text is held in memory only until the next event or :meth:`flush`.
    """

    def __init__(self, config: ShadowConfig | None = None, *, digest_key: bytes | None = None):
        self._config = config or ShadowConfig()
        if digest_key is not None and len(digest_key) < 16:
            raise ValueError("digest_key must be at least 16 bytes")
        self._key = digest_key if digest_key is not None else secrets.token_bytes(32)
        self._pending: tuple[ShadowEvent, list[str], list[float]] | None = None

    def digest(self, domain: str, value: str) -> str:
        """Return the keyed digest of ``value`` in ``domain``."""

        mac = hmac.new(self._key, f"{domain}\x00{value}".encode(), hashlib.sha256)
        return mac.hexdigest()[:_DIGEST_HEX]

    def feed(self, event: ShadowEvent) -> list[ShadowSymbol]:
        """Tokenize one event. Returns zero, one, or two symbols."""

        try:
            return self._feed(event)
        except Exception as exc:
            self._pending = None
            name = type(exc).__name__
        # Raised outside the handler so nothing is chained: the original
        # exception and its frames may hold typed text.
        raise ShadowTokenizeError(f"could not tokenize a {event.kind} event ({name})")

    def flush(self) -> list[ShadowSymbol]:
        """Emit a pending merged ``type`` symbol, if any, and forget its text."""

        try:
            return self._flush()
        except Exception as exc:
            self._pending = None
            name = type(exc).__name__
        raise ShadowTokenizeError(f"could not tokenize typed text ({name})")

    def _feed(self, event: ShadowEvent) -> list[ShadowSymbol]:
        out: list[ShadowSymbol] = []
        if event.kind == "type":
            # Secret typing is merged too, with its text dropped at once, so
            # the buffer can't count a password's characters.
            if self._is_secure(event):
                event = replace(event, text=None, secure=True)
            chunk = "" if event.secure else (event.text or "")
            if self._pending is not None and self._same_field(self._pending, event):
                self._pending[1].append(chunk)
                self._pending[2][0] = event.timestamp
                return out
            out.extend(self._flush())
            self._pending = (event, [chunk], [event.timestamp])
            return out
        out.extend(self._flush())
        symbol = self._symbol(event, None)
        if symbol is not None:
            out.append(symbol)
        return out

    def _flush(self) -> list[ShadowSymbol]:
        if self._pending is None:
            return []
        event, chunks, _last = self._pending
        self._pending = None
        text = "".join(chunks)
        chunks.clear()
        symbol = self._symbol(event, text)
        return [symbol] if symbol is not None else []

    @staticmethod
    def _is_secure(event: ShadowEvent) -> bool:
        return bool(event.secure) or (
            normalize_role(event.role) in _TEXT_ENTRY_ROLES and looks_secret(event.label)
        )

    @staticmethod
    def _same_field(
        pending: tuple[ShadowEvent, list[str], list[float]], second: ShadowEvent
    ) -> bool:
        first, _chunks, last = pending
        return (
            first.app == second.app
            and first.secure == second.secure
            and first.role == second.role
            and first.label == second.label
            and 0.0 <= second.timestamp - last[0] <= _TYPE_MERGE_GAP_S
        )

    def _label_parts(self, role: str, label: str | None) -> tuple[str | None, str | None, str]:
        """Return (label_digest, display_label, token label part)."""

        if not label:
            return None, None, "-"
        cfg = self._config
        digest = self.digest("label", label)
        display = None
        if cfg.keep_chrome_labels and role in cfg.chrome_roles:
            display = scrub_label(label, cfg.extra_label_words)
        if role in cfg.structural_label_roles:
            return digest, display, digest[:8]
        return digest, display, "-"

    def _symbol(self, event: ShadowEvent, typed: str | None) -> ShadowSymbol | None:
        app = (event.app or "unknown").strip() or "unknown"
        role = normalize_role(event.role)
        label_digest, display, label_part = self._label_parts(role, event.label)
        pattern = url_pattern(event.url, self._config.work_domains, self._config.extra_path_words)
        secure = bool(event.secure) or (role in _TEXT_ENTRY_ROLES and looks_secret(event.label))
        common: dict[str, Any] = {
            "t": float(event.timestamp),
            "kind": event.kind,
            "app": app,
            "window_class": event.window_class,
            "url_pattern": pattern,
            "secure": secure,
        }
        kind = event.kind
        if kind == "focus":
            return ShadowSymbol(token=f"focus|{app}", **common)
        if kind in ("click", "double_click", "right_click"):
            value_digest = None
            if label_digest is not None and role not in self._config.structural_label_roles:
                # A data control's label is the per-run value (which row was
                # clicked). Keep one digest of it, as the value, and no label.
                value_digest = self.digest("value", event.label or "")
                label_digest = None
            return ShadowSymbol(
                token=f"{kind}|{app}|{role}|{label_part}",
                role=role,
                label_digest=label_digest,
                display_label=display,
                value_digest=value_digest,
                **common,
            )
        if kind == "type":
            if secure:
                return ShadowSymbol(
                    token=f"type|{app}|{role}|{label_part}|secure",
                    role=role,
                    label_digest=label_digest,
                    **common,
                )
            text = typed if typed is not None else ""
            return ShadowSymbol(
                token=f"type|{app}|{role}|{label_part}",
                role=role,
                label_digest=label_digest,
                value_digest=self.digest("value", text),
                value_shape=value_shape(text),
                **common,
            )
        if kind == "key":
            name = (event.key or "").casefold()
            if name not in _NAMED_KEYS:
                name = "other"
            return ShadowSymbol(token=f"key|{app}|{name}", key=name, **common)
        if kind in ("shortcut", "copy", "paste"):
            mods = tuple(sorted({m.casefold() for m in event.modifiers}))
            if kind == "shortcut":
                key = shortcut_key(event.key, mods, secure=secure)
            else:
                key = (event.key or "").casefold()
                key = key if key in ("c", "x", "v") else "other"
            if kind == "shortcut":
                token = f"shortcut|{app}|{'+'.join(mods)}+{key}"
            else:
                token = f"{kind}|{app}"
            return ShadowSymbol(token=token, key=key, modifiers=mods, **common)
        if kind in ("navigate", "submit"):
            return ShadowSymbol(token=f"{kind}|{app}|{pattern or '-'}", **common)
        return None
