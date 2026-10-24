"""Two competent alternatives to OpenAdapt, as silent-saves executors.

Both get what OpenAdapt's runtime gets on MockMed: the page's controls (DOM
and accessibility view, not screenshots) and read access to the system of
record (``GET /api/db``, the same read ``--readback`` and the OpenAdapt
program use). Neither gets the fault.

- ``agent-tools`` (``python:silent_saves.agent_arms:AgentTools``): a model
  works every item through structured tools: ``read_screen``, ``click``,
  ``fill``, ``read_records`` and ``finish``. It is told to read the records
  before and after the save and to claim success only when exactly one new,
  correct row landed. That is the read-back check, done by the agent itself.
- ``agent-script`` (``python:silent_saves.agent_arms:AgentScript``): a coding
  agent writes a Playwright script from a demonstration, the store's API
  description and the live page, with the same checks (a duplicate guard
  before the save, a read-back after it), dry-runs it with the save blocked
  at the network layer, and submits it. Each item then runs the script with
  no model call. When the script breaks (an exception, or a reason that
  starts with ``UNEXPECTED``), the agent is called again with the script, the
  failure and the live page, dry-runs a fix, and the item is run again with
  the fix. Every authoring and repair session is a ``human_events`` entry
  (``script_review``) with 0 minutes logged: the benchmark never estimates
  minutes; ``benchmark/economics`` prices them with stated assumptions.

Model spend goes to the lifecycle comparison's ledger
(``lifecycle/results/spend.json``) under its $30 cap. Set
``AGENT_ARMS_MODEL`` to change the model (default Claude Sonnet 5.5, the
model of the scoreboard's computer-use arm on this suite).
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from urllib.request import urlopen

from .executors import BENCHMARK_DIR, Report, Task

if str(BENCHMARK_DIR) not in sys.path:
    sys.path.append(str(BENCHMARK_DIR))

from lifecycle.arms import budget  # noqa: E402

MODEL = os.environ.get("AGENT_ARMS_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("AGENT_ARMS_EFFORT", "medium")
SAVE_PATH = "/api/encounter"

READ_JS = r"""
() => {
  let n = 0;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = e => {
    if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l && l.innerText.trim()) return l.innerText.trim(); }
    return e.getAttribute('aria-label') || e.title || e.placeholder || '';
  };
  document.querySelectorAll('[data-ar]').forEach(e => e.removeAttribute('data-ar'));
  const out = {url_hash: location.hash, controls: [], buttons: []};
  document.querySelectorAll('input, select, textarea').forEach(e => {
    if (!vis(e) || e.type === 'hidden' || e.type === 'button' || e.type === 'submit') return;
    const ref = String(n++); e.setAttribute('data-ar', ref);
    out.controls.push({ref, id: e.id || e.name || undefined, label: labelOf(e),
      type: e.tagName === 'SELECT' ? 'select' : (e.type || e.tagName.toLowerCase()), value: e.value});
  });
  document.querySelectorAll('button, input[type=button], input[type=submit], a, [role=button], [role=tab]').forEach(e => {
    if (!vis(e)) return;
    const t = (e.innerText || e.value || e.title || e.getAttribute('aria-label') || '').trim(); if (!t) return;
    const ref = String(n++); e.setAttribute('data-ar', ref);
    const row = e.closest('tr, li');
    const b = {ref, text: t.slice(0, 60), id: e.id || undefined};
    if (row && row.innerText.trim() !== t) b.row = row.innerText.trim().replace(/\s+/g, ' ').slice(0, 120);
    out.buttons.push(b);
  });
  const main = document.querySelector('main') || document.body;
  out.text = main.innerText.replace(/\n{2,}/g, '\n').slice(0, 2000);
  return out;
}
"""

#: What every arm is told about the store. OpenAdapt's read-back reads the
#: same endpoint (``readback.VERIFY_YAML``).
API_DOC = """\
System of record (read only): GET {origin}/api/db returns
{{"records": [{{"id": int, "patient_id": "p1", "type": "Triage", "note": str, ...}}]}},
every saved encounter. patient_id is the patient's MRN in lower case (MRN P1 -> "p1")."""


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def read_records(origin: str) -> list[dict[str, Any]]:
    with urlopen(f"{origin}/api/db", timeout=10) as resp:  # noqa: S310 - loopback only
        data = json.loads(resp.read().decode() or "{}")
    return [
        {k: r.get(k) for k in ("id", "patient_id", "type", "note")}
        for r in data.get("records", [])
        if isinstance(r, dict)
    ]


