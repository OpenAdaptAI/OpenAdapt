"""Feed a running recorder's live events into a :class:`ShadowObserver`.

:class:`RecorderShadowBridge` is a subscriber for
:class:`openadapt_capture.live.LiveEventTap`::

    observer = ShadowObserver(config, acknowledgement=ack)
    tap = LiveEventTap(RecorderShadowBridge(observer)).start()
    Recorder(capture_dir, live_event_tap=tap)

It turns the recorder's raw events into :class:`ShadowEvent` objects:

* a mouse press becomes a click on the control the accessibility observer
  found under the pointer;
* a printable key press becomes one typed character for that control (the
  tokenizer merges the characters of one field into one digest);
* a key pressed with Ctrl, Cmd, or Meta held becomes a shortcut, or a copy or
  paste. Option and AltGr alone type characters, so a key pressed with only
  Alt (and Shift) held is typing, like any other character;
* a named key (Enter, Tab, an arrow) becomes a key event;
* a window event becomes a focus event on its application (never its title);
* a browser event maps as :func:`~openadapt_capture.shadow.events.from_browser_event`.

Typing with no accessibility evidence for the target (no observation, or one
with no element role) is treated as typing into a secure field: its value is
dropped, not digested, because nothing shows it isn't a password box.

The bridge holds the current modifier state and nothing else. A modifier
counts as held for :data:`MODIFIER_HOLD_S` seconds after its press at most,
and every window change clears them, so a missed release can't turn later
typing into a run of shortcuts. It never writes, and it never sees a screen
frame.
"""

from __future__ import annotations

import math
from typing import Any

from openadapt_capture.shadow.events import (
    ShadowEvent,
    _chord_kind,
    _structural_context,
    from_browser_event,
    from_window_event,
)

_MODIFIERS = {
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "option": "alt",
    "cmd": "cmd",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "command": "cmd",
    "meta": "meta",
    "super": "meta",
    "win": "meta",
}
_CHORD_MODIFIERS = frozenset({"ctrl", "cmd", "meta"})
#: How long a pressed modifier counts as held without a release.
MODIFIER_HOLD_S = 5.0
_NAMED = {"enter", "return", "tab", "escape", "esc", "backspace", "delete", "space"}


def _key_name(data: dict[str, Any]) -> str:
    name = data.get("canonical_key_name") or data.get("key_name") or ""
    name = str(name).casefold()
    return name[4:] if name.startswith("key.") else name


def _key_char(data: dict[str, Any]) -> str | None:
    char = data.get("canonical_key_char") or data.get("key_char")
    return char if isinstance(char, str) and len(char) == 1 and char.isprintable() else None


def _has_target(data: dict[str, Any]) -> bool:
    """True when the accessibility observer named the control being typed into."""

    obs = data.get("structural_observation")
    element = obs.get("element") if isinstance(obs, dict) else getattr(obs, "element", None)
    if isinstance(element, dict):
        role = element.get("role") or element.get("control_type")
    else:
        role = getattr(element, "role", None) or getattr(element, "control_type", None)
    return bool(role)


class RecorderShadowBridge:
    """Subscriber that maps live recorder events onto a shadow observer."""

    def __init__(self, observer: Any) -> None:
        self._observer = observer
        self._held: dict[str, float] = {}

    def __call__(self, event: Any) -> None:
        for shadow in self.translate(event):
            self._observer.observe(shadow)

    def translate(self, event: Any) -> list[ShadowEvent]:
        """Map one :class:`~openadapt_capture.live.LiveEvent`."""

        data = event.data if isinstance(event.data, dict) else {}
        ts = float(event.timestamp)
        if event.type == "window":
            self._held.clear()
            mapped = from_window_event({"timestamp": ts, "state": data.get("state") or data})
            return [mapped] if mapped is not None else []
        if event.type == "browser":
            mapped = from_browser_event({**data, "timestamp": ts})
            return [mapped] if mapped is not None else []
        if event.type != "action":
            return []
        name = data.get("name")
        if name == "click":
            if not data.get("mouse_pressed"):
                return []
            ctx = _structural_context(data)
            button = str(data.get("mouse_button_name") or "left").casefold()
            kind = "right_click" if button.endswith("right") else "click"
            return [ShadowEvent(ts, kind, **ctx)]
        if name in ("press", "release"):
            return self._key(ts, name == "press", data)
        return []

    def _key(self, ts: float, pressed: bool, data: dict[str, Any]) -> list[ShadowEvent]:
        key = _key_name(data)
        modifier = _MODIFIERS.get(key)
        if modifier is not None:
            if pressed:
                self._held[modifier] = ts
            else:
                self._held.pop(modifier, None)
            return []
        if not pressed:
            return []
        held = {
            m
            for m, since in self._held.items()
            if math.isfinite(ts) and 0 <= ts - since <= MODIFIER_HOLD_S
        }
        ctx = _structural_context(data)
        char = _key_char(data)
        chord = held & _CHORD_MODIFIERS
        if chord:
            mods = tuple(sorted(chord | (held & {"shift", "alt"})))
            target = (char or key).casefold()
            return [ShadowEvent(ts, _chord_kind(mods, target), key=target, modifiers=mods, **ctx)]
        if char is not None and key not in _NAMED:
            if not _has_target(data):
                ctx["secure"] = True
            return [ShadowEvent(ts, "type", text=char, **ctx)]
        return [ShadowEvent(ts, "key", key=key or None, **ctx)]
