"""Structural observations of a web page, for an agent that reads before it looks.

:func:`observe` lists what is on the page as short lines of text: headings,
text, table rows, and every visible control with a ref (``e1``, ``e2``...) the
agent acts on. Text is scrubbed with :func:`openadapt_flow.privacy.scrub_text`
before it leaves the harness, except for the values the operator put in the
task (its inputs and the record identity it names), which the agent needs to
find the right row.

Refs are valid for one observation. Each control keeps the center of its box,
so an action on a ref becomes an ordinary click at a point and is recorded the
same way as any other click. The page itself is never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "Control",
    "StructuralObservation",
    "commit_label",
    "element_at",
    "observe",
    "scrub_lines",
    "shows",
]

#: Controls an agent can act on.
INTERACTIVE = (
    "button, a[href], input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=tab], [role=checkbox], [role=radio], "
    "[role=menuitem], [role=option], [contenteditable=''], [contenteditable=true]"
)

#: Labels that commit a write. Narrower than the risk classifier's write list
#: (``openadapt_flow.risk``) on purpose: the gate checks the record identity
#: next to the control, and "Continue" or "Add" on a list page, which opens a
#: form, has none to check. The save on that form is gated.
_COMMIT_WORDS = (
    "save",
    "submit",
    "sign",
    "approve",
    "confirm",
    "send",
    "post",
    "delete",
    "remove",
    "update",
    "apply",
    "done",
    "finish",
    "finaliz",
    "complet",
)

#: The container whose text must show the record's identity when a control in
#: it commits a write: the nearest row, dialog, form or section around it.
SCOPE = (
    "tr, [role=row], [role=listitem], dialog, [role=dialog], form, section, "
    "article, main"
)

_NAME_JS = """
function oaName(el) {
  var n = el.getAttribute('aria-label') ||
    (el.labels && el.labels.length ? el.labels[0].innerText : '') ||
    el.getAttribute('placeholder') || el.innerText ||
    (el.tagName === 'INPUT' && /^(button|submit)$/i.test(el.type) ? el.value : '') ||
    el.getAttribute('title') || '';
  return String(n).replace(/\\s+/g, ' ').trim().slice(0, 80);
}
function oaRow(el) {
  var r = el.closest('tr, [role=row]');
  return r ? String(r.innerText).replace(/\\s+/g, ' ').trim().slice(0, 300) : '';
}
"""

_OBSERVE_JS = (
    "(sel) => {"
    + _NAME_JS
    + """
  function visible(el) {
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth)
      return false;
    var s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  }
  function ownText(el) {
    var t = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      var c = el.childNodes[i];
      if (c.nodeType === 3) t += c.textContent;
    }
    return t.replace(/\\s+/g, ' ').trim();
  }
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr'));
  var out = [];
  var all = document.body.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    var tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
    var tr = el.closest('tr');
    var row = tr ? rows.indexOf(tr) : -1;
    if (el.matches(sel)) {
      if (!visible(el)) continue;
      var r = el.getBoundingClientRect();
      var value = null;
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        value = el.type === 'password' ? '' : String(el.value || '');
      }
      var cls = (el.className && el.className.baseVal === undefined)
        ? String(el.className) : '';
      out.push({
        kind: 'control', tag: tag, role: el.getAttribute('role') || '',
        type: el.type || '', name: oaName(el), value: value,
        focused: document.activeElement === el,
        selected: /(^|\\s)(selected|active)(\\s|$)/.test(cls) ||
          el.getAttribute('aria-selected') === 'true' ||
          el.getAttribute('aria-pressed') === 'true' || !!el.checked,
        disabled: !!el.disabled,
        x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
        row: row, context: oaRow(el)
      });
      continue;
    }
    if (el.closest(sel)) continue;
    var text = ownText(el);
    if (!text || !visible(el)) continue;
    out.push({kind: /^h[1-6]$/.test(tag) ? 'heading' : 'text', tag: tag,
              text: text.slice(0, 200), row: row});
  }
  return {url: location.href, title: document.title, items: out};
}
"""
)

_DESCRIBE_JS = """
function oaDescribe(c, interactive, scopeSel) {
  var scope = c.closest(scopeSel) || document.body;
  return {tag: c.tagName.toLowerCase(), type: c.type || '', name: oaName(c),
          role: c.getAttribute('role') || '', interactive: interactive,
          editable: c.isContentEditable || c.tagName === 'TEXTAREA' ||
            (c.tagName === 'INPUT' &&
             !/^(button|submit|reset|checkbox|radio|image|file|range|color)$/i
               .test(c.type)),
          context: oaRow(c), scope: String(scope.innerText || '')};
}
"""

_ELEMENT_AT_JS = (
    "([x, y, sel, scopeSel]) => {"
    + _NAME_JS
    + _DESCRIBE_JS
    + """
  var el = document.elementFromPoint(x, y);
  if (!el) return null;
  var c = el.closest(sel);
  return oaDescribe(c || el, !!c, scopeSel);
}
"""
)

_FOCUSED_JS = (
    "([sel, scopeSel]) => {"
    + _NAME_JS
    + _DESCRIBE_JS
    + """
  var c = document.activeElement;
  if (!c || c === document.body) return null;
  return oaDescribe(c, c.matches(sel), scopeSel);
}
"""
)


@dataclass(frozen=True)
class Control:
    """One control in an observation."""

    ref: str
    tag: str
    name: str
    x: int
    y: int
    role: str = ""
    type: str = ""
    #: The text of the table row the control is in, when it is in one.
    context: str = ""

    @property
    def kind(self) -> str:
        if self.role:
            return self.role
        if self.tag == "input":
            return f"input[{self.type or 'text'}]"
        return {"a": "link"}.get(self.tag, self.tag)


@dataclass
class StructuralObservation:
    """What the agent reads: ``text``, and the controls its refs name."""

    url: str
    title: str
    text: str
    controls: dict[str, Control] = field(default_factory=dict)

    def find(self, name: str, *, near: Optional[str] = None) -> Optional[Control]:
        """The control named ``name`` (case-insensitive), on the line that
        also shows ``near`` when given. None when none or several match."""

        wanted = name.strip().lower()
        hits: list[Control] = []
        for line in self.text.splitlines():
            if near is not None and near.lower() not in line.lower():
                continue
            for ref, control in self.controls.items():
                if f"[{ref}]" in line and control.name.lower() == wanted:
                    hits.append(control)
        unique = {c.ref: c for c in hits}
        return next(iter(unique.values())) if len(unique) == 1 else None


def commit_label(name: str) -> bool:
    """True when a control labelled ``name`` commits a write (Save, Submit)."""

    words = "".join(ch if ch.isalnum() else " " for ch in name.lower()).split()
    if "sign" in words and ("in" in words or "up" in words):
        return False
    return any(word.startswith(stem) for word in words for stem in _COMMIT_WORDS)


def shows(text: str, identity: str) -> bool:
    """True when ``identity`` appears in ``text`` as whole words: "MRN P1"
    is not shown by "MRN P12"."""

    pattern = r"(?<!\w)" + re.escape(identity.strip()) + r"(?!\w)"
    return re.search(pattern, " ".join(text.split())) is not None


def scrub_lines(text: str, keep: Iterable[str] = ()) -> str:
    """Scrub ``text`` line by line, leaving every ``keep`` value as it is.

    Each kept value is swapped for a placeholder the scrubber leaves alone,
    and swapped back afterwards. When no scrubber is installed, or scrubbing
    is off (``OPENADAPT_FLOW_SCRUB``), the text is returned unchanged.
    """

    from openadapt_flow.privacy import scrub_text, text_scrubbing_enabled

    if not text or not text_scrubbing_enabled():
        return text
    kept = sorted({str(k) for k in keep if k and str(k).strip()}, key=len, reverse=True)
    out: list[str] = []
    for line in text.splitlines():
        held: dict[str, str] = {}
        for i, value in enumerate(kept):
            if value in line:
                token = f"KEEPTOKEN{i}X"
                held[token] = value
                line = line.replace(value, token)
        line = scrub_text(line) or ""
        for token, value in held.items():
            line = line.replace(token, value)
        out.append(line)
    return "\n".join(out)


def _render(raw: dict[str, Any]) -> tuple[str, dict[str, Control]]:
    controls: dict[str, Control] = {}
    lines: list[str] = []
    current_row: Optional[int] = None
    row_parts: list[str] = []

    def flush() -> None:
        nonlocal row_parts
        if row_parts:
            lines.append("row: " + " | ".join(row_parts))
        row_parts = []

    for item in raw.get("items") or []:
        if item["kind"] == "control":
            ref = f"e{len(controls) + 1}"
            control = Control(
                ref=ref,
                tag=str(item["tag"]),
                name=str(item["name"]),
                x=int(item["x"]),
                y=int(item["y"]),
                role=str(item.get("role") or ""),
                type=str(item.get("type") or ""),
                context=str(item.get("context") or ""),
            )
            controls[ref] = control
            part = f'[{ref}] {control.kind} "{control.name}"'
            if item.get("value") is not None:
                part += f' value="{item["value"]}"'
            flags = [
                flag for flag in ("focused", "selected", "disabled") if item.get(flag)
            ]
            if flags:
                part += " (" + ", ".join(flags) + ")"
        else:
            part = item["text"] if item["kind"] == "text" else f"# {item['text']}"
        row = int(item.get("row", -1))
        if row >= 0:
            if row != current_row:
                flush()
                current_row = row
            row_parts.append(part)
            continue
        flush()
        current_row = None
        lines.append(part)
    flush()
    return "\n".join(lines), controls


def observe(page: Any, *, keep: Sequence[str] = ()) -> StructuralObservation:
    """Read ``page``'s structure: one line per text, heading, row or control."""

    raw = page.evaluate(_OBSERVE_JS, INTERACTIVE)
    body, controls = _render(raw)
    header = f"page: {raw.get('title') or ''} ({raw.get('url') or ''})"
    text = scrub_lines(header + "\n" + body, keep)
    return StructuralObservation(
        url=str(raw.get("url") or ""),
        title=str(raw.get("title") or ""),
        text=text,
        controls=controls,
    )


def element_at(page: Any, x: int, y: int) -> Optional[dict[str, Any]]:
    """The control (or element) at a point: tag, type, name, in_form."""

    try:
        return page.evaluate(_ELEMENT_AT_JS, [int(x), int(y), INTERACTIVE, SCOPE])
    except Exception:  # noqa: BLE001 - an unreadable page has no element
        return None


def focused_element(page: Any) -> Optional[dict[str, Any]]:
    try:
        return page.evaluate(_FOCUSED_JS, [INTERACTIVE, SCOPE])
    except Exception:  # noqa: BLE001
        return None


def page_text(page: Any) -> str:
    """The page's visible text, unscrubbed, for the right-record check only."""

    try:
        return str(page.evaluate("() => document.body.innerText") or "")
    except Exception:  # noqa: BLE001
        return ""
