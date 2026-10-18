"""Code as action: the run-1 agent works through one terminal-style tool.

The agent gets a single tool, ``run``, that takes a short script: one command
per line. It reads the app's structure (a web page's DOM, a desktop app's
accessibility tree, or OCR text on a pixel-only screen), acts on elements by
role and name, asks for a screenshot only when it wants one, and keeps notes
in a working directory with a read-only shell. One model call can read, act
and check several times, so a task takes fewer calls and far fewer tokens than
a screenshot after every click.

This follows the 2026 harness results: CUAWright (a terminal as the only
action interface, state kept on disk, vision on demand), OpenGCUA (GUI,
accessibility and shell actions composed in one program) and ANIS (commands
with observable effects, read back after a write). Unlike those harnesses the
script is not general code: every line is either a read or one recordable
action, so each action goes through the save gate and is recorded as a step.

Commands (see :data:`COMMANDS`)::

    tree [grep=TEXT] [max=N]        the structure, one line per row or control
    find [role=R] [name=TEXT] [near=TEXT]   controls that match, with @refs
    text [grep=TEXT]                the visible text
    read TARGET                     an element's value and row
    click TARGET
    fill TARGET value=TEXT          types into an empty field
    select TARGET option=TEXT       picks an option
    press KEY                       Enter, Tab, Escape, ...
    scroll down|up [px=N]           at the pointer, recorded like any action
    screenshot                      an image of the screen, only when asked
    sh COMMAND                      read-only shell in the working directory
    save FILE                       the previous command's output, to FILE
    wait ms=N

A TARGET is a ref from the latest listing (``@3``) or a description
(``role=button name="Open" near="Jane Sample"``; on a pixel-only screen,
``text="Save"``). A description must match exactly one element.

Every action becomes a structured step in :class:`ActionTrace`: the element's
identity (role, name, the row it is in), the action and the value, the
recorder's event index, and the structural locator the recorder captured for
it. :func:`check_trace_against_ir` then checks that the compiled program
carries that locator on its step. The trace's reads are evidence for the
record check: which of the task's identity strings and values the agent read,
and whether it read its inputs back after the save.

Safety is the harness's: the right-record check and the hold run before every
action that may commit a write (:class:`~.harness.Harness`), the shell can't
reach a network or write outside its directory (:mod:`.sandbox`), and a web
action that leaves the app's origin ends the run.
"""

from __future__ import annotations

import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from openadapt_flow.compile_agent import observe as _observe
from openadapt_flow.compile_agent.harness import (
    CommitHint,
    Harness,
)
from openadapt_flow.compile_agent.sandbox import Sandbox, ShellRefused
from openadapt_flow.jit.adapters import UnsupportedAction

__all__ = [
    "COMMANDS",
    "ActionTrace",
    "CodeActionTool",
    "Element",
    "LeftTheApp",
    "ScriptError",
    "ScriptedCodeAgent",
    "ToolOutput",
    "check_trace_against_ir",
]

COMMANDS = """\
One command per line; lines run in order and stop at the first error.
  tree [grep=TEXT] [max=N]   the structure: headings, text, rows, controls with @refs
  find [role=R] [name=TEXT] [near=TEXT]   controls that match (name is exact, near is in the same row)
  text [grep=TEXT]           visible text lines
  read TARGET                an element's value and the row it is in
  click TARGET
  fill TARGET value=TEXT     click an empty field and type TEXT
  select TARGET option=TEXT  choose an option in a list or drop-down
  press KEY                  Enter, Tab, Escape, ArrowDown, ...
  scroll down|up [px=N]      scroll where the pointer is (click inside the area first)
  screenshot                 an image of the screen (costly; only when the text is not enough)
  sh COMMAND                 read-only shell in your working directory: grep, head, wc, sort, ... joined with |
  save FILE                  write the previous command's output to FILE in the working directory
  wait ms=N                  pause up to 2000 ms
TARGET is @N from the latest tree or find, or role=R name="..." [near="..."];
on a screen with no structure, text="..." (from OCR). Quote values with spaces.
Refs change after every action. After a write is committed the script stops,
so you can check the result."""

#: Roles the agent may name, mapped to the roles a listing uses.
_ROLE_ALIASES = {
    "field": "textbox",
    "input": "textbox",
    "textarea": "textbox",
    "text field": "textbox",
    "dropdown": "combobox",
    "select": "combobox",
    "listbox": "combobox",
    "a": "link",
}

_MAX_WAIT_MS = 2000
_MAX_LINES = 40


class ScriptError(ValueError):
    """A line could not run. Nothing in it was performed; the agent sees why."""


class LeftTheApp(RuntimeError):
    """An action took the browser to another origin. The run ends: the agent
    may act on the target app only."""


def _shown_url(url: Any) -> str:
    """A page URL without its query string, which can hold session tokens
    (OpenEMR's ``token_main``). The path and fragment stay: they say where the
    agent is."""

    parts = urlsplit(str(url or ""))
    if not parts.scheme:
        return str(url or "")
    shown = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if parts.query:
        shown += "?..."
    if parts.fragment:
        shown += f"#{parts.fragment}"
    return shown


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


@dataclass
class Element:
    """One element the agent can name: from the DOM, a tree, or OCR."""

    ref: str
    role: str
    name: str
    x: int
    y: int
    w: int = 0
    h: int = 0
    value: Optional[str] = None
    #: The text of the table row the element is in.
    context: str = ""
    #: The text of the row, dialog, form or section around it (the gate's scope).
    scope: str = ""
    tag: str = ""
    options: tuple[str, ...] = ()
    focused: bool = False
    disabled: bool = False
    checked: bool = False
    frame: int = 0
    source: str = "dom"
    automation_id: Optional[str] = None
    #: A desktop tree node the authoring session remembers.
    node_id: Optional[str] = None

    def identity(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "name": self.name}
        if self.context:
            out["row"] = self.context
        if self.automation_id:
            out["automation_id"] = self.automation_id
        out["source"] = self.source
        if self.frame:
            out["frame"] = self.frame
        return out

    def line(self) -> str:
        part = f'@{self.ref} {self.role} "{self.name}"'
        if self.value is not None:
            part += f' value="{self.value}"'
        if self.options:
            shown = ", ".join(f'"{o}"' for o in self.options[:12])
            more = f", +{len(self.options) - 12} more" if len(self.options) > 12 else ""
            part += f" options=[{shown}{more}]"
        flags = [
            name
            for name, on in (
                ("focused", self.focused),
                ("checked", self.checked),
                ("disabled", self.disabled),
            )
            if on
        ]
        if flags:
            part += " (" + ", ".join(flags) + ")"
        return part


