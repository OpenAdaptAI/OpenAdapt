"""Read-only browser teach: turn a task done once in a browser into a tool.

``openadapt-agent serve --teach-browser`` serves these MCP tools:

``teach_start``
    Opens a headless browser at the URL the person gave and starts run 1 of a
    named task. The agent declares the inputs (the values that change each
    time, with this run's values).
``teach_observe``, ``teach_act``
    The agent does the task one action at a time on refs from the page
    listing: click, type, select, press. Typing an input's value makes that
    step take the input.
``teach_finish``
    Ends run 1. The task must have downloaded a file. OpenAdapt compiles the
    steps, checks the file with the checks the person named, then runs the
    compiled steps once more with no model to show they reproduce the file.
    The new ``taught_<name>`` tool is then listed.
``taught_<name>``
    Runs the compiled steps with new inputs and returns the downloaded file's
    path, after checking it.

This lane only reads. The browser sends no POST, PUT, PATCH or DELETE
request: each one is blocked, and the run stops. A click on a button that
would send a form by POST, or that is labelled as a change (Save, Delete,
Send, Pay...), is refused before it happens. A compiled run stops before
acting when a control is missing, matches more than one element, or carries
a different label than when it was taught; it never guesses. Writes go
through the governed teach path (``serve --teach``), which reads every save
back.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from openadapt_flow.agent.bridge import BridgeError, ToolSpec
from openadapt_flow.wire_check import (
    READ_METHODS,
    PinnedWrite,
    WireCheck,
    carries_secret,
    mask_secrets,
)

__all__ = [
    "BROWSER_TEACH_TOOLS",
    "PROGRAM_SCHEMA",
    "BrowserTeachBridge",
    "default_files_dir",
    "default_tools_dir",
]

PROGRAM_SCHEMA = "openadapt.browser-tool/v1"
PROGRAM_FILE = "program.json"
TAUGHT_PREFIX = "taught_"
BROWSER_TEACH_TOOLS = (
    "teach_start",
    "teach_observe",
    "teach_act",
    "teach_finish",
    "teach_abort",
    "teach_status",
)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: Labels of controls that change data. A click on one is refused in this
#: read-only lane, whatever the request it would send.
_WRITE_LABEL = re.compile(
    r"\b(save|send|delete|remove|post|publish|pay|buy|purchase|order now|"
    r"place order|checkout|sign|approve|confirm|archive|transfer|upload|"
    r"submit payment|book|reserve|cancel|unsubscribe|deactivate|reset)\b",
    re.IGNORECASE,
)
_STEP_TIMEOUT_MS = 15_000
#: How long a compiled run waits for a control to appear after the page
#: loaded, before it stops.
_APPEAR_MS = 5_000
_MAX_LINES = 120

INSTRUCTIONS = (
    "This server turns a browser task the person does often into a tool you "
    "can call again with new values. It only reads: the first tasks are "
    "exports and lookups, never saves. To teach one, first ask the person: "
    "the page's URL, what the task is, which values change each time (give "
    "each a short name and this time's value), and what proves the result "
    "is right (the file must download; optionally a column whose values must "
    "lie between two inputs, or must equal one). Then call teach_start, use teach_observe and "
    "teach_act until the file downloads, and call teach_finish with the "
    "checks. teach_finish runs the steps again with no model and, when they "
    "reproduce the file, adds a taught_<name> tool. Call it with new values "
    "and tell the person the file path it returns. Outcome done is the only "
    "success; stopped means nothing was downloaded, so tell the person why."
)

WRITE_INSTRUCTIONS = (
    " This server can also teach writes, checked against the operator's write "
    "contracts (" + "teach_start's contract" + "). A taught write's run 1 is a "
    "real save: use a test record. teach_finish verifies run 1 in the system of "
    "record before it adds the tool, and every call of the tool returns a "
    "verified receipt (outcome done) or a refusal (needs_review). Never retry a "
    "refused write; tell the person. Sign-in values are held by the server: type "
    "them with input: <name> from sign_in_inputs; you never see them."
)

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_BROWSES = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
_LOCAL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def default_tools_dir() -> Path:
    return Path.home() / ".openadapt" / "tools"


def default_files_dir() -> Path:
    return Path.home() / "Downloads" / "OpenAdapt"


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


# -- the page, read as lines -------------------------------------------------------

#: One pass over the page in DOM order: headings, short text, table rows and
#: every visible control. Nothing on the page is modified.
_OBSERVE_JS = r"""
() => {
  const INTERACTIVE = 'a[href], button, input:not([type=hidden]), textarea, select, ' +
    '[role=button], [role=link], [role=tab], [role=checkbox], [role=menuitem]';
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const label = el => clean(
    el.getAttribute('aria-label') ||
    (el.labels && el.labels.length ? Array.from(el.labels[0].childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ') ||
      el.labels[0].innerText : '') ||
    el.getAttribute('placeholder') ||
    (el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type) ? el.value : '') ||
    (el.tagName === 'SELECT' || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? '' : el.innerText) ||
    el.getAttribute('title') || el.getAttribute('name') || '').slice(0, 80);
  const role = el => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      return 'textbox';
    }
    return tag;
  };
  const method = el => {
    const form = el.form || el.closest('form');
    if (!form) return '';
    const own = el.getAttribute && el.getAttribute('formmethod');
    return String(own || form.getAttribute('method') || 'get').toUpperCase();
  };
  const lines = [];
  const controls = [];
  let rows = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let el = walker.currentNode;
  while (el) {
    if (el.matches(INTERACTIVE)) {
      if (visible(el) && !el.disabled) {
        const c = {
          tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
          id: el.id || '', name_attr: el.getAttribute('name') || '',
          label: label(el), role: role(el), method: method(el),
          sign_in: Boolean((el.form || el.closest('form')) &&
            (el.form || el.closest('form')).querySelector('input[type=password]')),
          value: el.tagName === 'SELECT'
            ? clean(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '')
            : (['INPUT', 'TEXTAREA'].includes(el.tagName) && !/^(button|submit|reset|password)$/i.test(el.type) ? clean(el.value) : ''),
          options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => clean(o.text)).slice(0, 30) : [],
        };
        controls.push(c);
        lines.push({control: controls.length - 1});
      }
    } else if (/^H[1-6]$/.test(el.tagName) && visible(el)) {
      lines.push({text: '# ' + clean(el.innerText)});
    } else if (el.tagName === 'TR' && visible(el)) {
      rows += 1;
      if (rows <= 25) {
        lines.push({text: 'row: ' + Array.from(el.cells).map(c => clean(c.innerText)).join(' | ')});
      } else if (rows === 26) {
        lines.push({text: '(more rows not shown)'});
      }
    } else if ((el.tagName === 'P' || el.getAttribute('role') === 'alert') && visible(el) &&
               !el.querySelector(INTERACTIVE)) {
      const t = clean(el.innerText);
      if (t) lines.push({text: (el.getAttribute('role') === 'alert' ? 'alert: ' : '') + t.slice(0, 200)});
    }
    el = walker.nextNode();
  }
  return {url: location.href, title: document.title, lines, controls};
}
"""

#: The same label rule as the listing, for one element (the identity check).
_ELEMENT_LABEL_JS = r"""
el => {
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  return clean(
    el.getAttribute('aria-label') ||
    (el.labels && el.labels.length ? Array.from(el.labels[0].childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ') ||
      el.labels[0].innerText : '') ||
    el.getAttribute('placeholder') ||
    (el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type) ? el.value : '') ||
    (el.tagName === 'SELECT' || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? '' : el.innerText) ||
    el.getAttribute('title') || el.getAttribute('name') || '').slice(0, 80);
}
"""

_HREFS_JS = "els => els.map(e => e.getAttribute('href') || '')"

_FOCUSED_JS = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const form = el.form || el.closest('form');
  return {
    tag: el.tagName.toLowerCase(), id: el.id || '',
    name_attr: el.getAttribute('name') || '',
    method: form ? String(form.getAttribute('method') || 'get').toUpperCase() : '',
  };
}
"""


