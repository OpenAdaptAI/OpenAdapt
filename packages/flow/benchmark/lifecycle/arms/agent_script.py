"""Arm ``agent_script``: an agent writes a Playwright + REST script, then maintains it.

Setup: the agent gets the same demonstration and documents as OpenAdapt (the
recorded steps, the procedure, the REST read) and the live form to inspect. It
writes a script with an equivalent check (a REST read-back after the save, and
a duplicate check before it), tries it in dry runs (the save request is
blocked at the network layer), and submits it. A person then reviews the script
once before it goes live: the setup phase is named ``compile`` so the cost
model charges it exactly what it charges for reviewing a compiled program.

Per item: the script runs with no model calls. A business stop (duplicate,
missing date of birth) goes to the operator. When the script breaks (an
exception, or a reason starting "UNEXPECTED"), the agent is called again with
the script, the failure and the live form, writes a fix, dry-runs it on this
item, and the operator reviews the fix (``repair_review``) before the item is
run again. That maintenance is charged as maintenance, with its tokens and
the review, not as per-item cost.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from ..arm import Env, ItemResult, Operator, OperatorRequest, WorkItem
from . import budget, scripts
from .agent_tools import READ_JS
from .openemr_ui import SOP, OpenEMRSession, RestReader

CONTRACT = '''\
Write one Python module that defines

    def register(ctx, referral: dict) -> dict

returning {"result": "SAVED" | "NOT SAVED", "reason": "<one sentence>"}.
referral has the keys title, fname, lname, DOB, sex, street, city,
state_label, postal_code, phone_home, email, country_code, exactly as the
referral states them (values may be missing, blank or malformed).
ctx.page is a Playwright sync Page (signed in, on a blank New Patient form);
ctx.form is the form's Frame; ctx.find_patients(fname=, lname=, DOB=) is the
REST search; ctx.commit is False in a dry run; ctx.log(msg) records a line.
Only re, time, datetime and json may be imported; no file, OS or eval access.
Required behaviour:
- Apply the procedure's rules (normalise names and dates; stop on a missing
  date of birth; check for an existing chart before saving; never create a
  duplicate).
- After the confirmed save, read the chart back with ctx.find_patients and
  return SAVED only if exactly one chart matches the referral's values.
- In a dry run (ctx.commit is False), do everything including the confirm
  click (the save request is blocked for you), skip the read-back and return
  {"result": "NOT SAVED", "reason": "DRY RUN reached confirm"}.
- When the screen is not what the script expects (a control or dialog is
  missing, a wait times out), return NOT SAVED with a reason starting
  "UNEXPECTED:" rather than guessing.'''

SYSTEM = """\
You are a careful automation engineer. You write and maintain a Playwright \
script that registers referred patients in a clinic's OpenEMR, against a local \
synthetic test copy. Use the tools to inspect the live form, write the script, \
and dry-run it. Submit only a script whose dry runs pass. Be economical: \
a few dry runs, then submit."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_screen",
        "description": "Open a blank New Patient form and list the visible controls, buttons and dialog text of every frame.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "find_patients",
        "description": "REST search of existing charts by fname, lname, DOB.",
        "input_schema": {
            "type": "object",
            "properties": {"fname": {"type": "string"}, "lname": {"type": "string"}, "DOB": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "dry_run",
        "description": "Run the given script on a fresh blank form with the save request blocked. Returns its result, reason, log, and the controls on screen at the end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "the whole module"},
                "referral": {"type": "object", "description": "a synthetic referral dict to try"},
            },
            "required": ["source", "referral"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit",
        "description": "Submit the final script (the whole module).",
        "input_schema": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["source"],
            "additionalProperties": False,
        },
    },
]