class _Browser:
    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)

    def page(self, url: str, *, block_save: bool = False) -> tuple[Any, Any, dict[str, int]]:
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 800})
        ctx.set_default_timeout(5000)
        blocked = {"n": 0}
        if block_save:

            def block(route: Any) -> None:
                if route.request.method == "POST":
                    blocked["n"] += 1
                    route.abort()
                else:
                    route.continue_()

            ctx.route(f"**{SAVE_PATH}*", block)
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        return ctx, page, blocked

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()


def _screen(page: Any) -> dict[str, Any]:
    page.wait_for_timeout(150)
    return page.evaluate(READ_JS)


def _call(client: Any, meter: budget.Meter, system: str, tools: list[dict[str, Any]],
          messages: list[dict[str, Any]], max_tokens: int = 8000) -> Any:
    approx_in = 2000 + sum(len(json.dumps(m, default=str)) for m in messages) // 3
    meter.before_call(MODEL, budget.max_call_usd(MODEL, approx_in, max_tokens))
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},
        messages=messages,
    )
    meter.record(MODEL, response.usage)
    return response


# -- agent-tools -----------------------------------------------------------------

TOOLS_SYSTEM = """\
You do clinic tasks in a web app through tools. You cannot see the screen; \
read_screen lists its controls, buttons (with the table row or list item each \
one sits in) and visible text. Refs are valid until the next read_screen. You \
also have read access to the system of record (read_records).

Rules:
- Before you save, read the records once, so you know what was there.
- Save at most once. Never retry a save.
- After the save, read the records again. Report saved=true only if exactly \
one new record landed, on the right patient, with the right type and exactly \
the note you were given, and no earlier record was removed or changed. \
Otherwise report saved=false and say what you saw.
- If the screen does not let you do exactly what was asked (the patient is \
not there, the type is not offered), do not guess: report saved=false.
Call finish exactly once."""