# -- surfaces ------------------------------------------------------------------------

_DOM_JS = (
    "([sel, scopeSel]) => {\nconst fieldLabel = "
    + "%(field_label)s"
    + r""";
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  function nameOf(el) {
    let n = clean(el.getAttribute('aria-label'));
    if (!n) n = clean(fieldLabel(el));
    if (!n && el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type))
      n = clean(el.value);
    if (!n) n = clean(el.textContent).slice(0, 120);
    if (!n) n = clean(el.getAttribute('title'));
    return n.slice(0, 120);
  }
  function roleOf(el) {
    const r = el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return t;
      if (/^(button|submit|reset|image)$/.test(t)) return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return tag;
  }
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth)
      return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  }
  function ownText(el) {
    let t = '';
    for (const c of el.childNodes) if (c.nodeType === 3) t += c.textContent;
    return clean(t);
  }
  // A row with many controls is page layout, not a record row.
  const rows = Array.from(document.querySelectorAll('tr, [role=row]'))
    .filter(r => r.querySelectorAll(sel).length <= 8);
  const rowOf = (el) => {
    let r = el.closest('tr, [role=row]');
    while (r && rows.indexOf(r) < 0) r = r.parentElement && r.parentElement.closest('tr, [role=row]');
    return r;
  };
  const out = [];
  const scopeText = new Map();
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
    const tr = rowOf(el);
    const row = tr ? rows.indexOf(tr) : -1;
    if (el.matches(sel)) {
      if (!visible(el)) {
        const b = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (b.width > 0 && b.height > 0 && st.visibility !== 'hidden' &&
            st.display !== 'none' && (b.top > innerHeight || b.bottom < 0))
          out.push({kind: 'offscreen', where: b.top > innerHeight ? 'below' : 'above',
                    name: nameOf(el), row: -1});
        continue;
      }
      const r = el.getBoundingClientRect();
      let value = null, options = [];
      if (tag === 'input' || tag === 'textarea')
        value = el.type === 'password' ? '' : String(el.value || '');
      if (tag === 'select') {
        options = Array.from(el.options).map(o => clean(o.textContent)).slice(0, 80);
        const chosen = el.selectedOptions && el.selectedOptions[0];
        value = chosen ? clean(chosen.textContent) : '';
      }
      const scope = el.closest(scopeSel) || document.body;
      if (!scopeText.has(scope)) {
        // The gate's scope: the text around the control, and the values in
        // its fields (a new record's identity is typed, not printed).
        const values = Array.from(scope.querySelectorAll('input, textarea, select'))
          .filter(f => f.type !== 'password' && f.type !== 'hidden')
          .map(f => f.tagName === 'SELECT'
            ? clean(f.selectedOptions && f.selectedOptions[0] ?
                    f.selectedOptions[0].textContent : '')
            : clean(f.value))
          .filter(Boolean);
        scopeText.set(scope, (clean(scope.innerText) + ' ' + values.join(' ')).slice(0, 6000));
      }
      out.push({kind: 'control', tag: tag, role: roleOf(el), name: nameOf(el),
        value: value, options: options,
        focused: document.activeElement === el,
        checked: !!el.checked || el.getAttribute('aria-checked') === 'true' ||
          el.getAttribute('aria-selected') === 'true',
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        x: r.left, y: r.top, w: r.width, h: r.height, row: row,
        context: tr ? clean(tr.innerText).slice(0, 300) : '',
        scope: scopeText.get(scope)});
      continue;
    }
    if (el.closest(sel)) continue;
    const text = ownText(el);
    if (!text || !visible(el)) continue;
    out.push({kind: /^h[1-6]$/.test(tag) ? 'heading' : 'text', text: text.slice(0, 200),
              row: row});
  }
  return {url: location.href, title: document.title, items: out};
}"""
)

_HIT_JS = (
    "([px, py, sel]) => {\nconst fieldLabel = "
    + "%(field_label)s"
    + r""";
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const hit = document.elementFromPoint(px, py);
  if (!hit) return null;
  const el = hit.closest(sel);
  if (!el) return {name: '', context: ''};
  let n = clean(el.getAttribute('aria-label'));
  if (!n) n = clean(fieldLabel(el));
  if (!n && el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type))
    n = clean(el.value);
  if (!n) n = clean(el.textContent).slice(0, 120);
  if (!n) n = clean(el.getAttribute('title'));
  let tr = el.closest('tr, [role=row]');
  while (tr && tr.querySelectorAll(sel).length > 8)
    tr = tr.parentElement && tr.parentElement.closest('tr, [role=row]');
  let value = null;
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
    value = el.type === 'password' ? '' : String(el.value || '');
  if (el.tagName === 'SELECT') {
    const chosen = el.selectedOptions && el.selectedOptions[0];
    value = chosen ? clean(chosen.textContent) : '';
  }
  return {name: n.slice(0, 120), context: tr ? clean(tr.innerText).slice(0, 300) : '',
          value: value};
}"""
)


def _field_label_js() -> str:
    from openadapt_flow.backends.playwright_backend import _FIELD_LABEL_JS

    return _FIELD_LABEL_JS


