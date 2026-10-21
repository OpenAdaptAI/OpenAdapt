"""Arm ``agent_tools``: the same frontier model with structured tools and read-back.

This is the strong agent, not a strawman. Instead of screenshots it gets what
OpenAdapt's runtime gets:

- ``read_screen``: the visible controls of every frame (the DOM and
  accessibility view): id, label, type, value, required, options; the visible
  buttons and links; the text of any dialog or alert, including OpenEMR's
  possible-duplicate popup.
- ``set_field`` / ``click``: act on a control by the reference ``read_screen``
  gave it.
- ``find_patients``: a REST search of the record (the clerk's own API access),
  so it can check for an existing chart before it saves and confirm the chart
  after it saves.
- ``finish``: its claim, which the oracle checks independently.

Same procedure, model, effort and per-item budget style as ``cua_alone``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..arm import Env, ItemResult, Operator, OperatorRequest, WorkItem
from . import budget
from .openemr_ui import SOP, OpenEMRSession, RestReader

SYSTEM = """\
You register patients in a clinic's OpenEMR through tools. You cannot see the \
screen; read_screen gives you its controls and any dialog text. References from \
read_screen are valid until the next read_screen. After a click that saves or \
opens a dialog, call read_screen again. Use find_patients to look the person up \
in the record before you save and to confirm the chart after you save. Call \
finish exactly once at the end."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_screen",
        "description": "List the visible form controls, buttons and dialog text on the OpenEMR page, with a ref for each control.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_field",
        "description": "Set a text field, date field or select list (by its visible option text) to a value.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
            "required": ["ref", "value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "click",
        "description": "Click a button, link or checkbox by its ref.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "click_text",
        "description": "Click the first visible element whose text is exactly this (any frame), e.g. a section header.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "find_patients",
        "description": "Search existing patient charts through OpenEMR's REST API. Any of fname, lname, DOB (YYYY-MM-DD). Returns matching charts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fname": {"type": "string"},
                "lname": {"type": "string"},
                "DOB": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "Report the outcome. result is SAVED only if exactly one new chart for this referral exists now.",
        "input_schema": {
            "type": "object",
            "properties": {
                "result": {"type": "string", "enum": ["SAVED", "NOT SAVED"]},
                "reason": {"type": "string"},
            },
            "required": ["result", "reason"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

READ_JS = r"""
(start) => {
  let n = start;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = e => {
    if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l && l.innerText.trim()) return l.innerText.trim(); }
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    if (e.title) return e.title;
    const td = e.closest('td'); if (td && td.previousElementSibling) { const t = td.previousElementSibling.innerText.trim(); if (t) return t.slice(0,60); }
    const div = e.closest('.form-group, .col, div'); if (div) { const l = div.querySelector('label'); if (l && l.innerText.trim()) return l.innerText.trim().slice(0,60); }
    return e.placeholder || '';
  };
  const out = {controls: [], buttons: []};
  document.querySelectorAll('[data-ar]').forEach(e => e.removeAttribute('data-ar'));
  document.querySelectorAll('input, select, textarea').forEach(e => {
    if (!vis(e) || e.type === 'hidden') return;
    if (e.type === 'button' || e.type === 'submit') return;
    const ref = String(n++); e.setAttribute('data-ar', ref);
    const c = {ref, id: e.id || e.name, label: labelOf(e), type: e.tagName === 'SELECT' ? 'select' : (e.type || e.tagName.toLowerCase()), value: e.tagName === 'SELECT' ? (e.selectedOptions[0] ? e.selectedOptions[0].text : '') : (e.type === 'checkbox' ? String(e.checked) : e.value)};
    if (e.required) c.required = true;
    if (e.tagName === 'SELECT') { const o = [...e.options].map(x => x.text.trim()).filter(Boolean); c.options = o.length <= 15 ? o : `${o.length} options; set by visible text`; }
    out.controls.push(c);
  });
  document.querySelectorAll('button, input[type=button], input[type=submit], a.btn, a[onclick], a[href^="#"], [role=button], [role=tab], [data-toggle], [data-bs-toggle], [aria-expanded], summary').forEach(e => {
    if (!vis(e)) return;
    const t = (e.innerText || e.value || e.title || '').trim(); if (!t) return;
    const ref = String(n++); e.setAttribute('data-ar', ref);
    out.buttons.push({ref, text: t.slice(0,60), id: e.id || undefined});
  });
  const alerts = [...document.querySelectorAll('.alert, .modal.show, .error, .text-danger, .invalid-feedback, [role=dialog], [role=alert]')]
     .filter(vis).map(e => e.innerText.trim()).filter(Boolean).map(t => t.slice(0,400));
  if (alerts.length) out.notices = alerts;
  out.next = n;
  return out;
}
"""


class AgentToolsArm:
    name = "agent_tools"

    def __init__(self, options: dict[str, Any]) -> None:
        self.name = str(options.get("name", self.name))
        self.model = options.get("model", "claude-fable-5-1")
        self.effort = options.get("effort", "medium")
        self.max_turns = int(options.get("max_turns", 40))
        self.item_budget_usd = float(options.get("item_budget_usd", 1.00))
        self.headless = bool(options.get("headless", True))
        self.session: OpenEMRSession | None = None
        self.rest: RestReader | None = None
        self._dialogs: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "kind": "agent with structured tools and record read-back",
            "model": self.model,
            "effort": self.effort,
            "tools": [t["name"] for t in TOOLS],
            "max_turns_per_item": self.max_turns,
            "item_budget_usd": self.item_budget_usd,
            "prices_as_of": budget.prices()["as_of"],
            "sees": "DOM and accessibility view of every frame; dialog text",
            "reads_record": "OpenEMR REST patient search (clerk's API access)",
        }

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        self.session = OpenEMRSession(env, headless=self.headless)
        self.rest = RestReader(env)
        t0 = time.time()
        self.session.start()
        self.session.page.on("dialog", self._on_dialog)
        self.session.sign_in()
        return [
            {
                "phase": "write_procedure_and_tools",
                "wall_s": round(time.time() - t0, 2),
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "detail": {"note": "the shared SOP plus five tool wrappers (this file); engineering time to build the tools is reported in the report's setup table, not here"},
            }
        ]

    def _on_dialog(self, dialog: Any) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}"[:300])
        try:
            dialog.accept()
        except Exception:  # noqa: BLE001
            pass

    def after_drift(self, event: dict[str, Any], env: Env, operator: Operator) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    # -- tools -------------------------------------------------------------------

    def _read_screen(self) -> dict[str, Any]:
        page = self.session.page
        page.wait_for_timeout(300)
        n = 1
        frames: list[dict[str, Any]] = []
        for i, frame in enumerate(page.frames):
            try:
                got = frame.evaluate(READ_JS, n)
            except Exception:  # noqa: BLE001 - detached frames are skipped
                continue
            n = got.pop("next")
            if got["controls"] or got["buttons"] or got.get("notices"):
                url = (frame.url or "").split("?")[0]
                frames.append({"frame": url.rsplit("/", 2)[-1] or f"frame{i}", **got})
        out: dict[str, Any] = {"frames": frames}
        if self._dialogs:
            out["browser_dialogs"] = self._dialogs[:]
            self._dialogs.clear()
        return out

    def _locate(self, ref: str) -> Any:
        for frame in self.session.page.frames:
            try:
                loc = frame.locator(f'[data-ar="{ref}"]')
                if loc.count():
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
        raise KeyError(f"no control with ref {ref}; call read_screen again")

    def _settle(self) -> None:
        page = self.session.page
        page.wait_for_timeout(400)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001
            pass

    def _tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "read_screen":
            return self._read_screen()
        if name == "set_field":
            loc = self._locate(args["ref"])
            tag = loc.evaluate("e => e.tagName.toLowerCase()")
            if tag == "select":
                try:
                    loc.select_option(label=args["value"])
                except Exception:  # noqa: BLE001
                    loc.select_option(value=args["value"])
            else:
                loc.fill(args["value"])
                loc.dispatch_event("change")
            return "ok"
        if name == "click":
            self._locate(args["ref"]).click(timeout=15_000)
            self._settle()
            return "clicked; call read_screen to see the result"
        if name == "click_text":
            for frame in self.session.page.frames:
                try:
                    loc = frame.get_by_text(args["text"], exact=True).locator("visible=true")
                    if loc.count():
                        loc.first.click(timeout=15_000)
                        self._settle()
                        return "clicked; call read_screen to see the result"
                except Exception:  # noqa: BLE001
                    continue
            raise KeyError(f"no visible element with the text {args['text']!r}")
        if name == "find_patients":
            return self.rest.find(**args)
        raise KeyError(name)

    # -- one item ----------------------------------------------------------------

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult:
        assert self.session is not None
        prep = self.session.blank_form()
        meter = budget.Meter(f"{self.name} {item.item_id}", self.item_budget_usd)
        evidence = Path(env.workdir) / self.name / item.item_id
        evidence.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        claim: dict[str, Any] | None = None
        error = ""
        turns = 0
        trace: list[dict[str, Any]] = []
        try:
            claim, turns = self._loop(item, meter, trace)
        except budget.SpendCapReached:
            raise
        except budget.ItemBudgetExceeded as exc:
            error = f"budget: {exc}"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            (evidence / "trace.json").write_text(json.dumps(trace, indent=1, default=str)[:400_000])
        wall = time.time() - t0
        if claim is not None and claim.get("result") == "SAVED":
            claimed, status = "done", f"RESULT: SAVED: {claim.get('reason', '')}"
        elif claim is not None:
            claimed, status = "stopped", f"RESULT: NOT SAVED: {claim.get('reason', '')}"
        else:
            claimed, status = "error", error or "no finish"
        if claimed != "done":
            operator.ask(OperatorRequest(kind="stop", item_id=item.item_id, reason=status, surface="agent finish"))
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed,
            product_status=status,
            wall_s=round(wall, 2),
            model_calls=meter.calls,
            cost_usd=round(meter.usd, 6),
            evidence=str(evidence.relative_to(env.workdir)),
            detail={**budget.summarize(meter), **prep, "turns": turns, "compute_usd": round(budget.compute_usd(wall), 6)},
        )

    def _loop(self, item: WorkItem, meter: budget.Meter, trace: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
        client = budget.client(self.model)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": SOP.replace(
                "End your final reply with exactly one line, either\nRESULT: SAVED\nor\nRESULT: NOT SAVED: <one-sentence reason>",
                "Finish by calling the finish tool.",
            ) + "\n\nThe referral:\n" + item.source_text}
        ]
        for turn in range(1, self.max_turns + 1):
            approx_in = 3000 + 2500 * turn
            meter.before_call(self.model, budget.max_call_usd(self.model, approx_in, 4000))
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOLS,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            meter.record(self.model, response.usage)
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            trace.append({"turn": turn, "stop": response.stop_reason, "calls": [{"name": b.name, "input": b.input} for b in uses]})
            if response.stop_reason == "refusal":
                return {"result": "NOT SAVED", "reason": "the model declined the request"}, turn
            if not uses:
                text = " ".join(b.text for b in response.content if b.type == "text")
                messages.append({"role": "user", "content": "Call the finish tool to report the outcome." + (" " if text else "")})
                continue
            results = []
            for b in uses:
                if b.name == "finish":
                    return dict(b.input), turn
                try:
                    out = self._tool(b.name, dict(b.input))
                    content = out if isinstance(out, str) else json.dumps(out, default=str)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": content[:20_000]})
                except Exception as exc:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {exc}"[:400], "is_error": True})
                trace[-1].setdefault("results", []).append(results[-1]["content"][:2000])
            messages.append({"role": "user", "content": results})
        return None, self.max_turns


def factory(options: dict[str, Any]) -> AgentToolsArm:
    return AgentToolsArm(options)
