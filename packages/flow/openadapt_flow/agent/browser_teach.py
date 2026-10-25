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
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.agent.bridge import BridgeError, ToolSpec

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
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
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


# -- targets -------------------------------------------------------------------------


def _css_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _locator(page: Any, target: Mapping[str, Any]) -> Any:
    if "css" in target:
        return page.locator(target["css"])
    if "role" in target:
        return page.get_by_role(target["role"], name=target["name"], exact=True)
    return page.get_by_label(target["label_for"], exact=True)


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

    for cand in _candidates(c):
        try:
            if _locator(page, cand).count() == 1:
                return {**cand, "label": c["label"], "role": c["role"]}
        except Exception:  # noqa: BLE001 - a selector the page can't parse
            continue
    if c["role"] == "link" and c.get("label"):
        # Links with the same label and the same address go to the same
        # place (a menu and a body link): any of them will do.
        cand = {"role": "link", "name": c["label"]}
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

    def __init__(self, origin: str, downloads_dir: Path, headless: bool) -> None:
        from playwright.sync_api import sync_playwright

        self.origin = origin
        self.blocked: list[str] = []
        self.downloads: list[Any] = []
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
        downloads_dir.mkdir(parents=True, exist_ok=True)

    def _guard(self, route: Any) -> None:
        request = route.request
        method = request.method.upper()
        if method not in _READ_METHODS:
            self.blocked.append(f"{method} {urlsplit(request.url).path or '/'}")
            route.abort()
            return
        if (
            request.is_navigation_request()
            and request.frame == self.page.main_frame
            and _origin(request.url) != self.origin
        ):
            self.blocked.append(f"leaving {self.origin} for {_origin(request.url)}")
            route.abort()
            return
        route.continue_()

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


def _replay(
    program: Mapping[str, Any],
    inputs: Mapping[str, str],
    files_dir: Path,
    headless: bool,
) -> dict[str, Any]:
    """Run compiled steps in a fresh browser. No model is called."""

    started = time.monotonic()
    browser = _Browser(program["origin"], files_dir, headless)
    page = browser.page
    file: Optional[Path] = None
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
                if step["op"] == "fill":
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
                        file = _save_download(info.value, files_dir)
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
        return {
            "outcome": "stopped",
            "what_happened": f"Stopped before finishing: {exc}. Nothing was downloaded."
            if file is None
            else f"Stopped after the download: {exc}.",
            "model_calls": 0,
            "seconds": round(time.monotonic() - started, 2),
        }
    finally:
        browser.close()
    if file is None:
        return {
            "outcome": "stopped",
            "what_happened": "The steps ran but no file downloaded.",
            "model_calls": 0,
            "seconds": round(time.monotonic() - started, 2),
        }
    passed, failed, facts = _check_file(file, program, inputs)
    result = {
        "outcome": "needs_review" if failed else "done",
        "file": str(file),
        **facts,
        "checks_passed": passed,
        "model_calls": 0,
        "seconds": round(time.monotonic() - started, 2),
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

    instructions = INSTRUCTIONS

    def __init__(
        self,
        tools_dir: Optional[Path] = None,
        files_dir: Optional[Path] = None,
        *,
        headless: bool = True,
    ) -> None:
        self.tools_dir = Path(tools_dir or default_tools_dir()).expanduser().resolve()
        self.files_dir = Path(files_dir or default_files_dir()).expanduser().resolve()
        self.headless = headless
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
                            "enum": ["click", "type", "select", "press"],
                        },
                        "ref": {"type": "string", "pattern": "^e[0-9]{1,4}$"},
                        "text": {"type": "string", "maxLength": 2000},
                        "input": {"type": "string", "pattern": _NAME.pattern},
                        "key": {"type": "string", "maxLength": 32},
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
                f"Taught task: {program['task']} Downloads a file from "
                f"{program['origin']} and returns its path after checking it. "
                "Runs the recorded steps in a browser with no model calls; it "
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
            )

            def open_page() -> str:
                run.browser = _Browser(_origin(url), run.files_dir, self.headless)
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
                "changing value with input: <name>. Call teach_finish once "
                "the file has downloaded."
            ),
        }

    def _listing(self, run: _Teaching) -> str:
        assert run.browser is not None
        raw = run.browser.page.evaluate(_OBSERVE_JS)
        text, run.controls = _render(raw)
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
        if key:
            if key not in run.inputs:
                raise BridgeError(
                    f"no input named {key!r}; inputs: {sorted(run.inputs)}"
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
                    if c is None and key == "Enter" and focused["method"] == "POST":
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
            self._end("blocked")
            raise BridgeError(
                f"run 1 ended: the page tried {what}, which this lane blocks. "
                "Nothing was compiled. Start again and avoid that control."
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

    def _finish(self, args: Mapping[str, Any]) -> dict[str, Any]:
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

    def _run_taught(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        program = self.programs[tool]
        inputs = {str(k): str(v) for k, v in (args.get("inputs") or {}).items()}
        if sorted(inputs) != program["inputs"]:
            return {
                "outcome": "did_not_run",
                "what_happened": f"{tool} takes exactly these inputs: {program['inputs']}",
                "model_calls": 0,
            }
        return self._replay(program, inputs)