class DomSurface:
    """A web page, every frame included. Click points are top-level pixels."""

    kind = "dom"

    def __init__(self, page: Any, backend: Any = None) -> None:
        self.page = page
        self.backend = backend
        label = _field_label_js()
        self._dom_js = _DOM_JS.replace("%(field_label)s", label)
        self._hit_js = _HIT_JS.replace("%(field_label)s", label)

    def _frames(self) -> list[tuple[int, Any, float, float]]:
        """``(index, frame, x offset, y offset)`` for the page and each visible
        frame in it."""

        out: list[tuple[int, Any, float, float]] = [(0, self.page.main_frame, 0.0, 0.0)]
        for index, frame in enumerate(self.page.frames):
            if frame is self.page.main_frame:
                continue
            try:
                box = frame.frame_element().bounding_box()
            except Exception:  # noqa: BLE001 - a detached frame is not listed
                continue
            if not box or box["width"] <= 0 or box["height"] <= 0:
                continue
            out.append((index, frame, float(box["x"]), float(box["y"])))
        return out

    def read(self) -> tuple[str, list[dict[str, Any]], list[Element]]:
        """``(header, items, elements)`` across every frame."""

        items: list[dict[str, Any]] = []
        elements: list[Element] = []
        header = ""
        for index, frame, ox, oy in self._frames():
            try:
                raw = frame.evaluate(
                    self._dom_js, [_observe.INTERACTIVE, _observe.SCOPE]
                )
            except Exception:  # noqa: BLE001 - a frame that navigates away is skipped
                continue
            if index == 0:
                header = (
                    f"page: {raw.get('title') or ''} ({_shown_url(raw.get('url'))})"
                )
            elif raw.get("items"):
                items.append(
                    {
                        "kind": "heading",
                        "text": f"frame {index}: {raw.get('title') or ''}",
                        "row": -1,
                        "frame": index,
                    }
                )
            hidden: dict[str, list[str]] = {}
            for item in raw.get("items") or []:
                item = dict(item, frame=index)
                if item["kind"] == "offscreen":
                    hidden.setdefault(str(item["where"]), []).append(str(item["name"]))
                    continue
                if item["kind"] == "control":
                    w, h = float(item["w"]), float(item["h"])
                    element = Element(
                        ref=str(len(elements) + 1),
                        role=str(item.get("role") or item.get("tag") or ""),
                        name=str(item.get("name") or ""),
                        x=int(ox + float(item["x"]) + w / 2),
                        y=int(oy + float(item["y"]) + h / 2),
                        w=int(w),
                        h=int(h),
                        value=item.get("value"),
                        context=str(item.get("context") or ""),
                        scope=str(item.get("scope") or ""),
                        tag=str(item.get("tag") or ""),
                        options=tuple(str(o) for o in item.get("options") or ()),
                        focused=bool(item.get("focused")),
                        disabled=bool(item.get("disabled")),
                        checked=bool(item.get("checked")),
                        frame=index,
                        source="dom",
                    )
                    elements.append(element)
                    item["ref"] = element.ref
                items.append(item)
            for where, names in sorted(hidden.items()):
                shown = ", ".join(f'"{n}"' for n in names[:8] if n)
                more = f" and {len(names) - 8} more" if len(names) > 8 else ""
                items.append(
                    {
                        "kind": "text",
                        "row": -1,
                        "frame": index,
                        "text": f"({len(names)} controls {where} the visible area"
                        + (f": {shown}{more}" if shown else "")
                        + f"; scroll {'down' if where == 'below' else 'up'} to reach them)",
                    }
                )
        return header, items, elements

    def origin(self) -> Optional[str]:
        parts = urlsplit(str(getattr(self.page, "url", "") or ""))
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme else None

    def at(self, element: Element) -> Optional[dict[str, Any]]:
        """What a click at the element's point would hit, read in its frame."""

        point = None
        getter = getattr(self.backend, "_frame_point", None)
        if callable(getter):
            try:
                point = getter(element.x, element.y)
            except Exception:  # noqa: BLE001
                point = None
        try:
            if point is not None:
                return point.scope.evaluate(
                    self._hit_js, [point.x, point.y, _observe.INTERACTIVE]
                )
            if element.frame:
                return None
            return self.page.evaluate(
                self._hit_js, [element.x, element.y, _observe.INTERACTIVE]
            )
        except Exception:  # noqa: BLE001
            return None

    def focused(self) -> Optional[dict[str, Any]]:
        return _observe.focused_element(self.page)

    def screen_text(self) -> str:
        texts: list[str] = []
        for _index, frame, _ox, _oy in self._frames():
            try:
                texts.append(str(frame.evaluate("() => document.body.innerText") or ""))
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(texts)

    def locator_unique(self, locator: Any) -> Optional[bool]:
        resolve = getattr(self.backend, "_locator_with_scope", None)
        if not callable(resolve):
            return None
        try:
            got = resolve(locator)
            return None if got is None else got[1].count() == 1
        except Exception:  # noqa: BLE001
            return None


class TreeSurface:
    """A desktop app with an accessibility layer (UIA, AX, AT-SPI).

    It lists what :meth:`AuthoringSession.observe` projects (the same tree the
    MCP recording tools read) and finds an exact role and name with the
    backend's ``locate_structural``. Text comes from OCR.
    """

    kind = "tree"

    def __init__(self, session: Any, backend: Any) -> None:
        self.session = session
        self.backend = backend

    def read(self) -> tuple[str, list[dict[str, Any]], list[Element]]:
        try:
            payload = self.session.observe()
        except Exception:  # noqa: BLE001 - no tree is an ordinary miss
            payload = {"tree": []}
        elements: list[Element] = []
        items: list[dict[str, Any]] = []
        nodes = getattr(self.session, "_nodes", {})
        for node in payload.get("tree") or []:
            pixels = nodes.get(node.get("node_id"))
            if pixels is None:
                continue
            element = Element(
                ref=str(len(elements) + 1),
                role=str(node.get("role") or "unknown"),
                name=str(node.get("name") or ""),
                x=int(pixels.x + pixels.w / 2),
                y=int(pixels.y + pixels.h / 2),
                w=int(pixels.w),
                h=int(pixels.h),
                focused=bool(node.get("focused")),
                disabled=not bool(node.get("enabled", True)),
                source="tree",
                automation_id=node.get("automation_id"),
                node_id=node.get("node_id"),
            )
            elements.append(element)
            items.append({"kind": "control", "ref": element.ref, "row": -1})
        header = f"window: {self.session.backend_kind} app"
        if not elements:
            header += " (no accessibility listing; text is from OCR)"
        return header, items, elements

    def locate(self, role: str, name: str) -> Optional[Element]:
        from openadapt_flow.ir import StructuralLocator

        locate = getattr(self.backend, "locate_structural", None)
        if not callable(locate):
            return None
        try:
            handle = locate(StructuralLocator(role=role, name=name))
        except Exception as exc:  # noqa: BLE001 - ambiguity is the agent's to fix
            raise ScriptError(
                f"{role} {name!r} is not unique on screen: {exc}"
            ) from exc
        if handle is None:
            return None
        x, y = int(handle.point[0]), int(handle.point[1])
        return Element(ref="", role=role, name=name, x=x, y=y, source="tree")

    def origin(self) -> Optional[str]:
        return None

    def at(self, element: Element) -> Optional[dict[str, Any]]:
        getter = getattr(self.backend, "structural_locator_at", None)
        if not callable(getter):
            return None
        try:
            loc = getter(element.x, element.y)
        except Exception:  # noqa: BLE001
            return None
        return None if loc is None else {"name": loc.name or "", "context": ""}

    def focused(self) -> Optional[dict[str, Any]]:
        return None

    def screen_text(self) -> str:
        return _ocr_text(self.session.screenshot())

    def locator_unique(self, locator: Any) -> Optional[bool]:
        locate = getattr(self.backend, "locate_structural", None)
        if not callable(locate):
            return None
        try:
            return locate(locator) is not None
        except Exception:  # noqa: BLE001 - refused as ambiguous
            return False


