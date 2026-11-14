"""Structural observations of a web page, for an agent that reads before it looks.

:func:`observe` lists what is on the page as short lines of text: headings,
text, table rows, and every visible control with a ref (``e1``, ``e2``...) the
agent acts on. Text is scrubbed with :func:`openadapt_flow.privacy.scrub_text`
before it leaves the harness. The values the task names (its inputs and the
record identity) are sent as typed placeholders such as ``⟦task.identity_1⟧``,
never as values, so the agent can find the right row without seeing it.

Refs are valid for one observation. Each control keeps the center of its box,
so an action on a ref becomes an ordinary click at a point and is recorded the
same way as any other click. The page itself is never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "Control",
    "StructuralObservation",
    "commit_label",
    "element_at",
    "observe",
    "placeholder_regions",
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
    "(args) => {"
    + _NAME_JS
    + """
  var sel = Array.isArray(args) ? args[0] : args;
  var values = (Array.isArray(args) && args[1]) ? args[1] : [];
  function esc(s) { return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'); }
  var res = values.map(function (v) {
    return new RegExp('(?<![\\\\p{L}\\\\p{N}_])' +
      v.split(/\\s+/).map(esc).join('\\\\s+') + '(?![\\\\p{L}\\\\p{N}_])', 'gu');
  });
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
  var matchedParents = new Set();
  var skipDescendants = new Set();

  if (res.length > 0) {
    for (var j = 0; j < all.length; j++) {
      var el = all[j];
      if (!visible(el)) continue;
      var tag = el.tagName.toLowerCase();
      if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
      if (tag === 'input' || tag === 'textarea' || tag === 'select') continue;
      var text = String(el.innerText || '');
      for (var i = 0; i < res.length; i++) {
        res[i].lastIndex = 0;
        if (!res[i].test(text)) continue;
        var inner = false;
        for (var c = 0; c < el.children.length && !inner; c++) {
          res[i].lastIndex = 0;
          inner = res[i].test(String(el.children[c].innerText || ''));
        }
        if (inner) continue;
        var ancestorMatched = false;
        var p = el.parentElement;
        while (p && p !== document.body) {
          if (matchedParents.has(p)) { ancestorMatched = true; break; }
          p = p.parentElement;
        }
        if (ancestorMatched) continue;

        matchedParents.add(el);
        var desc = el.querySelectorAll('*');
        for (var d = 0; d < desc.length; d++) {
          skipDescendants.add(desc[d]);
        }
        break;
      }
    }
  }

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
    if (skipDescendants.has(el)) continue;

    var text = matchedParents.has(el)
      ? String(el.innerText || '').replace(/\\s+/g, ' ').trim()
      : ownText(el);
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
  var scope = c.closest(scopeSel);
  // A table row with many controls is page layout, not a record row: the
  // scope is the form or section around it.
  while (scope && scope.matches('tr, [role=row]') &&
         scope.querySelectorAll('input, select, textarea, button').length > 8)
    scope = scope.parentElement && scope.parentElement.closest(scopeSel);
  scope = scope || document.body;
  return {tag: c.tagName.toLowerCase(), type: c.type || '', name: oaName(c),
          role: c.getAttribute('role') || '', interactive: interactive,
          editable: c.isContentEditable || c.tagName === 'TEXTAREA' ||
            (c.tagName === 'INPUT' &&
             !/^(button|submit|reset|checkbox|radio|image|file|range|color)$/i
               .test(c.type)),
          // A drop-down whose change submits the form: Enter on it is a save.
          submits: /submit/i.test(c.getAttribute('onchange') || '') ||
            /submit/i.test((c.form && c.form.getAttribute('onchange')) || ''),
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


#: The ref tokens the listing generators emit: ``[e3]`` (:func:`observe`) and
#: ``@3`` (the code-action tool). They are structure, not page text, so they
#: never reach the scrubber.
REF_TOKEN = re.compile(r"\[e\d+\]|(?<![\w@])@\d+(?!\w)")
#: The separator between a table row's cells in a listing.
_CELL = " | "


def scrub_lines(
    text: str,
    keep: Iterable[str] = (),
    bindings: Optional[Mapping[str, str]] = None,
) -> str:
    """Scrub ``text`` line by line, for a listing that goes to the model.

    Only page text is scrubbed: each line is cut at its ref tokens
    (:data:`REF_TOKEN`) and its row-cell separators, and each piece is scrubbed
    on its own, so ``[e3]`` and the listing's syntax come back as they went in.
    A value in ``bindings`` (placeholder to value, see
    :func:`openadapt_flow.privacy.task_bindings`) is replaced by its
    placeholder and never sent. A ``keep`` value is left as it is. When no
    scrubber is installed, or scrubbing is off (``OPENADAPT_FLOW_SCRUB``), the
    text is returned unchanged.
    """

    from openadapt_flow.privacy import text_scrubbing_works

    if not text or not text_scrubbing_works():
        return text
    kept = sorted({str(k) for k in keep if k and str(k).strip()}, key=len, reverse=True)
    from openadapt_flow.privacy import listing_bindings

    bound = sorted(listing_bindings(bindings or {}).items(), key=lambda kv: -len(kv[1]))
    memo: dict[str, str] = {}

    def piece(segment: str) -> str:
        if not segment.strip():
            return segment
        if segment not in memo:
            memo[segment] = _scrub_piece(segment, kept, bound)
        return memo[segment]

    out: list[str] = []
    for line in text.splitlines():
        parts: list[str] = []
        pos = 0
        for match in REF_TOKEN.finditer(line):
            parts.append(_CELL.join(map(piece, line[pos : match.start()].split(_CELL))))
            parts.append(match.group(0))
            pos = match.end()
        parts.append(_CELL.join(map(piece, line[pos:].split(_CELL))))
        out.append("".join(parts))
    return "\n".join(out)


def _scrub_piece(
    segment: str, kept: Sequence[str], bound: Sequence[tuple[str, str]]
) -> str:
    from openadapt_flow.privacy import bound_pattern, scrub_text

    held: dict[str, str] = {}
    for i, (name, value) in enumerate(bound):
        token = f"BOUNDTOKEN{i}X"
        replaced = bound_pattern(value).sub(token, segment)
        if replaced != segment:
            held[token] = name
            segment = replaced
    for i, value in enumerate(kept):
        if value in segment:
            token = f"KEEPTOKEN{i}X"
            held[token] = value
            segment = segment.replace(value, token)
    segment = scrub_text(segment) or ""
    for token, value in held.items():
        segment = segment.replace(token, value)
    return segment


def placeholder_regions(
    page: Any, bindings: Mapping[str, str], width: int, height: int
) -> list[tuple[tuple[int, int, int, int], str]]:
    """Where the page shows a bound value, in screenshot pixels.

    ``width`` and ``height`` are the screenshot's size; the page's viewport is
    scaled to it. Each region is a text range that matches a bound value as
    whole words, or the smallest element whose text or field value does (a
    value split across elements). An unreadable page has no regions.
    """

    from openadapt_flow.privacy import listing_bindings

    bindings = listing_bindings(bindings)
    if not bindings:
        return []
    names = list(bindings)
    try:
        raw = page.evaluate(_REGIONS_JS, [bindings[n] for n in names])
    except Exception:  # noqa: BLE001 - an unreadable page shows nothing to label
        return []
    if not raw or not raw.get("w") or not raw.get("h"):
        return []
    sx = width / float(raw["w"])
    sy = height / float(raw["h"])
    out: list[tuple[tuple[int, int, int, int], str]] = []
    for index, x, y, w, h in raw.get("rects") or []:
        box = (
            int(x * sx) - 1,
            int(y * sy) - 1,
            int(w * sx + 0.999) + 2,
            int(h * sy + 0.999) + 2,
        )
        out.append((box, names[int(index)]))
    return out


_REGIONS_JS = r"""
(values) => {
  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  var res = values.map(function (v) {
    return new RegExp('(?<![\\p{L}\\p{N}_])' +
      v.split(/\s+/).map(esc).join('\\s+') + '(?![\\p{L}\\p{N}_])', 'gu');
  });
  function shown(el) {
    if (!el) return false;
    var s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  }
  var rects = [];
  function add(i, r) {
    if (r.width <= 0 || r.height <= 0) return;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) return;
    rects.push([i, r.left, r.top, r.width, r.height]);
  }
  var covered = values.map(function () { return []; });
  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var node;
  while ((node = walker.nextNode())) {
    if (!shown(node.parentElement)) continue;
    var t = node.textContent;
    for (var i = 0; i < res.length; i++) {
      res[i].lastIndex = 0;
      var m;
      while ((m = res[i].exec(t))) {
        var range = document.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        var list = range.getClientRects();
        for (var k = 0; k < list.length; k++) add(i, list[k]);
        covered[i].push(node.parentElement);
        if (!m[0].length) res[i].lastIndex++;
      }
    }
  }
  var all = document.body.querySelectorAll('*');
  for (var j = 0; j < all.length; j++) {
    var el = all[j];
    if (!shown(el)) continue;
    var tag = el.tagName;
    var field = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    var text = field ? String(el.value || '') : String(el.innerText || '');
    for (var i = 0; i < res.length; i++) {
      res[i].lastIndex = 0;
      if (!res[i].test(text)) continue;
      if (!field) {
        var inner = false;
        for (var c = 0; c < el.children.length && !inner; c++) {
          res[i].lastIndex = 0;
          inner = res[i].test(String(el.children[c].innerText || ''));
        }
        if (inner) continue;
        var done = covered[i].some(function (p) { return el.contains(p); });
        if (done) continue;
      }
      add(i, el.getBoundingClientRect());
    }
  }
  return {w: innerWidth, h: innerHeight, rects: rects};
}
"""


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


def observe(
    page: Any,
    *,
    keep: Sequence[str] = (),
    bindings: Optional[Mapping[str, str]] = None,
) -> StructuralObservation:
    """Read ``page``'s structure: one line per text, heading, row or control."""

    from openadapt_flow.privacy import listing_bindings

    bound_values = list(listing_bindings(bindings or {}).values())
    raw = page.evaluate(_OBSERVE_JS, [INTERACTIVE, bound_values])
    body, controls = _render(raw)
    header = f"page: {raw.get('title') or ''} ({raw.get('url') or ''})"
    text = scrub_lines(header + "\n" + body, keep, bindings)
    return StructuralObservation(
        url=str(raw.get("url") or ""),
        title=str(raw.get("title") or ""),
        text=text,
        controls=controls,
    )


def element_at(
    page: Any, x: int, y: int, *, backend: Any = None
) -> Optional[dict[str, Any]]:
    """The control (or element) at a point: tag, name, editable, scope.

    A point over an iframe is read inside the frame, through the backend's
    hit-tested frame chain, so a Save button in a frame is seen as one. A
    frame the backend can't map is reported as unreadable (None).
    """

    try:
        at = page.evaluate(_ELEMENT_AT_JS, [int(x), int(y), INTERACTIVE, SCOPE])
    except Exception:  # noqa: BLE001 - an unreadable page has no element
        return None
    if not at or str(at.get("tag")) not in ("iframe", "frame"):
        return at
    getter = getattr(backend, "_frame_point", None)
    if not callable(getter):
        return None
    try:
        point = getter(int(x), int(y))
        if point is None or not point.frame_path:
            return None
        return point.scope.evaluate(
            _ELEMENT_AT_JS, [point.x, point.y, INTERACTIVE, SCOPE]
        )
    except Exception:  # noqa: BLE001
        return None


def focused_element(page: Any) -> Optional[dict[str, Any]]:
    """The focused control, inside a frame when focus is in one."""

    try:
        got = page.evaluate(_FOCUSED_JS, [INTERACTIVE, SCOPE])
    except Exception:  # noqa: BLE001
        return None
    if not got or str(got.get("tag")) not in ("iframe", "frame"):
        return got
    # Focus is inside a frame: read the frame whose document holds it.
    main = getattr(page, "main_frame", None)
    for frame in getattr(page, "frames", None) or []:
        if frame is main:
            continue
        try:
            if not frame.evaluate("() => document.hasFocus()"):
                continue
            inner = frame.evaluate(_FOCUSED_JS, [INTERACTIVE, SCOPE])
        except Exception:  # noqa: BLE001 - a detached frame holds no focus
            continue
        if inner and str(inner.get("tag")) not in ("iframe", "frame"):
            return inner
    return None


def page_text(page: Any) -> str:
    """The page's visible text, unscrubbed, for the right-record check only."""

    try:
        return str(page.evaluate("() => document.body.innerText") or "")
    except Exception:  # noqa: BLE001
        return ""