class AgentScriptArm:
    name = "agent_script"

    def __init__(self, options: dict[str, Any]) -> None:
        self.model = options.get("model", "claude-opus-5-5")
        self.effort = options.get("effort", "high")
        self.author_budget_usd = float(options.get("author_budget_usd", 4.0))
        self.fix_budget_usd = float(options.get("fix_budget_usd", 2.0))
        self.max_turns = int(options.get("max_turns", 30))
        self.headless = bool(options.get("headless", True))
        self.session: Optional[OpenEMRSession] = None
        self.rest: Optional[RestReader] = None
        self.source = ""
        self.version = 0
        self.dir: Optional[Path] = None

    def describe(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "kind": "agent-written Playwright + REST script with read-back; agent maintains it on failure",
            "model": self.model,
            "effort": self.effort,
            "author_budget_usd": self.author_budget_usd,
            "fix_budget_usd": self.fix_budget_usd,
            "prices_as_of": budget.prices()["as_of"],
            "per_item_model_calls": 0,
            "sandbox": "static import/name check; dry runs block the save request",
        }

    # -- authoring and maintenance ---------------------------------------------------

    def _read_screen(self) -> dict[str, Any]:
        self.session.blank_form()
        frames = []
        n = 1
        for frame in self.session.page.frames:
            try:
                got = frame.evaluate(READ_JS, n)
            except Exception:  # noqa: BLE001
                continue
            n = got.pop("next")
            if got["controls"] or got["buttons"] or got.get("notices"):
                frames.append({"frame": (frame.url or "").split("?")[0].rsplit("/", 1)[-1], **got})
        return {"frames": frames}

    def _end_screen(self) -> list[dict[str, Any]]:
        out = []
        for frame in self.session.page.frames:
            try:
                got = frame.evaluate(READ_JS, 1)
            except Exception:  # noqa: BLE001
                continue
            got.pop("next", None)
            if got["buttons"] or got.get("notices"):
                out.append({"frame": (frame.url or "").split("?")[0].rsplit("/", 1)[-1],
                            "buttons": got["buttons"][:30], "notices": got.get("notices", [])})
        return out

    def _session_loop(self, opening: str, meter: budget.Meter, trace: list[dict[str, Any]]) -> tuple[Optional[str], list[dict[str, Any]]]:
        """Run the authoring agent until it submits. Returns (source, dry runs)."""

        client = budget.client()
        messages: list[dict[str, Any]] = [{"role": "user", "content": opening}]
        dry_runs: list[dict[str, Any]] = []
        for turn in range(1, self.max_turns + 1):
            approx_in = 8000 + 3000 * turn
            meter.before_call(self.model, budget.max_call_usd(self.model, approx_in, 8000))
            with client.messages.stream(
                model=self.model,
                max_tokens=32000,
                system=SYSTEM,
                tools=TOOLS,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                messages=messages,
            ) as stream:
                response = stream.get_final_message()
            meter.record(self.model, response.usage)
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            trace.append({"turn": turn, "stop": response.stop_reason, "tools": [b.name for b in uses]})
            if response.stop_reason == "refusal":
                return None, dry_runs
            if not uses:
                messages.append({"role": "user", "content": "Use the tools; finish with submit."})
                continue
            results = []
            for b in uses:
                args = dict(b.input)
                if b.name == "submit":
                    try:
                        scripts.check_source(args["source"])
                    except Exception as exc:  # noqa: BLE001
                        results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                        "content": f"rejected: {exc}"})
                        continue
                    return args["source"], dry_runs
                try:
                    if b.name == "read_screen":
                        out: Any = self._read_screen()
                    elif b.name == "find_patients":
                        out = self.rest.find(**args)
                    elif b.name == "dry_run":
                        r = scripts.run(args["source"], self.session, self.rest, args.get("referral") or {}, commit=False)
                        out = {"result": r.result, "reason": r.reason, "log": r.log[-30:],
                               "save_requests_blocked": r.blocked_saves, "seconds": round(r.seconds, 1),
                               "screen_at_end": self._end_screen()}
                        dry_runs.append({k: out[k] for k in ("result", "reason", "save_requests_blocked")})
                    else:
                        raise KeyError(b.name)
                    content = json.dumps(out, default=str)[:30_000]
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
                except Exception as exc:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": f"Error: {exc}"[:600]})
            messages.append({"role": "user", "content": results})
        return None, dry_runs

    def _save_version(self, source: str, why: str) -> None:
        self.version += 1
        self.source = source
        assert self.dir is not None
        (self.dir / f"script_v{self.version}.py").write_text(f"# {why}\n" + source)

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        self.session = OpenEMRSession(env, headless=self.headless)
        self.rest = RestReader(env)
        self.dir = Path(env.workdir) / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session.start()
        self.session.sign_in()
        meter = budget.Meter(f"{self.name} author", self.author_budget_usd)
        trace: list[dict[str, Any]] = []
        t0 = time.time()
        opening = "\n\n".join([
            "Write the registration script.", SOP, scripts.DEMONSTRATION, scripts.API_DOC, CONTRACT,
        ])
        source, dry = None, []
        error = ""
        try:
            source, dry = self._session_loop(opening, meter, trace)
        except budget.SpendCapReached:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:500]
        wall = time.time() - t0
        (self.dir / "author_trace.json").write_text(json.dumps({"trace": trace, "dry_runs": dry, "error": error}, indent=1))
        if source is None:
            raise RuntimeError(f"the agent did not submit a script: {error or 'no submit'}")
        self._save_version(source, "authored by the agent at setup")
        return [
            {
                "phase": "author_script",
                "wall_s": round(wall, 2),
                "attended_s": 0.0,
                **budget.summarize(meter),
                "detail": {"dry_runs": dry, "version": self.version},
            },
            {
                # The person reads the script once before it goes live; charged
                # like the review of a compiled program.
                "phase": "compile",
                "wall_s": 0.0,
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "detail": {"what": "a person reviews the generated script before it goes live"},
            },
        ]

    def _maintain(self, item: WorkItem, failure: scripts.RunOutcome, operator: Operator) -> tuple[bool, dict[str, Any]]:
        """Ask the agent for a fix; the operator reviews it. Returns (accepted, phase)."""

        meter = budget.Meter(f"{self.name} fix {item.item_id}", self.fix_budget_usd)
        trace: list[dict[str, Any]] = []
        t0 = time.time()
        opening = "\n\n".join([
            "The live script failed on a referral. Fix it, dry-run the fix on this referral, and submit the whole module.",
            "Failure:\n" + failure.reason + "\nScript log:\n" + "\n".join(failure.log[-30:]),
            "The referral:\n" + json.dumps(item.params),
            "The current script:\n```python\n" + self.source + "\n```",
            CONTRACT, SOP,
        ])
        source, dry, error = None, [], ""
        try:
            source, dry = self._session_loop(opening, meter, trace)
        except budget.SpendCapReached:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:500]
        probes_ok = bool(source) and bool(dry) and dry[-1]["result"] != "ERROR" and not dry[-1]["reason"].upper().startswith("UNEXPECTED")
        decision = operator.ask(
            OperatorRequest(
                kind="repair_review",
                item_id=item.item_id,
                reason=f"script fix after: {failure.reason.splitlines()[-1][:200] if failure.reason else ''}",
                surface="agent_script maintenance",
                detail={"probes_passed": probes_ok, "dry_runs": dry[-3:], "error": error},
            )
        )
        accepted = source is not None and decision.action == "accept_repair"
        if accepted:
            self._save_version(source, f"fix after {item.item_id}: {failure.reason.splitlines()[-1][:120]}")
        phase = {
            "phase": "fix_script",
            "item_id": item.item_id,
            "wall_s": round(time.time() - t0, 2),
            "attended_s": 0.0,
            **budget.summarize(meter),
            "detail": {"accepted": accepted, "probes_passed": probes_ok, "dry_runs": dry,
                       "version": self.version, "error": error, "turns": len(trace)},
        }
        return accepted, phase

    # -- per item -------------------------------------------------------------------

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult:
        assert self.session is not None and self.rest is not None
        t0 = time.time()
        phases: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        out = scripts.run(self.source, self.session, self.rest, item.params, commit=True)
        attempts.append({"version": self.version, "result": out.result, "reason": out.reason[-300:], "s": round(out.seconds, 2)})
        if out.broke:
            accepted, phase = self._maintain(item, out, operator)
            phases.append(phase)
            if accepted:
                out = scripts.run(self.source, self.session, self.rest, item.params, commit=True)
                attempts.append({"version": self.version, "result": out.result, "reason": out.reason[-300:], "s": round(out.seconds, 2)})
        wall = time.time() - t0
        if out.result == "SAVED":
            claimed, status = "done", f"SAVED: {out.reason}"
        elif out.result == "NOT SAVED":
            claimed, status = "stopped", f"NOT SAVED: {out.reason}"
        else:
            claimed, status = "error", out.reason[-300:]
        if claimed != "done":
            operator.ask(OperatorRequest(kind="stop", item_id=item.item_id, reason=status[:300], surface="script result"))
        maint_cost = sum(p.get("cost_usd", 0.0) for p in phases)
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed,
            product_status=status[:500],
            wall_s=round(wall, 2),
            model_calls=0,
            cost_usd=0.0,
            evidence=f"{self.name}/",
            detail={
                "attempts": attempts,
                "script_version": self.version,
                "maintenance_phases": phases,
                "maintenance_cost_usd": round(maint_cost, 6),
                "usage": [],
                "compute_usd": round(budget.compute_usd(sum(a["s"] for a in attempts)), 6),
            },
        )

    def after_drift(self, event: dict[str, Any], env: Env, operator: Operator) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def factory(options: dict[str, Any]) -> AgentScriptArm:
    return AgentScriptArm(options)