AGENT_TOOLS: list[dict[str, Any]] = [
    {"name": "read_screen", "description": "List the page's visible controls, buttons and text, with a ref for each control.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "click", "description": "Click a button or link by its ref.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"], "additionalProperties": False}},
    {"name": "fill", "description": "Set a text field or text area (by ref) to exactly this text.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["ref", "text"], "additionalProperties": False}},
    {"name": "read_records", "description": "Read every saved encounter from the system of record (GET /api/db).",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "finish", "description": "Report the outcome once, at the end.",
     "input_schema": {"type": "object", "properties": {"saved": {"type": "boolean"}, "summary": {"type": "string"}},
                      "required": ["saved", "summary"], "additionalProperties": False}},
]


class AgentTools:
    """A model with structured tools and record read-back, every item."""

    name = "agent-tools"
    paid = True

    def __init__(self, max_turns: int = 30, item_budget_usd: float = 0.40) -> None:
        self.max_turns = max_turns
        self.item_budget_usd = item_budget_usd
        self._b = _Browser()

    def run(self, task: Task) -> Report:
        meter = budget.Meter(f"silent-saves agent-tools item {task.item}", self.item_budget_usd)
        ctx, page, _ = self._b.page(task.url)
        origin = _origin(task.url)
        trace: list[dict[str, Any]] = []
        try:
            client = budget.client(MODEL)
            messages: list[dict[str, Any]] = [{"role": "user", "content": (
                f"Task: {task.instruction}\n\n" + API_DOC.format(origin="the app"))}]
            for turn in range(1, self.max_turns + 1):
                try:
                    response = _call(client, meter, TOOLS_SYSTEM, AGENT_TOOLS, messages, 4000)
                except budget.ItemBudgetExceeded as exc:
                    return self._report(False, f"item budget: {exc}", meter, trace)
                messages.append({"role": "assistant", "content": response.content})
                uses = [b for b in response.content if b.type == "tool_use"]
                trace.append({"turn": turn, "calls": [{"name": b.name, "input": b.input} for b in uses]})
                if not uses:
                    messages.append({"role": "user", "content": "Call finish to report the outcome."})
                    continue
                results = []
                for b in uses:
                    if b.name == "finish":
                        return self._report(bool(b.input.get("saved")), str(b.input.get("summary", "")), meter, trace)
                    try:
                        out = self._tool(page, origin, b.name, dict(b.input))
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": json.dumps(out, default=str)[:12000]})
                    except Exception as exc:  # noqa: BLE001 - the agent sees the error
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": f"Error: {exc}"[:400], "is_error": True})
                messages.append({"role": "user", "content": results})
            return self._report(False, f"no finish within {self.max_turns} turns", meter, trace)
        finally:
            (task.work_dir / "agent-trace.json").write_text(json.dumps(trace, default=str, indent=1))
            ctx.close()

    def _tool(self, page: Any, origin: str, name: str, args: dict[str, Any]) -> Any:
        if name == "read_screen":
            return _screen(page)
        if name == "read_records":
            return read_records(origin)
        loc = page.locator(f'[data-ar="{args["ref"]}"]')
        if name == "click":
            loc.click()
            page.wait_for_timeout(300)
            return "clicked; call read_screen to see the result"
        if name == "fill":
            loc.fill(args["text"])
            return "filled"
        raise ValueError(f"unknown tool {name}")

    def _report(self, saved: bool, text: str, meter: budget.Meter, trace: list[Any]) -> Report:
        return Report(saved, final_text=text[:2000], model_calls=meter.calls,
                      cost_usd=round(meter.usd, 6), extra={"turns": len(trace), "model": MODEL})

    def close(self) -> None:
        self._b.close()


# -- agent-script ------------------------------------------------------------------

DEMONSTRATION = """\
Demonstration (one recorded run, as a person would describe the recording):
Start: the app at its task list (#tasks), a table of referrals with columns
Patient, Reason, Priority, Action.
1. In the row for the patient (here Jane Sample), click its Open button
   (button#open-p1, class open-btn). The patient record opens; its banner reads
   "Jane Sample - MRN P1 - DOB 1980-01-01".
2. Click "New Encounter" (#new-encounter). The encounter form opens.
3. Click the type button "Triage" (#type-triage).
4. Fill the note text area (#note) with the note.
5. Click "Save Encounter" (#save-encounter). A banner (#saved-banner) reads
   "Encounter saved - <note>"; a failed save shows #save-error instead.
The save is a POST to /api/encounter."""

CONTRACT = """\
Write one Python module that defines

    def run(ctx, params: dict) -> dict

returning {"result": "SAVED" | "NOT SAVED", "reason": "<one sentence>"}.
params has "patient" (the patient's full name, e.g. "Jane Sample"), "type"
("Triage") and "note" (the exact note text).
ctx.page is a Playwright sync Page on the app's task list; ctx.records() reads
the system of record (a list of {"id", "patient_id", "type", "note"});
ctx.commit is False in a dry run; ctx.log(msg) records a line.
Only re, time and json may be imported; no file, OS, network or eval access.
Required behaviour, the same checks the other arms make:
- Find the patient's row by the patient's name, never by position. Confirm the
  record banner shows that name before acting, and take the MRN from it.
- Duplicate guard: before saving, if the records already hold a row for that
  patient with that type and note, return NOT SAVED ("DUPLICATE: ...").
- Save at most once.
- Read-back: after the save, read the records again. Return SAVED only if
  exactly one new row landed, for that patient (patient_id = MRN in lower
  case), with that type and exactly that note, and no earlier row was removed
  or changed; otherwise NOT SAVED with a reason starting "READBACK:".
- In a dry run (ctx.commit is False), do everything including the save click
  (the save request is blocked for you), skip the read-back and return
  {"result": "NOT SAVED", "reason": "DRY RUN reached save"}.
- When the screen is not what the script expects (a control is missing, a
  wait times out, the patient is not in the list), return NOT SAVED with a
  reason starting "UNEXPECTED:" rather than guessing.
Keep waits short (the page answers in under a second; use timeouts of at most
5 s)."""

SCRIPT_SYSTEM = """\
You are a careful automation engineer. You write and maintain a Playwright \
script for a clinic web app, against a local synthetic test copy. Use the tools \
to inspect the live page, write the script, and dry-run it. Submit only a script \
whose dry run reaches the save. Be economical: a few dry runs, then submit."""

SCRIPT_TOOLS: list[dict[str, Any]] = [
    {"name": "read_screen", "description": "Open the app's start page fresh and list its controls, buttons and text. With 'clicks', first click these CSS selectors in order (to look at later screens).",
     "input_schema": {"type": "object", "properties": {"clicks": {"type": "array", "items": {"type": "string"}}},
                      "additionalProperties": False}},
    {"name": "dry_run", "description": "Run a script once on a fresh page with the save request blocked. Returns its result, reason, log and any traceback.",
     "input_schema": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"], "additionalProperties": False}},
    {"name": "submit", "description": "Submit the final script.",
     "input_schema": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"], "additionalProperties": False}},
]

ALLOWED_IMPORTS = {"re", "time", "json"}
FORBIDDEN = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "os", "sys",
             "subprocess", "importlib", "builtins", "__builtins__", "breakpoint", "input", "urllib"}


def check_source(source: str) -> None:
    """A static check, not isolation: the scripts only touch the local MockMed."""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise ValueError(f"import {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                raise ValueError(f"from {node.module} import is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN:
            raise ValueError(f"name {node.id} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"attribute {node.attr} is not allowed")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
        raise ValueError("the script must define run(ctx, params)")


def _load(source: str) -> Callable[..., dict[str, Any]]:
    check_source(source)
    ns: dict[str, Any] = {"__name__": "agent_script"}
    exec(compile(source, "<script>", "exec"), ns)  # noqa: S102 - checked above
    return ns["run"]


class _Ctx:
    def __init__(self, page: Any, origin: str, commit: bool) -> None:
        self.page = page
        self.commit = commit
        self.lines: list[str] = []
        self._origin = origin

    def records(self) -> list[dict[str, Any]]:
        return read_records(self._origin)

    def log(self, msg: Any) -> None:
        self.lines.append(str(msg)[:400])


def run_script(b: _Browser, source: str, url: str, params: dict[str, str], *, commit: bool) -> dict[str, Any]:
    ctx, page, blocked = b.page(url, block_save=not commit)
    c = _Ctx(page, _origin(url), commit)
    t0 = time.monotonic()
    try:
        out = _load(source)(c, dict(params))
        if not isinstance(out, dict) or out.get("result") not in ("SAVED", "NOT SAVED"):
            res = {"result": "ERROR", "reason": f"bad return value {out!r}"[:300]}
        else:
            res = {"result": out["result"], "reason": str(out.get("reason", ""))[:500]}
    except Exception as exc:  # noqa: BLE001 - a failing script is the data
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        res = {"result": "ERROR", "reason": "".join(tb[-3:])[-1500:]}
    finally:
        if not commit:
            page.wait_for_timeout(300)
        ctx.close()
    res.update(seconds=round(time.monotonic() - t0, 3), log=c.lines[-40:], blocked_saves=blocked["n"])
    return res


def broke(res: dict[str, Any]) -> bool:
    return res["result"] == "ERROR" or res["reason"].upper().startswith("UNEXPECTED")


class AgentScript:
    """An agent-written script with read-back and a duplicate guard, kept working by the agent."""

    name = "agent-script"
    paid = True

    def __init__(self, session_budget_usd: float = 1.50, max_turns: int = 16,
                 max_repairs_per_signature: int = 2) -> None:
        self.session_budget_usd = session_budget_usd
        self.max_turns = max_turns
        self.max_repairs = max_repairs_per_signature
        self.source: Optional[str] = None
        self.versions = 0
        self._tries: dict[str, int] = {}
        self._b = _Browser()
        self._dir: Optional[Path] = None

    @staticmethod
    def params(task: Task) -> dict[str, str]:
        return {"patient": "Jane Sample", "type": "Triage", "note": task.params["note"]}

    def run(self, task: Task) -> Report:
        self._dir = task.work_dir.parent.parent / "scripts"
        self._dir.mkdir(parents=True, exist_ok=True)
        events: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        if self.source is None:
            s = self._session("author", task, None)
            sessions.append(s)
            events.append({"kind": "script_review", "phase": "setup", "minutes": 0, "by": "scripted"})
            if self.source is None:
                return Report(False, final_text="the agent submitted no script", model_calls=s["calls"],
                              cost_usd=s["usd"], extra={"human_events": events, "sessions": sessions})
        res = run_script(self._b, self.source, task.url, self.params(task), commit=True)
        first = res
        if broke(res):
            sig = res["reason"].splitlines()[-1][:120] if res["reason"] else "?"
            if self._tries.get(sig, 0) < self.max_repairs:
                self._tries[sig] = self._tries.get(sig, 0) + 1
                s = self._session("repair", task, res)
                sessions.append(s)
                if s.get("submitted"):
                    events.append({"kind": "script_review", "phase": "maintenance", "minutes": 0, "by": "scripted"})
                    res = run_script(self._b, self.source, task.url, self.params(task), commit=True)
        calls = sum(s["calls"] for s in sessions)
        usd = sum(s["usd"] for s in sessions)
        if sessions:
            (task.work_dir / "sessions.json").write_text(json.dumps(sessions, indent=1))
        return Report(
            res["result"] == "SAVED",
            final_text=f'{res["result"]}: {res["reason"]}'[:2000],
            model_calls=calls,
            cost_usd=round(usd, 6),
            error=None if res["result"] != "ERROR" else res["reason"][-400:],
            extra={"human_events": events, "sessions": sessions, "script_version": self.versions,
                   "first_attempt": {k: first[k] for k in ("result", "reason", "seconds")},
                   "script_s": res["seconds"]},
        )

    # One authoring or repair session with the coding agent.
    def _session(self, kind: str, task: Task, failure: Optional[dict[str, Any]]) -> dict[str, Any]:
        meter = budget.Meter(f"silent-saves agent-script {kind} item {task.item}", self.session_budget_usd)
        t0 = time.monotonic()
        client = budget.client(MODEL)
        if kind == "author":
            prompt = (f"{DEMONSTRATION}\n\n{API_DOC.format(origin='the app')}\n\n{CONTRACT}\n\n"
                      f"The task each run does: {task.instruction.replace(task.params['note'], '<note>')}")
        else:
            prompt = (f"The script below broke on a run. Fix it so it does the task on the app as it is now, "
                      f"keeping every required check.\n\n{CONTRACT}\n\nFailure: {failure['result']}: "
                      f"{failure['reason']}\nLog: {json.dumps(failure.get('log', []))}\n\n"
                      f"Current script:\n```python\n{self.source}\n```")
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        submitted = False
        dry_runs = 0
        for _turn in range(self.max_turns):
            try:
                response = _call(client, meter, SCRIPT_SYSTEM, SCRIPT_TOOLS, messages, 12000)
            except budget.ItemBudgetExceeded:
                break
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            if not uses:
                messages.append({"role": "user", "content": "Use the tools; finish by calling submit."})
                continue
            results = []
            done = False
            for b in uses:
                try:
                    if b.name == "read_screen":
                        out: Any = self._look(task.url, list(b.input.get("clicks") or []))
                    elif b.name == "dry_run":
                        dry_runs += 1
                        out = run_script(self._b, b.input["source"], task.url, self.params(task), commit=False)
                    elif b.name == "submit":
                        check_source(b.input["source"])
                        self.source = b.input["source"]
                        self.versions += 1
                        if self._dir:
                            (self._dir / f"v{self.versions:02d}-{kind}-item{task.item:03d}.py").write_text(self.source)
                        submitted = done = True
                        out = "submitted"
                    else:
                        raise ValueError(f"unknown tool {b.name}")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)[:12000]})
                except Exception as exc:  # noqa: BLE001 - the agent sees the error
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": f"Error: {exc}"[:600], "is_error": True})
            if done:
                break
            messages.append({"role": "user", "content": results})
        return {"kind": kind, "item": task.item, "submitted": submitted, "dry_runs": dry_runs,
                "calls": meter.calls, "usd": round(meter.usd, 6), "agent_s": round(time.monotonic() - t0, 2),
                "version": self.versions}

    def _look(self, url: str, clicks: list[str]) -> dict[str, Any]:
        ctx, page, _ = self._b.page(url, block_save=True)
        try:
            for sel in clicks[:6]:
                page.locator(sel).first.click()
                page.wait_for_timeout(200)
            return _screen(page)
        finally:
            ctx.close()

    def close(self) -> None:
        self._b.close()