class PixelSurface:
    """An RDP or Citrix window: pixels only. Elements are OCR text lines."""

    kind = "pixels"

    def __init__(
        self, session: Any, ocr: Optional[Callable[[bytes], list[Any]]] = None
    ):
        self.session = session
        self._ocr = ocr

    def _lines(self) -> list[Any]:
        png = self.session.screenshot()
        if self._ocr is not None:
            return list(self._ocr(png))
        from openadapt_flow.vision.ocr import ocr

        return list(ocr(png))

    def read(self) -> tuple[str, list[dict[str, Any]], list[Element]]:
        elements: list[Element] = []
        items: list[dict[str, Any]] = []
        lines = self._lines()
        everything = "\n".join(str(line.text) for line in lines)
        for line in lines:
            x, y, w, h = (int(v) for v in line.region)
            element = Element(
                ref=str(len(elements) + 1),
                role="text",
                name=str(line.text),
                x=x + w // 2,
                y=y + h // 2,
                w=w,
                h=h,
                scope=everything,
                source="ocr",
            )
            elements.append(element)
            items.append({"kind": "control", "ref": element.ref, "row": -1})
        return "screen: pixels only (text is from OCR)", items, elements

    def origin(self) -> Optional[str]:
        return None

    def at(self, element: Element) -> Optional[dict[str, Any]]:
        return None

    def focused(self) -> Optional[dict[str, Any]]:
        return None

    def screen_text(self) -> str:
        return "\n".join(str(line.text) for line in self._lines())

    def locator_unique(self, locator: Any) -> Optional[bool]:
        return None


def _ocr_text(png: bytes) -> str:
    try:
        from openadapt_flow.vision.ocr import ocr

        return "\n".join(str(line.text) for line in ocr(png))
    except Exception:  # noqa: BLE001 - no OCR engine installed
        return ""


def surface_for(harness: Harness) -> Any:
    """The surface the agent reads: the DOM, a desktop tree, or pixels."""

    session = getattr(harness.computer, "session", None)
    backend = getattr(session, "backend", None)
    if harness.has_page:
        return DomSurface(harness.page, backend)
    if getattr(session, "screen_only", False):
        return PixelSurface(session)
    return TreeSurface(session, backend)


# -- the trace --------------------------------------------------------------------------


@dataclass
class ActionTrace:
    """What the agent did, as structured steps, and what it read."""

    surface: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    reads: list[dict[str, Any]] = field(default_factory=list)
    #: The task's identity strings and values: which ones each read showed.
    watch: dict[str, str] = field(default_factory=dict)
    scripts: int = 0
    #: Every script and what it printed (scrubbed, cut to 3,000 characters).
    log: list[dict[str, Any]] = field(default_factory=list)
    lines: int = 0
    errors: int = 0
    screenshots: int = 0
    shell_commands: int = 0
    shell_isolation: Optional[str] = None

    def saw(self, command: str, text: str) -> None:
        shown = sorted(
            name for name, value in self.watch.items() if _observe.shows(text, value)
        )
        self.reads.append(
            {"after_step": len(self.steps), "command": command, "shows": shown}
        )

    def summary(self) -> dict[str, Any]:
        def seen_before(index: int) -> set[str]:
            return {
                name
                for read in self.reads
                if read["after_step"] <= index
                for name in read["shows"]
            }

        commits = [s["i"] for s in self.steps if s.get("gated")]
        last_commit = commits[-1] if commits else None
        after = (
            sorted(
                {
                    name
                    for read in self.reads
                    if last_commit is not None and read["after_step"] > last_commit
                    for name in read["shows"]
                }
            )
            if last_commit is not None
            else []
        )
        recorded = [s for s in self.steps if s.get("recorded")]
        return {
            "surface": self.surface,
            "scripts": self.scripts,
            "lines": self.lines,
            "actions": len(self.steps),
            "errors": self.errors,
            "screenshots": self.screenshots,
            "shell_commands": self.shell_commands,
            "shell_isolation": self.shell_isolation,
            "by_structure": sum(1 for s in self.steps if s.get("element")),
            "with_recorded_locator": len(recorded),
            "locator_agrees": sum(1 for s in recorded if s.get("agrees")),
            "locator_unique": sum(1 for s in recorded if s.get("unique")),
            "commits": len(commits),
            "identity_read_before_commit": (
                sorted(seen_before(commits[0])) if commits else []
            ),
            "read_back_after_commit": after,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "openadapt.code-action-trace/v1",
            "summary": self.summary(),
            "steps": self.steps,
            "reads": self.reads,
            "scripts": self.log,
        }


@dataclass
class ToolOutput:
    """What one ``run`` call returns: text, and images the agent asked for."""

    text: str
    images: list[bytes] = field(default_factory=list)
    committed: bool = False

    def blocks(self) -> list[dict[str, Any]]:
        from openadapt_flow.compile_agent.agents import _image

        out: list[dict[str, Any]] = [
            {"type": "text", "text": self.text or "(no output)"}
        ]
        out.extend(_image(png) for png in self.images)
        return out