def _render(raw: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    out = [f"page: {raw.get('title', '')} ({raw.get('url', '')})"]
    controls = list(raw.get("controls") or [])
    for item in raw.get("lines") or []:
        if len(out) >= _MAX_LINES:
            out.append("(page listing cut short)")
            break
        if "control" in item:
            i = item["control"]
            c = controls[i]
            line = f'[e{i + 1}] {c["role"]} "{c["label"]}"'
            if c.get("value"):
                line += f" = {c['value']}"
            if c.get("options"):
                line += " options: " + ", ".join(c["options"])
            if c.get("method") == "POST" and c["role"] == "button":
                line += " (sends a POST: refused here)"
            out.append(line)
        else:
            out.append(item["text"])
    return "\n".join(out), controls


# -- sign-in values -------------------------------------------------------------------

_MASK = "\u2022\u2022\u2022\u2022"


def _mask(text: str, secrets: Sequence[str]) -> str:
    return mask_secrets(text, secrets, _MASK)


def _mask_all(value: Any, secrets: Sequence[str]) -> Any:
    if not secrets:
        return value
    if isinstance(value, str):
        return _mask(value, secrets)
    if isinstance(value, dict):
        return {k: _mask_all(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_all(v, secrets) for v in value]
    return value


# -- frames ---------------------------------------------------------------------------

#: The selector of a frame's own element in its parent, when it has a name or id.
_FRAME_SELECTOR_JS = r"""
e => {
  const tag = e.localName;
  if (e.getAttribute('name')) return tag + '[name=' + JSON.stringify(e.getAttribute('name')) + ']';
  if (e.id) return tag + '[id=' + JSON.stringify(e.id) + ']';
  return null;
}
"""


def _frame_chain(frame: Any) -> Optional[list[str]]:
    """Selectors from the top page down to ``frame``, or None when a frame on
    the way has no name or id to find it by again."""

    chain: list[str] = []
    while frame.parent_frame is not None:
        selector = frame.frame_element().evaluate(_FRAME_SELECTOR_JS)
        if not selector:
            return None
        chain.insert(0, selector)
        frame = frame.parent_frame
    return chain


def _observe_page(page: Any, origin: str) -> dict[str, Any]:
    """The page and its same-site frames, as one listing. Each control keeps
    the frame chain it lives in."""

    merged: dict[str, Any] = {"url": page.url, "title": "", "lines": [], "controls": []}
    for frame in page.frames:
        if frame is page.main_frame:
            chain: Optional[list[str]] = []
        else:
            # about:srcdoc and about:blank frames share their parent's site.
            if not frame.url.startswith("about:") and _origin(frame.url) != origin:
                continue
            try:
                chain = _frame_chain(frame)
            except Exception:  # noqa: BLE001 - a frame that went away
                continue
            if chain is None:
                merged["lines"].append({"text": "(a frame with no name is not listed)"})
                continue
        try:
            raw = frame.evaluate(_OBSERVE_JS)
        except Exception:  # noqa: BLE001 - a frame mid-navigation
            continue
        if frame is page.main_frame:
            merged["title"] = raw.get("title", "")
        offset = len(merged["controls"])
        controls = raw.get("controls") or []
        if chain and controls:
            merged["lines"].append({"text": f"frame {' > '.join(chain)}:"})
        for control in controls:
            control["frames"] = list(chain or [])
            merged["controls"].append(control)
        for item in raw.get("lines") or []:
            if "control" in item:
                merged["lines"].append({"control": item["control"] + offset})
            else:
                merged["lines"].append(item)
    return merged


# -- targets -------------------------------------------------------------------------


def _css_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scope(page: Any, target: Mapping[str, Any]) -> Any:
    """The page, or the iframe chain the control was taught in."""

    scope = page
    for selector in target.get("frames") or ():
        scope = scope.frame_locator(selector)
    return scope


def _locator(page: Any, target: Mapping[str, Any]) -> Any:
    scope = _scope(page, target)
    if "css" in target:
        return scope.locator(target["css"])
    if "role" in target:
        return scope.get_by_role(target["role"], name=target["name"], exact=True)
    return scope.get_by_label(target["label_for"], exact=True)


def _candidates(c: Mapping[str, Any]) -> list[dict[str, Any]]:
    tag = c["tag"]
    out: list[dict[str, Any]] = []
    if c.get("id"):
        out.append({"css": f"{tag}[id={_css_str(c['id'])}]"})
    if c.get("name_attr") and tag in ("input", "select", "textarea", "button"):
        out.append({"css": f"{tag}[name={_css_str(c['name_attr'])}]"})
    if c.get("label") and c["role"] in ("button", "link", "checkbox", "tab"):
        out.append({"role": c["role"], "name": c["label"]})
    if c.get("label") and tag in ("input", "select", "textarea"):
        out.append({"label_for": c["label"]})
    return out


def _unique_target(page: Any, c: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The first selector that finds exactly this one control, with the label
    a compiled run checks before acting."""

    frames = list(c.get("frames") or [])
    for cand in _candidates(c):
        if frames:
            cand = {**cand, "frames": frames}
        try:
            if _locator(page, cand).count() == 1:
                return {**cand, "label": c["label"], "role": c["role"]}
        except Exception:  # noqa: BLE001 - a selector the page can't parse
            continue
    if c["role"] == "link" and c.get("label"):
        # Links with the same label and the same address go to the same
        # place (a menu and a body link): any of them will do.
        cand = {"role": "link", "name": c["label"]}
        if frames:
            cand["frames"] = frames
        hrefs = _locator(page, cand).evaluate_all(_HREFS_JS)
        if len(hrefs) > 1 and len(set(hrefs)) == 1 and hrefs[0]:
            return {**cand, "same_href": hrefs[0], "label": c["label"], "role": "link"}
    return None


class StepStopped(Exception):
    """A compiled step could not be done safely; nothing after it ran."""


def _resolve(page: Any, target: Mapping[str, Any], timeout_ms: int) -> Any:
    """The one element ``target`` names, with the label it was taught with.

    Stops when the control does not appear, appears more than once, or
    carries another label: a compiled run never picks between look-alikes.
    """

    loc = _locator(page, target)
    deadline = time.monotonic() + timeout_ms / 1000
    count = 0
    while True:
        count = loc.count()
        if count >= 1 or time.monotonic() > deadline:
            break
        page.wait_for_timeout(100)
    if count == 0:
        raise StepStopped(f"the control {target['label']!r} is not on the page")
    if count > 1 and target.get("same_href"):
        hrefs = loc.evaluate_all(_HREFS_JS)
        if set(hrefs) != {target["same_href"]}:
            raise StepStopped(
                f"{count} links named {target['label']!r} now go to different "
                "places; stopped instead of picking one"
            )
    elif count > 1:
        raise StepStopped(
            f"{count} controls match {target['label']!r}; stopped instead of "
            "picking one"
        )
    label = loc.first.evaluate(_ELEMENT_LABEL_JS)
    if _norm(label) != _norm(target["label"]):
        raise StepStopped(
            f"the control is now labelled {label!r}, not {target['label']!r} as taught"
        )
    return loc.first


# -- the browser thread ----------------------------------------------------------------


class _BrowserThread:
    """Playwright's sync API is bound to the thread that started it, and MCP
    calls arrive on a worker pool. Every browser call runs here."""

    def __init__(self) -> None:
        self._jobs: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._main, name="openadapt-browser-teach", daemon=True
        )
        self._thread.start()

    def _main(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, box, done = job
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in call()
                box["error"] = exc
            done.set()

    def call(self, fn: Callable[[], Any], timeout: float = 120.0) -> Any:
        box: dict[str, Any] = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise BridgeError("the browser did not answer in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def stop(self) -> None:
        self._jobs.put(None)


class _Browser:
    """One headless Chromium context that only reads and stays on one origin."""

    def __init__(
        self,
        origin: str,
        downloads_dir: Path,
        headless: bool,
        *,
        allow_writes: bool = False,
        allowed_writes: Sequence[str] = (),
        pinned: Optional[Sequence[Mapping[str, str]]] = None,
        secrets: Sequence[str] = (),
    ) -> None:
        from playwright.sync_api import sync_playwright

        self.origin = origin
        self.allow_writes = allow_writes
        self.secrets = [v for v in secrets if v]
        self.blocked: list[str] = []
        self.downloads: list[Any] = []
        #: What may leave the browser (the shared wire check): no writes on the
        #: read lane; on the write lane only the contract's writes, and on a
        #: compiled run exactly the taught ones, in order and shape. Sign-in
        #: values never travel in an address.
        self.wire = WireCheck(
            origin=origin,
            secrets=self.secrets,
            allowed=frozenset(allowed_writes) if allow_writes else None,
            pinned=(
                [PinnedWrite(str(p.get("write")), str(p.get("shape"))) for p in pinned]
                if pinned is not None
                else None
            ),
            refused=self.blocked,
        )
        #: Write requests sent to the origin, and their shapes (write lane).
        self.writes_sent = self.wire.sent
        self.write_shapes = self.wire.shapes
        self.dialogs: list[str] = []
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=headless)
        except Exception as exc:
            self._pw.stop()
            raise BridgeError(
                "could not start Chromium; run `python -m playwright install "
                f"chromium` and try again ({type(exc).__name__})"
            ) from None
        self.context = self._browser.new_context(accept_downloads=True)
        self.context.route("**/*", self._guard)
        self.page = self.context.new_page()
        self.page.set_default_timeout(_STEP_TIMEOUT_MS)
        self.page.on("download", lambda d: self.downloads.append(d))
        self.page.on("dialog", self._dialog)
        downloads_dir.mkdir(parents=True, exist_ok=True)

    def _dialog(self, dialog: Any) -> None:
        # A confirm() in a taught write is part of the write: accept it, and
        # keep its text with the step. The read lane never confirms.
        self.dialogs.append(f"{dialog.type}: {dialog.message[:200]}")
        try:
            dialog.accept() if self.allow_writes else dialog.dismiss()
        except Exception:  # noqa: BLE001 - already handled
            pass

    def _guard(self, route: Any) -> None:
        request = route.request
        if (
            request.method.upper() in READ_METHODS
            and request.is_navigation_request()
            and request.frame == self.page.main_frame
            and _origin(request.url) != self.origin
            and not carries_secret(request.url, self.secrets)
        ):
            self.blocked.append(f"leaving {self.origin} for {_origin(request.url)}")
            route.abort()
            return
        self.wire.handle(route)

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=_STEP_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - an aborted or download navigation
            pass

    def close(self) -> None:
        for closer in (self.context.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


# -- the downloaded file and its checks --------------------------------------------------


def _read_csv(path: Path) -> Optional[tuple[list[str], list[list[str]]]]:
    if path.suffix.lower() not in (".csv", ".tsv"):
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    rows = list(
        csv.reader(io.StringIO(text), delimiter="\t" if path.suffix == ".tsv" else ",")
    )
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _as_number(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _within(value: str, low: str, high: str) -> bool:
    nums = [_as_number(v) for v in (value, low, high)]
    if all(n is not None for n in nums):
        return nums[1] <= nums[0] <= nums[2]  # type: ignore[operator]
    return low <= value <= high


def _check_file(
    path: Path,
    program: Mapping[str, Any],
    inputs: Mapping[str, str],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """What passed, what failed, and facts about the file."""

    passed: list[str] = []
    failed: list[str] = []
    size = path.stat().st_size
    facts: dict[str, Any] = {
        "bytes": size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    if size == 0:
        failed.append("the downloaded file is empty")
        return passed, failed, facts
    result = program.get("result") or {}
    expected_suffix = result.get("suffix")
    if expected_suffix and path.suffix.lower() != expected_suffix:
        failed.append(f"the file is {path.suffix or 'untyped'}, not {expected_suffix}")
    table = _read_csv(path)
    if result.get("columns") is not None:
        if table is None:
            failed.append("the file is not a readable CSV")
            return passed, failed, facts
        header, rows = table
        facts.update(rows=len(rows), columns=header)
        if header == result["columns"]:
            passed.append("its columns match the taught file")
        else:
            failed.append(
                f"its columns are {header}, not {result['columns']} as taught"
            )
            return passed, failed, facts
        checks = program.get("checks") or {}
        min_rows = checks.get("min_rows")
        if min_rows is not None:
            if len(rows) >= int(min_rows):
                passed.append(f"it has {len(rows)} rows, at least {min_rows}")
            else:
                failed.append(f"it has {len(rows)} rows, fewer than {min_rows}")
        for rule in checks.get("between") or []:
            col = header.index(rule["column"])
            low, high = inputs[rule["from"]], inputs[rule["to"]]
            outside = [
                row[col]
                for row in rows
                if len(row) <= col or not _within(row[col], low, high)
            ]
            if outside:
                failed.append(
                    f"{len(outside)} rows have {rule['column']} outside {low} to "
                    f"{high} (first: {outside[0]!r})"
                )
            else:
                passed.append(
                    f"every {rule['column']} is from {low} to {high} ({len(rows)} rows)"
                )
        for rule in checks.get("equals") or []:
            col = header.index(rule["column"])
            want = inputs[rule["input"]]
            other = [row[col] for row in rows if len(row) <= col or row[col] != want]
            if other:
                failed.append(
                    f"{len(other)} rows have {rule['column']} other than {want!r} "
                    f"(first: {other[0]!r})"
                )
            else:
                passed.append(f"every {rule['column']} is {want} ({len(rows)} rows)")
    return passed, failed, facts


# -- run 1 and compiled runs ---------------------------------------------------------------


@dataclass
class _Teaching:
    name: str
    task: str
    url: str
    inputs: dict[str, str]
    files_dir: Path
    thread: _BrowserThread
    #: The write contract a taught write is verified against, by name.
    contract: Optional[str] = None
    pending: Any = None
    secret_values: dict[str, str] = field(default_factory=dict)
    secret_typed: bool = False
    browser: Optional[_Browser] = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    file: Optional[Path] = None
    download_step: Optional[int] = None
    ended: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _save_download(download: Any, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    failure = download.failure()
    if failure:
        raise StepStopped(f"the download failed ({failure})")
    name = Path(download.suggested_filename or "download").name or "download"
    path = folder / f"{_now()}-{name}"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{_now()}-{n}-{name}"
    download.save_as(str(path))
    return path


@dataclass
class _StepsRun:
    """What running compiled steps did."""

    file: Optional[Path] = None
    stopped: Optional[str] = None
    writes_sent: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _run_steps(
    program: Mapping[str, Any],
    inputs: Mapping[str, str],
    files_dir: Path,
    headless: bool,
    *,
    allow_writes: bool = False,
    allowed_writes: Sequence[str] = (),
    pinned: Optional[Sequence[Mapping[str, str]]] = None,
    secrets: Sequence[str] = (),
) -> _StepsRun:
    """Run compiled steps in a fresh browser. No model is called.

    ``inputs`` holds every value the steps type, sign-in values included.
    """

    started = time.monotonic()
    out = _StepsRun()
    browser = _Browser(
        program["origin"],
        files_dir,
        headless,
        allow_writes=allow_writes,
        allowed_writes=allowed_writes,
        pinned=pinned,
        secrets=secrets,
    )
    page = browser.page
    try:
        page.goto(program["url"], wait_until="domcontentloaded")
        for i, step in enumerate(program["steps"], 1):
            if browser.blocked:
                raise StepStopped(
                    f"the page tried {browser.blocked[0]}; it was blocked"
                )
            target = step.get("target")
            value = step.get("value") or {}
            text = inputs[value["input"]] if "input" in value else value.get("text", "")
            try:
                if step["op"] == "goto":
                    page.goto(
                        program["origin"] + step["url"], wait_until="domcontentloaded"
                    )
                elif step["op"] == "fill":
                    _resolve(page, target, _APPEAR_MS).fill(text)
                elif step["op"] == "select":
                    el = _resolve(page, target, _APPEAR_MS)
                    try:
                        el.select_option(label=text, timeout=2000)
                    except Exception:  # noqa: BLE001 - the input names a value
                        el.select_option(value=text)
                elif step["op"] == "press":
                    if target:
                        _resolve(page, target, _APPEAR_MS).press(step["key"])
                    else:
                        page.keyboard.press(step["key"])
                elif step["op"] == "click":
                    el = _resolve(page, target, _APPEAR_MS)
                    if step.get("download"):
                        with page.expect_download(timeout=_STEP_TIMEOUT_MS) as info:
                            el.click()
                        out.file = _save_download(info.value, files_dir)
                    else:
                        el.click()
                else:
                    raise StepStopped(f"unknown step {step['op']!r}")
            except StepStopped as exc:
                raise StepStopped(f"step {i} ({step['op']}): {exc}") from None
            except Exception as exc:  # noqa: BLE001 - a Playwright timeout or error
                raise StepStopped(
                    f"step {i} ({step['op']} {target and target.get('label')!r}) "
                    f"failed: {type(exc).__name__}"
                ) from None
            browser.settle()
        if browser.blocked:
            raise StepStopped(f"the page tried {browser.blocked[0]}; it was blocked")
    except StepStopped as exc:
        out.stopped = str(exc)
    finally:
        out.writes_sent = list(browser.writes_sent)
        out.blocked = list(browser.blocked)
        out.dialogs = list(browser.dialogs)
        browser.close()
        out.seconds = round(time.monotonic() - started, 2)
    return out


def _replay(
    program: Mapping[str, Any],
    inputs: Mapping[str, str],
    files_dir: Path,
    headless: bool,
) -> dict[str, Any]:
    """Run a read task's compiled steps and check the file it downloads."""

    run = _run_steps(program, inputs, files_dir, headless)
    file = run.file
    if run.stopped is not None:
        return {
            "outcome": "stopped",
            "what_happened": f"Stopped before finishing: {run.stopped}. Nothing was downloaded."
            if file is None
            else f"Stopped after the download: {run.stopped}.",
            "model_calls": 0,
            "seconds": run.seconds,
        }
    if file is None:
        return {
            "outcome": "stopped",
            "what_happened": "The steps ran but no file downloaded.",
            "model_calls": 0,
            "seconds": run.seconds,
        }
    passed, failed, facts = _check_file(file, program, inputs)
    result = {
        "outcome": "needs_review" if failed else "done",
        "file": str(file),
        **facts,
        "checks_passed": passed,
        "model_calls": 0,
        "seconds": run.seconds,
    }
    if failed:
        result["checks_failed"] = failed
        result["what_happened"] = (
            "The file downloaded but failed its checks: "
            + "; ".join(failed)
            + ". Don't use it without a person looking at it."
        )
    else:
        result["what_happened"] = (
            f"Downloaded {file.name} and checked it: " + "; ".join(passed) + "."
        )
    return result


class BrowserTeachBridge:
    """The read-only teach tools and the taught tools, for :func:`build_server`.

    Duck-types :class:`~openadapt_flow.agent.teach.TeachBridge`: the MCP layer
    calls ``handles``, ``list_tool_specs``, ``hold_needed`` and ``dispatch``.
    No action here needs a person's approval, because none can change data.
    """

    @property
    def instructions(self) -> str:
        if not self.contracts:
            return INSTRUCTIONS
        return INSTRUCTIONS + WRITE_INSTRUCTIONS

    def __init__(
        self,
        tools_dir: Optional[Path] = None,
        files_dir: Optional[Path] = None,
        *,
        headless: bool = True,
        contracts: Optional[Mapping[str, Any]] = None,
        secrets: Optional[Mapping[str, str]] = None,
        verify_dir: Optional[Path] = None,
    ) -> None:
        """``contracts`` (name -> :class:`~openadapt_flow.verify.WriteContract`)
        turns on taught writes: a task that names one may write to the app,
        and every run of it is verified against it. ``secrets`` maps an input
        name to the environment variable that holds its value (a sign-in), so
        the value never reaches the agent, the program or a result."""

        self.tools_dir = Path(tools_dir or default_tools_dir()).expanduser().resolve()
        self.files_dir = Path(files_dir or default_files_dir()).expanduser().resolve()
        self.headless = headless
        self.contracts = dict(contracts or {})
        self.secrets = dict(secrets or {})
        for key in self.secrets:
            if not _NAME.fullmatch(key):
                raise ValueError(f"secret input names are a-z, 0-9 and _ ({key!r})")
        self.verify_dir = (
            Path(verify_dir or (self.tools_dir.parent / "verify"))
            .expanduser()
            .resolve()
        )
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self._teaching: Optional[_Teaching] = None
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        #: taught tool name -> compiled program
        self.programs: dict[str, dict[str, Any]] = {}
        for path in sorted(self.tools_dir.glob(f"*/{PROGRAM_FILE}")):
            try:
                program = json.loads(path.read_text())
                if program.get("schema") != PROGRAM_SCHEMA or not _NAME.fullmatch(
                    program.get("name", "")
                ):
                    raise ValueError("not a browser tool program")
            except (OSError, ValueError) as exc:
                print(f"openadapt-agent: skipped {path}: {exc}", file=sys.stderr)
                continue
            self.programs[TAUGHT_PREFIX + program["name"]] = program

    @property
    def taught(self) -> dict[str, str]:
        return {tool: p["name"] for tool, p in self.programs.items()}

    # -- tool surface ------------------------------------------------------------------

    def handles(self, name: str) -> bool:
        return name in BROWSER_TEACH_TOOLS or name in self.programs

    def hold_needed(self, arguments: Mapping[str, Any]) -> None:
        return None

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="teach_start",
                description=(
                    "Start teaching a browser task by doing it once. Ask the "
                    "person first: the URL, the task, which values change each "
                    "time, and what proves the result is right. OpenAdapt opens "
                    "a browser at url and records what you do with teach_act. "
                    "inputs maps a short name for each value that changes to "
                    "this run's value (for example start_date: 2026-01-01). "
                    "Read-only: the browser sends no POST, PUT, PATCH or DELETE, "
                    "and stays on url's site."
                ),
                input_schema=_obj(
                    {
                        "name": {
                            "type": "string",
                            "pattern": _NAME.pattern,
                            "description": "Tool name: taught_<name> (a-z, 0-9, _).",
                        },
                        "url": {
                            "type": "string",
                            "pattern": "^https?://",
                            "maxLength": 2000,
                        },
                        "task": {"type": "string", "minLength": 4, "maxLength": 500},
                        "inputs": {
                            "type": "object",
                            "maxProperties": 12,
                            "additionalProperties": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                        },
                        "replace": {
                            "type": "boolean",
                            "description": "Teach again over an existing tool of this name.",
                        },
                        **(
                            {
                                "contract": {
                                    "type": "string",
                                    "enum": sorted(self.contracts),
                                    "description": (
                                        "For a task that saves something: the write "
                                        "contract the save is verified against. Run 1 "
                                        "is a real write; use test records."
                                    ),
                                }
                            }
                            if self.contracts
                            else {}
                        ),
                    },
                    ["name", "url", "task", "inputs"],
                ),
                annotations=_BROWSES,
            ),
            ToolSpec(
                name="teach_observe",
                description=(
                    "Read the page as short lines: headings, text, table rows, "
                    "and every control with a ref such as [e3]. Refs change "
                    "after every action."
                ),
                input_schema=_obj({}, []),
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="teach_act",
                description=(
                    "Do one action in run 1 on a ref from the latest listing: "
                    "click, type (text, or input to type an input's value), "
                    "select (an option's text, or input), or press (key, such as "
                    "Enter). Type each changing value with input, so the tool "
                    "takes it as a parameter. Returns the page after the action "
                    "and any file it downloaded. A control that would save, "
                    "send or delete is refused."
                ),
                input_schema=_obj(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["click", "type", "select", "press", "goto"],
                        },
                        "ref": {"type": "string", "pattern": "^e[0-9]{1,4}$"},
                        "text": {"type": "string", "maxLength": 2000},
                        "input": {"type": "string", "pattern": _NAME.pattern},
                        "key": {"type": "string", "maxLength": 32},
                        "url": {
                            "type": "string",
                            "maxLength": 2000,
                            "description": "goto: a page on the same site.",
                        },
                    },
                    ["action"],
                ),
                annotations=_BROWSES,
            ),
            ToolSpec(
                name="teach_finish",
                description=(
                    "End run 1 after the file downloaded. OpenAdapt compiles the "
                    "steps, checks the file, runs the steps once more with no "
                    "model to show they reproduce it, and adds the taught_<name> "
                    "tool. checks (ask the person): between, a column whose "
                    "values must lie from one input to another (dates as "
                    "YYYY-MM-DD, or numbers); equals, a column whose every "
                    "value must be an input's value; min_rows, the fewest rows "
                    "a right file has. The file's columns are always checked."
                ),
                input_schema=_obj(
                    {
                        "checks": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "between": {
                                    "type": "array",
                                    "maxItems": 5,
                                    "items": _obj(
                                        {
                                            "column": {
                                                "type": "string",
                                                "maxLength": 100,
                                            },
                                            "from": {
                                                "type": "string",
                                                "pattern": _NAME.pattern,
                                            },
                                            "to": {
                                                "type": "string",
                                                "pattern": _NAME.pattern,
                                            },
                                        },
                                        ["column", "from", "to"],
                                    ),
                                },
                                "equals": {
                                    "type": "array",
                                    "maxItems": 5,
                                    "items": _obj(
                                        {
                                            "column": {
                                                "type": "string",
                                                "maxLength": 100,
                                            },
                                            "input": {
                                                "type": "string",
                                                "pattern": _NAME.pattern,
                                            },
                                        },
                                        ["column", "input"],
                                    ),
                                },
                                "min_rows": {"type": "integer", "minimum": 0},
                            },
                        }
                    },
                    [],
                ),
                annotations=_BROWSES,
            ),
            ToolSpec(
                name="teach_abort",
                description="Stop run 1 now. Nothing is compiled.",
                input_schema=_obj({}, []),
                annotations=_LOCAL,
            ),
            ToolSpec(
                name="teach_status",
                description="The tools taught so far, and run 1 if one is going.",
                input_schema=_obj({}, []),
                annotations=_READ_ONLY,
            ),
        ]
        for tool, program in sorted(self.programs.items()):
            specs.append(self._taught_spec(tool, program))
        return specs

    def _taught_spec(self, tool: str, program: Mapping[str, Any]) -> ToolSpec:
        examples = program.get("examples") or {}
        return ToolSpec(
            name=tool,
            description=(
                (
                    f"Taught write: {program['task']} Makes the write on "
                    f"{program['origin']}, then reads the system of record and "
                    "returns outcome done with a verified receipt, or a refusal "
                    "(needs_review): never retry a refused write. "
                    if (program.get("result") or {}).get("kind") == "write"
                    else f"Taught task: {program['task']} Downloads a file from "
                    f"{program['origin']} and returns its path after checking it. "
                )
                + "Runs the recorded steps in a browser with no model calls; it "
                "stops instead of guessing when the page changed."
            ),
            input_schema=_obj(
                {
                    "inputs": _obj(
                        {
                            key: {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                                **(
                                    {"description": f"for example {examples[key]}"}
                                    if key in examples
                                    else {}
                                ),
                            }
                            for key in program["inputs"]
                        },
                        list(program["inputs"]),
                    )
                },
                ["inputs"],
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )

    # -- dispatch ----------------------------------------------------------------------

    def dispatch(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        held_by_person: Optional[str] = None,
        person: Optional[str] = None,
    ) -> dict[str, Any]:
        secrets = self._known_secrets()
        try:
            return _mask_all(self._dispatch(name, arguments), secrets)
        except BridgeError as exc:
            raise BridgeError(_mask(str(exc), secrets)) from None

    def _known_secrets(self) -> list[str]:
        import os

        values = [os.environ.get(env, "") for env in self.secrets.values()]
        run = getattr(self, "_teaching", None)
        if run is not None:
            values.extend(run.secret_values.values())
        return [v for v in values if v]

    def _dispatch(
        self, name: str, arguments: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        if name in self.programs:
            return self._run_taught(name, args)
        handlers = {
            "teach_start": self._start,
            "teach_observe": lambda a: self._observe(),
            "teach_act": self._act,
            "teach_finish": self._finish,
            "teach_abort": lambda a: self._abort(),
            "teach_status": lambda a: self._status(),
        }
        if name not in handlers:
            raise BridgeError("unknown tool name")
        return handlers[name](args)

    def close(self) -> None:
        with self._lock:
            if self._teaching is not None:
                self._end("closed")

    # -- run 1 -----------------------------------------------------------------------------

    def _active(self) -> _Teaching:
        run = self._teaching
        if run is None or run.ended:
            raise BridgeError("no run 1 is going; call teach_start")
        return run

    def _start(self, args: Mapping[str, Any]) -> dict[str, Any]:
        name = str(args["name"])
        url = str(args["url"])
        inputs = {str(k): str(v) for k, v in (args.get("inputs") or {}).items()}
        for key in inputs:
            if not _NAME.fullmatch(key):
                raise BridgeError(f"input names are a-z, 0-9 and _ ({key!r})")
        values = list(inputs.values())
        if len(set(values)) != len(values):
            raise BridgeError(
                "give each input a different value in run 1, so each typed "
                "value maps to one input"
            )
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise BridgeError("url must be an http(s) address")
        tool = TAUGHT_PREFIX + name
        if tool in self.programs and not args.get("replace"):
            raise BridgeError(
                f"{tool} already exists; call it, or pass replace: true to teach it again"
            )
        contract_name = args.get("contract")
        contract = None
        if contract_name is not None:
            contract = self.contracts.get(str(contract_name))
            if contract is None:
                raise BridgeError(f"no write contract named {contract_name!r}")
        clash = sorted(set(inputs) & set(self.secrets))
        if clash:
            raise BridgeError(
                f"{clash[0]!r} is a sign-in value the server holds; leave it out of inputs"
            )
        secret_values = self._secret_values()
        if contract is not None and not contract.writes:
            raise BridgeError(
                f"the {contract_name} contract lists no writes, so a taught write "
                'can\'t be bounded: the operator adds writes: ["POST /path", ...] '
                "to it"
            )
        if contract is not None:
            from openadapt_flow.agent.verify_tools import contract_params

            missing = [
                p
                for p in contract_params(contract)
                if p not in inputs and p not in secret_values
            ]
            if missing:
                raise BridgeError(
                    f"the {contract_name} contract checks {', '.join(missing)}: "
                    "give each a value in inputs"
                )
        with self._lock:
            if self._teaching is not None and not self._teaching.ended:
                raise BridgeError(
                    "run 1 of another task is going; teach_finish or teach_abort it first"
                )
            thread = _BrowserThread()
            run = _Teaching(
                name=name,
                task=str(args["task"]),
                url=url,
                inputs=inputs,
                files_dir=self.files_dir / name,
                thread=thread,
                contract=str(contract_name) if contract is not None else None,
                secret_values=secret_values,
            )
            if contract is not None:
                # Run 1 is a real write: read the store first, so it can be
                # verified like every later run.
                from openadapt_flow.verify import BaselineUnreadable, begin_write

                try:
                    run.pending = begin_write(
                        contract, {**inputs, **secret_values}, self.verify_dir
                    )
                except BaselineUnreadable as exc:
                    thread.stop()
                    raise BridgeError(f"{exc}; nothing was done") from None

            def open_page() -> str:
                run.browser = _Browser(
                    _origin(url),
                    run.files_dir,
                    self.headless,
                    allow_writes=run.contract is not None,
                    allowed_writes=contract.writes if contract is not None else (),
                    secrets=list(secret_values.values()),
                )
                run.browser.page.goto(url, wait_until="domcontentloaded")
                return self._listing(run)

            try:
                listing = thread.call(open_page)
            except Exception as exc:
                thread.call(lambda: run.browser and run.browser.close())
                thread.stop()
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError(
                    f"could not open {url} ({type(exc).__name__}); is the app running?"
                ) from None
            self._teaching = run
        return {
            "status": "recording",
            "name": name,
            "inputs": sorted(inputs),
            "page": listing,
            "next": (
                "Do the task with teach_act on refs from page. Type each "
                "changing value with input: <name>. "
                + (
                    "This is a real write, checked against the "
                    f"{run.contract} contract: call teach_finish after the save."
                    if run.contract
                    else "Call teach_finish once the file has downloaded."
                )
            ),
            **({"sign_in_inputs": sorted(secret_values)} if secret_values else {}),
        }

    def _secret_values(self) -> dict[str, str]:
        """The sign-in values the server holds, read from the environment."""

        import os

        values = {}
        for key, env in self.secrets.items():
            value = os.environ.get(env, "")
            if not value:
                raise BridgeError(
                    f"the sign-in value {key!r} is not set (environment variable {env})"
                )
            values[key] = value
        return values

    def _listing(self, run: _Teaching) -> str:
        assert run.browser is not None
        raw = _observe_page(run.browser.page, run.browser.origin)
        if run.secret_typed:
            # Once a sign-in value is typed, no address is shown with its query.
            raw["url"] = str(raw.get("url", "")).split("?", 1)[0]
        text, run.controls = _render(raw)
        # A sign-in value never reaches the agent, in any encoding.
        secrets = list(run.secret_values.values())
        text = _mask(text, secrets)
        for c in run.controls:
            if c.get("value") and carries_secret(str(c["value"]), secrets):
                c["value"] = _MASK
        return text

    def _observe(self) -> dict[str, Any]:
        run = self._active()
        return {"page": run.thread.call(lambda: self._listing(run))}

    def _act(self, args: Mapping[str, Any]) -> dict[str, Any]:
        run = self._active()
        action = args["action"]
        with self._lock:
            return run.thread.call(lambda: self._act_on_thread(run, action, args))

    def _value(
        self, run: _Teaching, args: Mapping[str, Any]
    ) -> tuple[str, dict[str, str]]:
        key = args.get("input")
        if key and key in run.secret_values:
            return run.secret_values[key], {"input": key, "secret": "true"}
        if key:
            if key not in run.inputs:
                raise BridgeError(
                    f"no input named {key!r}; inputs: {sorted(run.inputs)}"
                    + (
                        f", sign-in: {sorted(run.secret_values)}"
                        if run.secret_values
                        else ""
                    )
                )
            return run.inputs[key], {"input": key}
        if "text" not in args:
            raise BridgeError("type and select need text or input")
        text = str(args["text"])
        for name, value in run.inputs.items():
            if text == value:
                return text, {"input": name}
        return text, {"text": text}

    def _control(self, run: _Teaching, ref: Optional[str]) -> dict[str, Any]:
        if not ref:
            raise BridgeError("this action needs a ref from teach_observe")
        i = int(ref[1:]) - 1
        if not 0 <= i < len(run.controls):
            raise BridgeError(f"{ref} is not in the latest listing; call teach_observe")
        return run.controls[i]

    def _refuse_write(self, c: Mapping[str, Any], how: str) -> None:
        run = getattr(self, "_teaching", None)
        if run is not None and run.contract is not None:
            return  # a taught write: its save is the point, and it is verified
        if c.get("method") == "POST":
            raise BridgeError(
                f"refused: {how} {c['label']!r} would send a form by POST, which "
                "can change data. This lane only reads; nothing was done."
            )
        if c["role"] in ("button", "link") and _WRITE_LABEL.search(c.get("label", "")):
            raise BridgeError(
                f"refused: {c['label']!r} looks like it changes data. This lane "
                "only reads; nothing was done."
            )

    def _act_on_thread(
        self, run: _Teaching, action: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        browser = run.browser
        assert browser is not None
        page = browser.page
        if run.file is not None:
            raise BridgeError("the file has downloaded; call teach_finish")
        step: dict[str, Any] = {"op": "fill" if action == "type" else action}
        c: Optional[dict[str, Any]] = None
        if action == "goto":
            url = str(args.get("url") or "")
            target_url = urljoin(page.url, url)
            if _origin(target_url) != browser.origin:
                raise BridgeError(
                    f"goto stays on {browser.origin}; {url!r} is another site"
                )
            parts = urlsplit(target_url)
            step["url"] = parts.path + (f"?{parts.query}" if parts.query else "")
            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 - reported to the agent
                raise BridgeError(
                    f"could not open {url!r} ({type(exc).__name__})"
                ) from None
            run.steps.append(step)
            return {"page": self._listing(run)}
        if action == "press":
            key = str(args.get("key") or "")
            if not key:
                raise BridgeError("press needs key")
            if args.get("ref"):
                c = self._control(run, args.get("ref"))
            else:
                focused = page.evaluate(_FOCUSED_JS)
                if focused is not None:
                    match = [
                        x
                        for x in run.controls
                        if x["tag"] == focused["tag"]
                        and x["id"] == focused["id"]
                        and x["name_attr"] == focused["name_attr"]
                    ]
                    c = match[0] if len(match) == 1 else None
                    if (
                        c is None
                        and key == "Enter"
                        and focused["method"] == "POST"
                        and run.contract is None
                    ):
                        raise BridgeError(
                            "refused: Enter here would send a form by POST. This "
                            "lane only reads; nothing was done."
                        )
            if c is not None and key in ("Enter", " "):
                self._refuse_write(c, f"pressing {key} on")
            step["key"] = key
        else:
            c = self._control(run, args.get("ref"))
            if action == "click":
                if c["role"] == "textbox" and c["tag"] in ("input", "textarea"):
                    pass  # a click on a field only focuses it
                else:
                    self._refuse_write(c, "clicking")
            elif action == "type":
                if c["role"] not in ("textbox",):
                    raise BridgeError(
                        f"{args['ref']} is a {c['role']}, not a text field"
                    )
                text, step["value"] = self._value(run, args)
                if step["value"].get("secret") and not (
                    c.get("type") == "password"
                    or (c.get("method") == "POST" and c.get("sign_in"))
                ):
                    raise BridgeError(
                        "refused: a sign-in value goes only into a password field "
                        "or another field of a sign-in form that posts; anywhere "
                        "else it could reach an address, a record or the page. "
                        "Nothing was typed."
                    )
                if step["value"].get("secret"):
                    run.secret_typed = True
            elif action == "select":
                if c["tag"] != "select":
                    raise BridgeError(f"{args['ref']} is not a drop-down")
                text, step["value"] = self._value(run, args)
        target = None
        if c is not None:
            target = _unique_target(page, c)
            if target is None:
                raise BridgeError(
                    f"refused: {c['label']!r} can't be told apart from another "
                    "control on this page, so a later run could act on the wrong "
                    "one. Try another way to the same result."
                )
            step["target"] = target
        before = len(browser.downloads)
        try:
            # The same resolution a compiled run uses, so what works here
            # works there.
            el = _resolve(page, target, 2000) if target is not None else None
            if action == "click":
                el.click()
            elif action == "type":
                el.fill(text)
            elif action == "select":
                try:
                    el.select_option(label=text, timeout=2000)
                except Exception:  # noqa: BLE001 - the text is an option value
                    el.select_option(value=text, timeout=2000)
            elif action == "press":
                if el is not None:
                    el.press(step["key"])
                else:
                    page.keyboard.press(step["key"])
        except StepStopped as exc:
            raise BridgeError(f"refused: {exc}") from None
        except Exception as exc:  # noqa: BLE001 - reported to the agent
            detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
            raise BridgeError(
                f"the {action} did not work ({type(exc).__name__}: {detail}); "
                "call teach_observe"
            ) from None
        browser.settle()
        if action in ("click", "press"):
            # An attachment arrives as a download event, a moment after the
            # response headers.
            for _ in range(10):
                if len(browser.downloads) > before:
                    break
                page.wait_for_timeout(100)
        if browser.blocked:
            what = browser.blocked[0]
            # On the browser thread already: close here, not through _end,
            # which would wait on this same thread.
            run.ended = "blocked"
            browser.close()
            run.thread.stop()
            raise BridgeError(
                f"run 1 ended: the page tried {what}; it was not sent. Nothing "
                "was compiled. Start again and avoid that control."
            )
        run.steps.append(step)
        result: dict[str, Any] = {}
        if len(browser.downloads) > before:
            try:
                run.file = _save_download(browser.downloads[-1], run.files_dir)
            except StepStopped as exc:
                raise BridgeError(str(exc)) from None
            step["download"] = True
            run.download_step = len(run.steps) - 1
            result["downloaded"] = str(run.file)
            result["next"] = "The file downloaded. Call teach_finish with the checks."
            table = _read_csv(run.file)
            if table is not None:
                result["columns"] = table[0]
                result["rows"] = len(table[1])
        result["page"] = self._listing(run)
        return result

    def _end(self, why: str) -> None:
        run = self._teaching
        if run is None or run.ended:
            return
        run.ended = why
        try:
            run.thread.call(lambda: run.browser and run.browser.close(), timeout=30)
        finally:
            run.thread.stop()

    def _abort(self) -> dict[str, Any]:
        with self._lock:
            run = self._active()
            self._end("aborted")
        return {"status": "aborted", "name": run.name}

    def _status(self) -> dict[str, Any]:
        run = self._teaching
        return {
            "tools": [
                {"tool": tool, "task": p["task"], "inputs": p["inputs"]}
                for tool, p in sorted(self.programs.items())
            ],
            "run_1": (
                None
                if run is None or run.ended
                else {
                    "name": run.name,
                    "steps": len(run.steps),
                    "downloaded": bool(run.file),
                }
            ),
        }

    def _finish_write(self) -> dict[str, Any]:
        """End a taught write: verify run 1's write, then add the tool."""

        from openadapt_flow.verify import verify_write

        with self._lock:
            run = self._active()
            assert run.browser is not None
            writes = list(run.browser.writes_sent)
            shapes = list(run.browser.write_shapes)
            if not writes:
                raise BridgeError(
                    "run 1 has sent no write yet; finish the task (save), or teach_abort"
                )
            used = {
                st["value"]["input"]
                for st in run.steps
                if "input" in (st.get("value") or {})
            }
            unused = sorted(set(run.inputs) - used)
            if unused:
                raise BridgeError(
                    f"input {unused[0]!r} was never typed or selected, so a new "
                    "value would change nothing. Type it with input, or "
                    "teach_abort and start without it."
                )
            steps = list(run.steps)
            self._end("finished")
        result = verify_write(
            run.pending, executor={"kind": "agent", "id": "teach-run-1"}
        )
        if not result.verified:
            return {
                "status": "not_added",
                "what_happened": (
                    f"Run 1's write was not verified ({result.verdict}: "
                    f"{getattr(result, 'reason', '')}). No tool was added. A person "
                    "checks the record before anything else is written; don't "
                    "teach it again until they have."
                ),
                "verification": result.to_dict(),
            }
        program = {
            "schema": PROGRAM_SCHEMA,
            "name": run.name,
            "task": run.task,
            "url": run.url,
            "origin": _origin(run.url),
            "inputs": sorted(run.inputs),
            "examples": dict(run.inputs),
            "sign_in": sorted(
                st["value"]["input"]
                for st in steps
                if (st.get("value") or {}).get("secret")
            ),
            "steps": steps,
            # Exactly the writes run 1 sent, in order, by method, path and the
            # names of the fields each carried. A compiled run sends these and
            # nothing else.
            "writes": [
                {"write": write, "shape": shape} for write, shape in zip(writes, shapes)
            ],
            "result": {"kind": "write", "contract": run.contract},
            "taught_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        folder = self.tools_dir / run.name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / PROGRAM_FILE).write_text(json.dumps(program, indent=2) + "\n")
        tool = TAUGHT_PREFIX + run.name
        self.programs[tool] = program
        return {
            "status": "ready",
            "new_tool": tool,
            "inputs": program["inputs"],
            "steps": len(steps),
            "run_1": result.to_dict(),
            "writes_sent": writes,
            "program": str(folder / PROGRAM_FILE),
            "next": (
                f"Call {tool} with new inputs. Each call makes the write and "
                f"returns a verified receipt from the {run.contract} contract, or a "
                "refusal: never retry a refused write."
            ),
        }

    def _finish(self, args: Mapping[str, Any]) -> dict[str, Any]:
        run = self._active()
        if run.contract is not None:
            return self._finish_write()
        with self._lock:
            run = self._active()
            if run.file is None or run.download_step is None:
                raise BridgeError(
                    "no file has downloaded in run 1 yet; finish the task, or "
                    "teach_abort. This lane teaches tasks that end in a download."
                )
            steps = run.steps[: run.download_step + 1]
            used = {
                s["value"]["input"] for s in steps if "input" in (s.get("value") or {})
            }
            unused = sorted(set(run.inputs) - used)
            if unused:
                raise BridgeError(
                    f"input {unused[0]!r} was never typed or selected, so a new "
                    "value would change nothing. Type it with input, or "
                    "teach_abort and start without it."
                )
            checks = dict(args.get("checks") or {})
            table = _read_csv(run.file)
            rules = [*(checks.get("between") or []), *(checks.get("equals") or [])]
            for rule in rules:
                for key in (rule.get("from"), rule.get("to"), rule.get("input")):
                    if key is not None and key not in run.inputs:
                        raise BridgeError(
                            f"check names input {key!r}, which run 1 lacks"
                        )
                if table is None or rule["column"] not in table[0]:
                    raise BridgeError(
                        f"the file has no column {rule['column']!r}"
                        + (f"; its columns: {table[0]}" if table else "")
                    )
            program = {
                "schema": PROGRAM_SCHEMA,
                "name": run.name,
                "task": run.task,
                "url": run.url,
                "origin": _origin(run.url),
                "inputs": sorted(run.inputs),
                "examples": dict(run.inputs),
                "steps": steps,
                "result": {
                    "kind": "download",
                    "suffix": run.file.suffix.lower(),
                    "columns": table[0] if table is not None else None,
                },
                "checks": checks,
                "taught_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            passed, failed, facts = _check_file(run.file, program, run.inputs)
            if failed:
                raise BridgeError(
                    "the file run 1 downloaded fails these checks: "
                    + "; ".join(failed)
                    + ". Fix the checks or the task; run 1 is still open."
                )
            self._end("finished")
        # Show that the compiled steps alone reproduce the file.
        replay = self._replay(program, run.inputs)
        if replay["outcome"] != "done":
            return {
                "status": "not_added",
                "what_happened": (
                    "Run 1 worked, but running its compiled steps again did not: "
                    + replay["what_happened"]
                    + " No tool was added."
                ),
                "replay": replay,
            }
        folder = self.tools_dir / run.name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / PROGRAM_FILE).write_text(json.dumps(program, indent=2) + "\n")
        tool = TAUGHT_PREFIX + run.name
        self.programs[tool] = program
        return {
            "status": "ready",
            "new_tool": tool,
            "inputs": program["inputs"],
            "steps": len(steps),
            "run_1_file": str(run.file),
            "checks_passed": passed,
            "replayed": {
                "file": replay["file"],
                "same_bytes_as_run_1": replay["sha256"] == facts["sha256"],
                "model_calls": 0,
                "seconds": replay["seconds"],
            },
            "program": str(folder / PROGRAM_FILE),
            "next": f"Call {tool} with new inputs.",
        }

    # -- compiled runs ------------------------------------------------------------------------

    def _replay(
        self, program: Mapping[str, Any], inputs: Mapping[str, str]
    ) -> dict[str, Any]:
        thread = _BrowserThread()
        try:
            with self._run_lock:
                return thread.call(
                    lambda: _replay(
                        program, inputs, self.files_dir / program["name"], self.headless
                    ),
                    timeout=600,
                )
        except BridgeError as exc:
            return {
                "outcome": "did_not_run",
                "what_happened": str(exc),
                "model_calls": 0,
            }
        finally:
            thread.stop()

    def _run_write(
        self, tool: str, program: Mapping[str, Any], inputs: Mapping[str, str]
    ) -> dict[str, Any]:
        """Make a taught write and verify it. Only a verified write is done."""

        from openadapt_flow.verify import BaselineUnreadable, begin_write, verify_write

        name = program["result"]["contract"]
        contract = self.contracts.get(name)
        if contract is None:
            return {
                "outcome": "did_not_run",
                "what_happened": f"this server doesn't check the {name} contract; nothing was written",
                "model_calls": 0,
            }
        try:
            secret_values = self._secret_values()
        except BridgeError as exc:
            return {
                "outcome": "did_not_run",
                "what_happened": f"{exc}; nothing was written",
                "model_calls": 0,
            }
        values = {**inputs, **secret_values}
        try:
            pending = begin_write(contract, values, self.verify_dir)
        except BaselineUnreadable as exc:
            return {
                "outcome": "did_not_run",
                "what_happened": f"{exc}; nothing was written",
                "model_calls": 0,
            }
        thread = _BrowserThread()
        try:
            with self._run_lock:
                run = thread.call(
                    lambda: _run_steps(
                        program,
                        values,
                        self.files_dir / program["name"],
                        self.headless,
                        allow_writes=True,
                        allowed_writes=contract.writes,
                        pinned=program.get("writes") or [],
                        secrets=list(secret_values.values()),
                    ),
                    timeout=600,
                )
        except BridgeError as exc:
            return {
                "outcome": "did_not_run",
                "what_happened": str(exc),
                "model_calls": 0,
            }
        finally:
            thread.stop()
        base = {
            "model_calls": 0,
            "seconds": run.seconds,
            "writes_sent": len(run.writes_sent),
        }
        if run.stopped is not None and not run.writes_sent:
            return {
                **base,
                "outcome": "stopped",
                "what_happened": f"Stopped before any write was sent: {run.stopped}.",
            }
        result = verify_write(
            pending,
            executor={"kind": "program", "id": tool},
            write_attempted=bool(run.writes_sent),
            declared_by="agent (tool inputs)",
        )
        stopped = f" The steps stopped: {run.stopped}." if run.stopped else ""
        if run.blocked:
            # A write outside the taught set was stopped before it was sent:
            # never a success, whatever the record shows.
            return {
                **base,
                **result.to_dict(),
                "verified": False,
                "outcome": "needs_review",
                "what_happened": (
                    f"The page tried {run.blocked[0]}; it was not sent. The taught "
                    "write's own check is in verdict. A person checks the record "
                    "and the app; don't retry."
                ),
            }
        if result.verified:
            return {
                **base,
                **result.to_dict(),
                "outcome": "done",
                "what_happened": (
                    f"The write is in the system of record as the {name} contract "
                    f"declares; receipt {result.receipt_sha256[:12]}.{stopped}"
                ),
            }
        return {
            **base,
            **result.to_dict(),
            "outcome": "needs_review",
            "what_happened": (
                f"The write was not verified ({result.verdict}: {result.reason}).{stopped} "
                "Don't retry it: a person checks the record."
            ),
        }

    def _run_taught(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        program = self.programs[tool]
        inputs = {str(k): str(v) for k, v in (args.get("inputs") or {}).items()}
        if sorted(inputs) != program["inputs"]:
            return {
                "outcome": "did_not_run",
                "what_happened": f"{tool} takes exactly these inputs: {program['inputs']}",
                "model_calls": 0,
            }
        if (program.get("result") or {}).get("kind") == "write":
            return self._run_write(tool, program, inputs)
        return self._replay(program, inputs)
