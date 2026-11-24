"""What the agent's form shows at the moment its save is held.

With no write adapter the guard can't read the values a save sends. A
careful person then checks the screen: the filled form, and each field's
value on it. :data:`READ_CONTROLS_JS` reads the page's form controls the way
the readiness check reads them (``agent_guard/cdp.py`` on the shadow-hold
branch, ``_READ_FORM_VALUES``): ``input``, ``select``, ``textarea`` and
``contenteditable`` values in document order, leaving out password, hidden,
file and button inputs. It also keeps each control's label (its ``<label>``,
``aria-label``, ``aria-labelledby`` or placeholder), id and name, so a value
can be matched to a task field by name, and whether the page renders it: a
control inside ``hidden``, ``aria-hidden`` or ``inert``, failing
``checkVisibility()``, with ``display: none``, ``visibility`` other than
visible, zero opacity, or an empty box is not on screen. A control scrolled
out of the viewport still is. Nothing here names an app.

:func:`match_fields` gives a task field a screen value only when every
control whose id, name or label is that field's name (or the record field
the effect contract binds it to) shows the same value, counting only
rendered controls. No match, a match only by hidden controls, or two
controls that disagree, leaves the field unread on screen.

Raw values: kept in memory and in 0600 files only, like the card's others.
"""

from __future__ import annotations

import re
import struct
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "READ_CONTROLS_JS",
    "controls_from",
    "match_fields",
    "readable_png",
]

_MAX_VALUE = 512

#: ``[{key, id, name, label, value}]`` for each form control, or the page's
#: own notice left out (``%(notice)s`` is filled in by ``agent_guard.cdp``).
READ_CONTROLS_JS = """
() => {
  const out = [];
  const seen = {};
  const skip = new Set(['password', 'hidden', 'file', 'submit', 'button', 'reset', 'image']);
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
  const labelOf = (el) => {
    if (el.labels && el.labels.length) { return clean(el.labels[0].innerText); }
    const aria = el.getAttribute('aria-label');
    if (aria) { return clean(aria); }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\\s+/).map(id => document.getElementById(id))
        .filter(Boolean).map(n => n.innerText);
      if (parts.length) { return clean(parts.join(' ')); }
    }
    return clean(el.getAttribute('placeholder'));
  };
  const shown = (el) => {
    if (el.closest('[hidden], [aria-hidden="true"], [inert]')) { return false; }
    if (typeof el.checkVisibility === 'function'
        && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
      return false;
    }
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility !== 'visible' || Number(st.opacity) === 0) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  for (const el of document.querySelectorAll('input, select, textarea, [contenteditable]')) {
    if (el.closest('#' + %(notice)s)) { continue; }
    const tag = el.tagName.toLowerCase();
    const base = tag + ':' + (el.id || el.getAttribute('name') || '');
    seen[base] = (seen[base] || 0) + 1;
    let value = null;
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (skip.has(type)) { continue; }
      if (type === 'checkbox' || type === 'radio') {
        if (!el.checked) { continue; }
        value = el.value;
      } else {
        value = el.value || '';
      }
    } else if (tag === 'select') {
      value = Array.from(el.selectedOptions || []).map(o => o.text || o.value).join(', ');
    } else if (tag === 'textarea') {
      value = el.value || '';
    } else if (el.isContentEditable) {
      value = el.innerText || '';
    } else {
      continue;
    }
    out.push({key: base + ':' + seen[base], id: el.id || '',
      name: el.getAttribute('name') || '', label: labelOf(el), value: value,
      visible: shown(el)});
  }
  return out;
}
"""


def controls_from(raw: Any) -> Optional[list[dict[str, Any]]]:
    """The controls :data:`READ_CONTROLS_JS` returned, or None if malformed."""

    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        control: dict[str, Any] = {
            k: ("" if item.get(k) is None else str(item.get(k)))
            for k in ("key", "id", "name", "label", "value")
        }
        # Only a control the page renders is "on screen"; unknown is hidden.
        control["visible"] = item.get("visible") is True
        out.append(control)
    return out


def _norm(text: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def match_fields(
    controls: Optional[Sequence[Mapping[str, Any]]],
    fields: Sequence[str],
    bound: Mapping[str, str] = {},  # noqa: B006 - read only
) -> dict[str, str]:
    """Task field -> its value on screen, for the fields that match exactly
    one value. ``bound`` maps a task parameter to its record field."""

    if not controls:
        return {}
    out: dict[str, str] = {}
    for name in fields:
        wanted = {n for n in (_norm(name), _norm(bound.get(name))) if n}
        values = {
            str(c.get("value") or "")[:_MAX_VALUE]
            for c in controls
            # A control the page doesn't render is not what a person saw.
            if c.get("visible") is True
            and wanted
            & {_norm(c.get("id")), _norm(c.get("name")), _norm(c.get("label"))}
        }
        if len(values) == 1:
            out[name] = values.pop()
    return out


def readable_png(data: Optional[bytes]) -> bool:
    """A PNG with a header and a non-empty picture: something a person can see."""

    if not data or len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    if data[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", data[16:24])
    return width > 0 and height > 0