# -- the tool --------------------------------------------------------------------------


def _parse_args(tokens: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    named: dict[str, str] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key):
            named[key] = value
        else:
            positional.append(token)
    return positional, named


class CodeActionTool:
    """Runs the agent's scripts against the harness, and keeps the trace.

    Args:
        harness: The run's harness (gate, recording computer).
        workdir: The agent's working directory. Default: next to the gate
            evidence, or a temporary one.
        app_origin: The web app's origin; an action that leaves it ends the run.
        watch: Task values whose appearance in a read is evidence
            (identity strings, inputs), by name.
        surface: Override the surface (tests).
    """

    def __init__(
        self,
        harness: Harness,
        *,
        workdir: Optional[Path] = None,
        app_origin: Optional[str] = None,
        watch: Optional[Mapping[str, str]] = None,
        surface: Any = None,
    ) -> None:
        self.harness = harness
        if workdir is None:
            if harness.evidence_dir is not None:
                workdir = Path(harness.evidence_dir).parent / "workspace"
            else:
                workdir = Path(tempfile.mkdtemp(prefix="oa-code-action-"))
        self.sandbox = Sandbox(workdir)
        self.surface = surface if surface is not None else surface_for(harness)
        self.app_origin = app_origin or (
            self.surface.origin() if self.surface.kind == "dom" else None
        )
        self.trace = ActionTrace(
            surface=self.surface.kind,
            watch={k: str(v) for k, v in (watch or {}).items() if str(v).strip()},
            shell_isolation=self.sandbox.isolation,
        )
        self._elements: dict[str, Element] = {}
        self._last_output = ""

    # -- scrubbing ------------------------------------------------------------------

    def _out(self, text: str) -> str:
        return _observe.scrub_lines(text, self.harness.keep)

    # -- one script --------------------------------------------------------------------

    def run(self, script: str) -> ToolOutput:
        """Run every line of ``script`` in order, stopping at the first error or
        after a committed write. GateRefused and LeftTheApp end the run."""

        self.trace.scripts += 1
        lines = [
            line.strip()
            for line in str(script).splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if len(lines) > _MAX_LINES:
            return ToolOutput(f"error: a script may have at most {_MAX_LINES} lines")
        chunks: list[str] = []
        images: list[bytes] = []
        committed = False
        for number, line in enumerate(lines, 1):
            self.trace.lines += 1
            gates = self.harness.stats.gate_checks
            try:
                text, image = self._line(line)
            except (ScriptError, ShellRefused, UnsupportedAction, LookupError) as exc:
                self.trace.errors += 1
                chunks.append(f"$ {line}\nerror: {exc}")
                if number < len(lines):
                    chunks.append(f"(stopped; {len(lines) - number} lines not run)")
                break
            self._last_output = text
            chunks.append(f"$ {line}\n{text}" if text else f"$ {line}")
            if image is not None:
                images.append(image)
            if self.harness.stats.gate_checks > gates:
                committed = True
                if number < len(lines):
                    chunks.append(
                        f"(a write was committed; {len(lines) - number} lines not "
                        "run. Read the screen to check the result.)"
                    )
                break
        out = ToolOutput("\n".join(chunks), images, committed)
        self.trace.log.append(
            {"script": str(script), "output": out.text[:3000], "images": len(images)}
        )
        return out

    def _line(self, line: str) -> tuple[str, Optional[bytes]]:
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise ScriptError(f"can't parse the line: {exc}") from exc
        command, rest = tokens[0].lower(), tokens[1:]
        if command == "sh":
            if not rest:
                raise ScriptError("sh needs a command")
            self.trace.shell_commands += 1
            result = self.sandbox.run(line.split(None, 1)[1])
            text = result.output.rstrip("\n")
            if result.exit_code:
                text += f"\n(exit {result.exit_code})"
            return self._out(text), None
        if command == "save":
            if len(rest) != 1:
                raise ScriptError("save needs one file name")
            path = self.sandbox.write(rest[0], self._last_output)
            return f"saved {len(self._last_output)} characters to {path.name}", None
        positional, named = _parse_args(rest)
        if command == "tree":
            return self._tree(named), None
        if command == "find":
            return self._find(named), None
        if command == "text":
            return self._text(named), None
        if command == "screenshot":
            self.trace.screenshots += 1
            png = self.harness.screenshot("requested")
            return "(screenshot attached)", png
        if command == "wait":
            ms = min(max(int(named.get("ms", "500") or 0), 0), _MAX_WAIT_MS)
            time.sleep(ms / 1000)
            return f"waited {ms} ms", None
        if command == "read":
            element = self._target(positional, named)
            got = self.surface.at(element) or {}
            value = got.get("value", element.value)
            text = element.line()
            if value is not None and value != element.value:
                text += f' now="{value}"'
            if element.context:
                text += f"\n  row: {element.context}"
            elif element.scope:
                text += f"\n  in: {element.scope[:300]}"
            self.trace.saw(line, text)
            return self._out(text), None
        if command in ("click", "fill", "select", "press"):
            return self._act(command, line, positional, named), None
        if command == "scroll":
            return self._scroll(line, positional, named), None
        raise ScriptError(
            f"unknown command {command!r}; commands are tree, find, text, read, "
            "click, fill, select, press, scroll, screenshot, sh, save, wait"
        )

    # -- reads -----------------------------------------------------------------------

    def _read(self) -> tuple[str, list[dict[str, Any]], list[Element]]:
        header, items, elements = self.surface.read()
        self._elements = {e.ref: e for e in elements}
        return header, items, elements

    def _tree(self, named: Mapping[str, str]) -> str:
        header, items, elements = self._read()
        by_ref = {e.ref: e for e in elements}
        lines: list[str] = []
        current: Optional[tuple[int, int]] = None
        parts: list[str] = []

        def flush() -> None:
            nonlocal parts
            if parts:
                lines.append("row: " + " | ".join(parts))
            parts = []

        for item in items:
            if item["kind"] == "control":
                part = by_ref[item["ref"]].line()
            elif item["kind"] == "heading":
                part = f"# {item['text']}"
            else:
                part = str(item["text"])
            row = int(item.get("row", -1))
            key = (int(item.get("frame", 0)), row)
            if row >= 0:
                if key != current:
                    flush()
                    current = key
                parts.append(part)
                continue
            flush()
            current = None
            lines.append(part)
        flush()
        if self.surface.kind == "tree" and not elements:
            lines.append(self.surface.screen_text())
        grep = named.get("grep")
        if grep:
            lines = [ln for ln in lines if grep.lower() in ln.lower()]
        limit = int(named.get("max", "0") or 0)
        cut = ""
        if limit and len(lines) > limit:
            cut = f"\n({len(lines) - limit} more lines; use grep= or max=)"
            lines = lines[:limit]
        text = header + "\n" + "\n".join(lines) + cut
        self.trace.saw("tree", text)
        return self._out(text)

    def _find(self, named: Mapping[str, str]) -> str:
        if not named:
            raise ScriptError("find needs role=, name= or near=")
        unknown = set(named) - {"role", "name", "near", "text"}
        if unknown:
            raise ScriptError(f"find takes role=, name=, near=; not {sorted(unknown)}")
        header, _items, _elements = self._read()
        hits = self._match(named, exact=False)
        text = "\n".join(
            e.line() + (f"  row: {e.context}" if e.context else "") for e in hits
        )
        text = text or "no match"
        self.trace.saw("find", text)
        return self._out(text)

    def _text(self, named: Mapping[str, str]) -> str:
        text = self.surface.screen_text()
        grep = named.get("grep")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if grep:
            lines = [ln for ln in lines if grep.lower() in ln.lower()]
        out = "\n".join(lines[:200]) or "no text"
        self.trace.saw("text", out)
        return self._out(out)

    # -- targets -----------------------------------------------------------------------

    def _match(self, named: Mapping[str, str], *, exact: bool) -> list[Element]:
        role = named.get("role")
        if role:
            role = _ROLE_ALIASES.get(role.lower(), role.lower())
        name = named.get("name", named.get("text"))
        near = named.get("near")
        hits: list[Element] = []
        for element in self._elements.values():
            if role and element.role.lower() != role:
                continue
            if name is not None:
                if exact and _norm(element.name) != _norm(name):
                    continue
                if not exact and _norm(name) not in _norm(element.name):
                    continue
            if near is not None and _norm(near) not in _norm(
                element.context or element.scope
            ):
                continue
            hits.append(element)
        return hits

    def _target(self, positional: Sequence[str], named: Mapping[str, str]) -> Element:
        refs = [p for p in positional if p.startswith("@")]
        if refs:
            if len(refs) != 1 or positional[0] != refs[0]:
                raise ScriptError("give one @ref, or role=/name=/near=")
            ref = refs[0][1:]
            element = self._elements.get(ref)
            if element is None:
                raise ScriptError(
                    f"@{ref} is not in the latest listing; run tree or find"
                )
            return element
        described = {
            k: v for k, v in named.items() if k in ("role", "name", "near", "text")
        }
        if not described.get("name") and not described.get("text"):
            raise ScriptError("a target needs @N, or name= (with role= or near=)")
        self._read()
        hits = self._match(described, exact=True)
        if not hits and self.surface.kind == "tree" and described.get("role"):
            located = self.surface.locate(described["role"], described["name"])
            if located is not None:
                return located
        if len(hits) == 1:
            return hits[0]
        if not hits:
            near = self._match(
                {"name": described.get("name") or described.get("text", "")},
                exact=False,
            )
            hint = "".join(f"\n  {e.line()}" for e in near[:5])
            raise ScriptError(
                "nothing matches "
                + _describe(described)
                + (f"; close matches:{hint}" if hint else "")
            )
        listed = "".join(
            f"\n  {e.line()}" + (f"  row: {e.context}" if e.context else "")
            for e in hits[:6]
        )
        raise ScriptError(
            f"{len(hits)} elements match {_describe(described)}; add near= or use "
            f"a ref:{listed}"
        )

    def _still_there(self, element: Element) -> None:
        if element.source != "dom":
            return
        got = self.surface.at(element)
        if got is None:
            raise ScriptError(
                f"@{element.ref} can't be read at its place (covered or moved); "
                "run tree again"
            )
        if not got.get("name") and element.name:
            raise ScriptError(
                f"@{element.ref} ({element.name!r}) is covered by another element "
                "(a dialog or overlay?); run tree or screenshot"
            )
        if _norm(got.get("name")) != _norm(element.name) or _norm(
            got.get("context")
        ) != _norm(element.context):
            raise ScriptError(
                f"@{element.ref} ({element.name!r}) is no longer at its place; "
                "run tree again"
            )

    # -- actions -----------------------------------------------------------------------

    def _act(
        self,
        command: str,
        line: str,
        positional: Sequence[str],
        named: Mapping[str, str],
    ) -> str:
        if command == "press":
            if len(positional) != 1:
                raise ScriptError("press needs one key, such as Enter or Tab")
            key = positional[0]
            focus = self.surface.focused() or {}
            hint = CommitHint(
                name=str(focus.get("name") or "the focused element"),
                scope=str(focus.get("scope") or ""),
                key=(
                    key
                    if key in ("Enter", "Space") and focus.get("tag") != "select"
                    else None
                ),
            )
            event = self._event_index()
            self._gates_before = len(self.harness.gate_log)
            self._do({"kind": "press", "key": key}, hint)
            step = {"action": "press", "key": key}
            return self._finish_step(step, None, [event], line)

        element = self._target(positional, named)
        self._gates_before = len(self.harness.gate_log)
        if element.disabled:
            raise ScriptError(f"@{element.ref} {element.name!r} is disabled")
        self._still_there(element)
        hint = CommitHint(
            name=element.name,
            scope=element.scope or element.context,
            editable=element.role in ("textbox", "searchbox", "spinbutton"),
        )
        events: list[int] = []
        if command == "click":
            events.append(self._event_index())
            recorded = self._click(element, hint)
            step = {"action": "click", "element": element.identity()}
            return self._finish_step(step, recorded, events, line)

        if command == "fill":
            if "value" not in named:
                raise ScriptError('fill needs value="..."')
            value = named["value"]
            if element.role not in ("textbox", "searchbox", "combobox", "spinbutton"):
                raise ScriptError(f"@{element.ref} is a {element.role}, not a field")
            if element.value not in (None, ""):
                raise ScriptError(
                    f"@{element.ref} already holds text; fill types into an empty "
                    "field only (typing would append)"
                )
            events.append(self._event_index())
            recorded = self._click(element, hint)
            events.append(self._event_index())
            self._do({"kind": "type", "text": value}, None)
            got = self.surface.at(element) or {}
            step = {
                "action": "fill",
                "element": element.identity(),
                "value": value,
                "value_after": got.get("value"),
            }
            out = self._finish_step(step, recorded, events, line)
            if got.get("value") is not None and got.get("value") != value:
                out += f'\nnote: the field now shows "{got.get("value")}"'
            return out

        # select
        if "option" not in named:
            raise ScriptError('select needs option="..."')
        wanted = named["option"]
        if element.tag == "select":
            options = {_norm(o): o for o in element.options}
            option = options.get(_norm(wanted))
            if option is None:
                raise ScriptError(
                    f"{wanted!r} is not an option of @{element.ref}; options: "
                    + ", ".join(f'"{o}"' for o in element.options[:20])
                )
            events.append(self._event_index())
            recorded = self._click(
                element, CommitHint(name=element.name, editable=True)
            )
            events.append(self._event_index())
            self._do({"kind": "type", "text": option}, None)
            events.append(self._event_index())
            self._do({"kind": "press", "key": "Tab"}, None)
            got = self.surface.at(element) or {}
            chosen = got.get("value")
            step = {
                "action": "select",
                "element": element.identity(),
                "value": option,
                "value_after": chosen,
            }
            out = self._finish_step(step, recorded, events, line)
            if chosen is not None and _norm(chosen) != _norm(option):
                raise ScriptError(
                    out + f'\nthe list shows "{chosen}", not "{option}"; check it '
                    "before going on"
                )
            return out
        # A custom list: open it, then click the option by name.
        events.append(self._event_index())
        recorded = self._click(element, CommitHint(name=element.name, editable=True))
        self._read()
        options = [
            e
            for e in self._elements.values()
            if e.role in ("option", "menuitem", "listitem", "radio", "treeitem")
            and _norm(e.name) == _norm(wanted)
        ]
        if len(options) != 1:
            raise ScriptError(
                f"opened @{element.ref}, but {len(options)} options are named "
                f"{wanted!r}; run find role=option to see them"
            )
        events.append(self._event_index())
        self._click(options[0], CommitHint(name=options[0].name, scope=""))
        step = {
            "action": "select",
            "element": element.identity(),
            "value": wanted,
            "option": options[0].identity(),
        }
        return self._finish_step(step, recorded, events, line)

    def _scroll(
        self, line: str, positional: Sequence[str], named: Mapping[str, str]
    ) -> str:
        where = (positional[0] if positional else "down").lower()
        if where not in ("down", "up"):
            raise ScriptError("scroll down or scroll up, with px=N (default 500)")
        px = min(max(int(named.get("px", "500") or 0), 1), 2000)
        dy = px if where == "down" else -px
        if self.harness.computer.pointer is None:
            raise ScriptError(
                "the recording scrolls where the pointer is; click inside the "
                "area you want to scroll first"
            )
        event = self._event_index()
        self._gates_before = len(self.harness.gate_log)
        before = self._scroll_offsets()
        self._do({"kind": "scroll", "dx": 0, "dy": dy}, None)
        moved = before is None or self._scroll_offsets() != before
        step = {"action": "scroll", "key": f"{where} {px}px", "moved": moved}
        out = self._finish_step(step, None, [event], line)
        if not moved:
            raise ScriptError(
                "the scroll was sent, but nothing scrolled: the pointer is over a part of the "
                "screen that does not scroll (a fixed bar?). Click a field or "
                "label in the area you want to scroll, then scroll again"
            )
        return out

    def _scroll_offsets(self) -> Optional[tuple[Any, ...]]:
        if self.surface.kind != "dom":
            return None
        offsets: list[Any] = []
        js = (
            "() => [scrollX, scrollY, ...Array.from(document.querySelectorAll('*'))"
            ".filter(e => e.scrollTop || e.scrollLeft).slice(0, 50)"
            ".map(e => e.scrollTop * 100000 + e.scrollLeft)]"
        )
        for _index, frame, _ox, _oy in self.surface._frames():
            try:
                offsets.append(tuple(frame.evaluate(js)))
            except Exception:  # noqa: BLE001
                offsets.append(None)
        return tuple(offsets)

    def _event_index(self) -> int:
        recorder = getattr(
            getattr(self.harness.computer, "session", None), "_recorder", None
        )
        count = getattr(recorder, "event_count", None)
        return int(count) if count is not None else len(self.trace.steps)

    def _recorded_event(self, index: int) -> Optional[dict[str, Any]]:
        """Line ``index`` of the recording's events.jsonl, or None."""

        import json

        recorder = getattr(
            getattr(self.harness.computer, "session", None), "_recorder", None
        )
        path = getattr(recorder, "_events_path", None)
        if path is None:
            return None
        try:
            lines = Path(path).read_text().splitlines()
            return json.loads(lines[index]) if 0 <= index < len(lines) else None
        except (OSError, ValueError):
            return None

    def _click(self, element: Element, hint: CommitHint) -> Optional[dict[str, Any]]:
        """Click the element's point and return the locator the recorder will
        store for it (read the same way, just before the click)."""

        locator = None
        getter = getattr(
            getattr(self.harness.computer, "session", None), "backend", None
        )
        getter = getattr(getter, "structural_locator_at", None)
        if callable(getter):
            try:
                locator = getter(element.x, element.y)
            except Exception:  # noqa: BLE001
                locator = None
        recorded = None
        if locator is not None:
            # Before the click: the program resolves it on this screen.
            recorded = locator.model_dump(exclude_none=True)
            recorded["unique"] = self.surface.locator_unique(locator)
        self._do({"kind": "click", "x": element.x, "y": element.y}, hint)
        return recorded

    def _do(self, native: Mapping[str, Any], hint: Optional[CommitHint]) -> None:
        self.harness.act(native, hint=hint)
        self._settle()
        origin = self.surface.origin()
        if self.app_origin and origin and origin != self.app_origin:
            raise LeftTheApp(
                f"the action left the app ({self.app_origin}) for {origin}; the "
                "agent may act on the target app only"
            )

    def _settle(self) -> None:
        if self.surface.kind != "dom":
            return
        try:
            self.harness.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:  # noqa: BLE001 - a page that never loads is still usable
            pass

    def _finish_step(
        self,
        step: dict[str, Any],
        recorded: Optional[dict[str, Any]],
        events: list[int],
        line: str,
    ) -> str:
        step["i"] = len(self.trace.steps)
        step["line"] = line
        step["events"] = events
        step["gated"] = len(self.harness.gate_log) > self._gates_before
        if step["gated"]:
            step["gate"] = self.harness.gate_log[-1].get("decision")
        event = self._recorded_event(events[0]) if events else None
        if event is not None and event.get("structural"):
            # What the recorder actually stored is what the compiler reads.
            unique = (recorded or {}).get("unique")
            stored = dict(event["structural"])
            if recorded is not None:
                step["matches_precheck"] = stored == {
                    k: v for k, v in recorded.items() if k != "unique"
                }
            recorded = {**stored, "unique": unique}
        if recorded is not None:
            unique = recorded.pop("unique", None)
            step["recorded"] = recorded
            step["unique"] = unique
            element = step.get("element") or {}
            step["agrees"] = bool(
                recorded.get("selector")
                or _norm(recorded.get("name")) == _norm(element.get("name"))
            )
        self.trace.steps.append(step)
        what = step["action"]
        if "element" in step:
            el = step["element"]
            text = f'ok: {what} {el["role"]} "{el["name"]}"'
            if step.get("value") is not None:
                text += f' value="{step["value"]}"'
        else:
            text = f"ok: {what} {step.get('key')}"
        if step["gated"]:
            text += " (save gate passed)"
        header = ""
        if self.surface.kind == "dom":
            page = self.harness.page
            header = f"\npage: {page.title()} ({_shown_url(page.url)})"
        return self._out(text + header)

    _gates_before: int = 0


def _describe(named: Mapping[str, str]) -> str:
    return " ".join(f'{k}="{v}"' for k, v in named.items())


# -- the scripted agent -----------------------------------------------------------------


@dataclass
class ScriptedCodeAgent:
    """Runs fixed scripts through :class:`CodeActionTool`; no model calls.

    Each script may use ``{name}`` for a task input. A script whose last line
    fails ends the run with :class:`ScriptError`.
    """

    scripts: Sequence[str]
    principal_id: Optional[str] = "agent:scripted-code"
    dialect: ClassVar[str] = "generic"
    requires_model_key: ClassVar[bool] = False
    outputs: list[str] = field(default_factory=list)

    def drive(self, request: Any, harness: Harness) -> Any:
        from openadapt_flow.compile_agent.agents import AgentLedger

        ledger = AgentLedger(agent="scripted-code", vision="code")
        tool = CodeActionTool(harness, watch=_watch(request, harness))
        self.last_trace = tool.trace
        for script in self.scripts:
            out = tool.run(str(script).format(**request.inputs))
            self.outputs.append(out.text)
            if "\nerror: " in "\n" + out.text:
                raise ScriptError(out.text)
        ledger.finished = True
        ledger.wall_seconds = time.monotonic() - ledger.started_at
        return ledger


def _watch(request: Any, harness: Harness) -> dict[str, str]:
    """The task values a read is checked for: inputs and identity strings."""

    watch = {f"input:{k}": str(v) for k, v in (request.inputs or {}).items()}
    for i, value in enumerate(harness.gate.on_screen):
        watch[f"identity:{value}"] = value
    return watch


# -- the compiled program --------------------------------------------------------------


def distilled_events(recording_dir: Optional[Path]) -> dict[int, str]:
    """``{event index: rule}`` for the steps distill dropped from a recording
    (its ``distill.json``), or an empty dict."""

    import json

    if recording_dir is None:
        return {}
    path = Path(recording_dir) / "distill.json"
    if not path.is_file():
        return {}
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {int(d["index"]): str(d["rule"]) for d in report.get("dropped") or []}


def check_trace_against_ir(
    trace: Mapping[str, Any],
    workflow: Any,
    *,
    distilled: Optional[Mapping[int, str]] = None,
) -> dict[str, Any]:
    """Does the compiled program carry the identity the agent acted on?

    For each step the agent took by structure, the recorder's locator for its
    target must be on one of the program's steps, in order. A step whose
    recorded event distill dropped (``distilled``, from
    :func:`distilled_events`: a click with no effect, a detour that ended
    where it started) is reported as distilled, not missing. Returns counts
    and the steps that are missing.
    """

    distilled = dict(distilled or {})

    def key(locator: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            locator.get("selector"),
            _norm(locator.get("role")),
            _norm(locator.get("name")),
            locator.get("automation_id"),
            tuple(locator.get("frame_path") or ()),
        )

    program: list[tuple[Any, ...]] = []
    for step in getattr(workflow, "steps", []) or []:
        anchor = getattr(step, "anchor", None)
        structural = getattr(anchor, "structural", None) if anchor else None
        if structural is not None:
            program.append(key(structural.model_dump(exclude_none=True)))
    wanted = []
    dropped: list[dict[str, Any]] = []
    for step in trace.get("steps", []):
        if not step.get("recorded"):
            continue
        events = [int(e) for e in step.get("events") or []]
        if events and events[0] in distilled:
            dropped.append({"step": int(step["i"]), "rule": distilled[events[0]]})
            continue
        wanted.append((int(step["i"]), key(step["recorded"])))
    missing: list[int] = []
    cursor = 0
    for index, locator in wanted:
        found = next(
            (j for j in range(cursor, len(program)) if program[j] == locator), None
        )
        if found is None:
            missing.append(index)
        else:
            cursor = found + 1
    return {
        "trace_steps_with_locator": len(wanted) + len(dropped),
        "distilled_steps": dropped,
        "in_program": len(wanted) - len(missing),
        "missing_steps": missing,
        "program_structural_steps": len(program),
        "passed": not missing,
    }


def element_lines(elements: Iterable[Element]) -> str:
    return "\n".join(e.line() for e in elements)
