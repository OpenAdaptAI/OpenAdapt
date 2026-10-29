"""Arms A and B: the same #1528 agent, with and without OpenAdapt's verifier.

**A** is ``lifecycle.arms.agent_tools.AgentToolsArm`` (#1528's ``agent_tools``)
with its prompt, tools, model, effort, turn limit and budget unchanged. This
module adds only the harness plumbing both arms share:

- the save request (``new_comprehensive_save.php``) is routed through the
  harness so an item's hooks can act on it (``faults.py``);
- ``save_timeout``: the save is sent and lands, and then the browser's
  request is failed as timed out; the click returns a timeout error;
- ``restart_after_save``: after the save's response, the agent's process
  and browser are ended, and a fresh agent with a fresh browser starts the
  same item (no memory of the first).

**B** is A plus OpenAdapt on the write, as the #1609 adapters wire it:

- before the agent acts, ``openadapt_flow.verifier.capture`` reads the
  system of record against the operator's contract, pinned to the referral's
  values (normalised as the intake mapping does: names trimmed and
  capitalised, dates to ISO). If it can't be read, nothing is written;
- the first save request is let through. Any later one in the same item is
  held, not sent, and the click says so ("held: reconcile first");
- the snapshot is saved (0600) and survives a restart of the agent;
- at ``finish``, ``verify_write`` reads again. ``verified`` is done.
  ``refuted`` or ``unconfirmed`` goes to a person. A "NOT SAVED" finish is
  checked as ``write_attempted=False``: if anything moved, a person
  reconciles it.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from lifecycle.arm import Env, ItemResult, Operator, OperatorRequest, WorkItem
from lifecycle.arms import budget
from lifecycle.arms.agent_tools import SYSTEM, TOOLS, AgentToolsArm
from lifecycle.arms.openemr_ui import SOP, OpenEMRSession, RestReader

from .faults import Ctx, FormFields, Hooks

SAVE_URL = "**/interface/new/new_comprehensive_save.php*"


class Restart(Exception):
    """The agent's process was ended after the save (harness-injected)."""


# -- form bodies -------------------------------------------------------------------


def _parse(body: bytes, content_type: str) -> tuple[FormFields, str]:
    if content_type.startswith("multipart/form-data"):
        boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
        fields: FormFields = []
        for part in body.split(b"--" + boundary.encode()):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            head, _, value = part.partition(b"\r\n\r\n")
            name = ""
            for line in head.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    for bit in line.split(";"):
                        bit = bit.strip()
                        if bit.startswith("name="):
                            name = bit[5:].strip('"')
            fields.append((name, value.decode("latin-1")))
        return fields, "multipart:" + boundary
    pairs = urllib.parse.parse_qsl(body.decode("latin-1"), keep_blank_values=True, encoding="latin-1")
    return list(pairs), "urlencoded"


def _render(fields: FormFields, how: str) -> bytes:
    if how.startswith("multipart:"):
        b = how.split(":", 1)[1]
        out = b""
        for name, value in fields:
            out += (
                f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("latin-1")
                + value.encode("latin-1")
                + b"\r\n"
            )
        return out + f"--{b}--\r\n".encode()
    return urllib.parse.urlencode(fields, encoding="latin-1").encode("latin-1")


class ArmA(AgentToolsArm):
    """#1528's agent_tools, with the shared fault plumbing."""

    name = "A_agent_readback"
    guarded = False

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__({"model": "claude-sonnet-5-5", "effort": "medium", "item_budget_usd": 0.50, **options})
        self.ctx: Optional[Ctx] = None
        self.hooks = Hooks()
        self._env: Optional[Env] = None
        self._reset_item_state()

    def _reset_item_state(self) -> None:
        self.save_requests = 0
        self.saves_sent = 0
        self.held = 0
        self._save_in_click = False
        self._timed_out = False
        self._restarted = False
        self.save_log: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({"arm": self.name, "base": "lifecycle.arms.agent_tools (#1528), unchanged prompt and tools", "guarded": self.guarded})
        return d

    # -- session with the save route ------------------------------------------

    def _new_session(self, env: Env) -> None:
        self.session = OpenEMRSession(env, headless=self.headless)
        self.session.start()
        self.session.page.on("dialog", self._on_dialog)
        self.session.context.route(SAVE_URL, self._on_save_route)
        self.session.sign_in()

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        self._env = env
        self.rest = RestReader(env)
        t0 = time.time()
        self._new_session(env)
        # Open the form and one existing chart once, so first-use UI settings
        # (the user's, and the dashboard's global ones) are written before any
        # item.
        self.session.blank_form()
        try:
            self.session.page.evaluate(
                "() => { window.navigateTab('/interface/patient_file/summary/demographics.php?set_pid=1', 'pat'); "
                "window.activateTabByName('pat', true); }"
            )
            self.session.page.wait_for_timeout(6000)
        except Exception:  # noqa: BLE001 - a warm-up only
            pass
        return [{"phase": "sign_in", "wall_s": round(time.time() - t0, 2), "attended_s": 0.0, "model_calls": 0, "cost_usd": 0.0}]

    def begin(self, ctx: Ctx, hooks: Hooks) -> None:
        self.ctx, self.hooks = ctx, hooks
        self._reset_item_state()

    def _on_save_route(self, route: Any, request: Any) -> None:
        if request.method != "POST":
            route.continue_()
            return
        self.save_requests += 1
        n = self.save_requests
        if self.guarded and self.saves_sent >= 1:
            self.held += 1
            self.save_log.append({"n": n, "action": "held", "t": round(time.time(), 2)})
            route.fulfill(
                status=409,
                content_type="text/html",
                body="<html><body><div class='alert'>Held by OpenAdapt: a save for this referral "
                "was already sent. Do not retry; it will be reconciled.</div></body></html>",
            )
            return
        ctx, hooks = self.ctx, self.hooks
        body = request.post_data_buffer or b""
        ctype = request.headers.get("content-type", "")
        mutated = False
        if n == 1 and hooks.form_mutators:
            fields, how = _parse(body, ctype)
            for m in hooks.form_mutators:
                fields = m(fields)
            body, mutated = _render(fields, how), True
        if n == 1 and ctx is not None:
            for fn in hooks.before_save:
                try:
                    fn(ctx)
                    ctx.log(op="before_save", ok=True)
                except Exception as exc:  # noqa: BLE001 - recorded
                    ctx.log(op="before_save", ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        headers = dict(request.headers)
        resp = route.fetch(post_data=body, headers=headers, timeout=120_000) if mutated else route.fetch(timeout=120_000)
        self.saves_sent += 1
        self._save_in_click = True
        self.save_log.append({"n": n, "action": "sent", "status": resp.status, "mutated": mutated, "t": round(time.time(), 2)})
        if ctx is not None:
            ctx.run_after_save(hooks)
        if hooks.save_timeout and n == 1:
            self._timed_out = True
            route.abort("timedout")
            return
        route.fulfill(response=resp)

    # -- tools -----------------------------------------------------------------

    def _tool(self, name: str, args: dict[str, Any]) -> Any:
        if name in ("click", "click_text"):
            self._save_in_click = False
            self._timed_out = False
            try:
                out = super()._tool(name, args)
            except Exception:
                if self._save_in_click and self.hooks.restart_after_save and not self._restarted:
                    raise Restart()
                raise
            if self._save_in_click and self.hooks.restart_after_save and not self._restarted:
                raise Restart()
            if self._timed_out:
                return "Error: TimeoutError: the page did not respond within 30000 ms after the click (net::ERR_TIMED_OUT)."
            return out
        return super()._tool(name, args)

    # -- one item -----------------------------------------------------------------

    def _prompt(self, item: WorkItem) -> str:
        return SOP.replace(
            "End your final reply with exactly one line, either\nRESULT: SAVED\nor\nRESULT: NOT SAVED: <one-sentence reason>",
            "Finish by calling the finish tool.",
        ) + "\n\nThe referral:\n" + item.source_text

    def _agent(self, item: WorkItem, meter: budget.Meter, trace: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], int]:
        """#1528's ``_loop``, verbatim in behaviour, with the claim judged by
        :meth:`_on_finish` before it is returned (B's verifier)."""

        client = budget.client(self.model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._prompt(item)}]
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
                messages.append({"role": "user", "content": "Call the finish tool to report the outcome."})
                continue
            results = []
            for b in uses:
                if b.name == "finish":
                    return dict(b.input), turn
                try:
                    out = self._tool(b.name, dict(b.input))
                    content = out if isinstance(out, str) else json.dumps(out, default=str)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": content[:20_000]})
                except Restart:
                    raise
                except Exception as exc:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {exc}"[:400], "is_error": True})
                trace[-1].setdefault("results", []).append(results[-1]["content"][:2000])
            messages.append({"role": "user", "content": results})
        return None, self.max_turns

    def _warm_tokens(self) -> None:
        """Both arms: take the item's REST token now, before the item (and
        before B's baseline), so token bookkeeping isn't written mid-item."""

        assert self.rest is not None
        self.rest._session = None
        self.rest._s()

    def _before_item(self, item: WorkItem, evidence: Path) -> Optional[str]:
        """B captures here; returns a stop reason when it must not proceed."""

        self._warm_tokens()
        return None

    def after_item(self, ctx: Ctx, result: ItemResult) -> Optional[dict[str, Any]]:
        """Work the arm does once the item's own run is over, before the
        oracle judges it. A checks nothing later."""

        return None

    def _on_restart(self) -> None:
        """A keeps nothing across the restart but the agent's own reading."""

    def _on_finish(self, claim: Optional[dict[str, Any]], item: WorkItem, evidence: Path) -> dict[str, Any]:
        """A trusts its own read-back: the claim stands."""

        if claim is not None and claim.get("result") == "SAVED":
            return {"claimed": "done", "status": f"RESULT: SAVED: {claim.get('reason', '')}"}
        if claim is not None:
            return {"claimed": "stopped", "status": f"RESULT: NOT SAVED: {claim.get('reason', '')}"}
        return {"claimed": "error", "status": "no finish"}

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult:
        assert self.session is not None
        meter = budget.Meter(f"{self.name} {item.item_id}", self.item_budget_usd)
        evidence = Path(env.workdir) / self.name / item.item_id
        evidence.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        claim: Optional[dict[str, Any]] = None
        error = ""
        turns = 0
        restarts = 0
        trace: list[dict[str, Any]] = []
        prep: dict[str, Any] = {}
        stop = self._before_item(item, evidence)
        verify_s = 0.0
        if stop is None:
            while True:
                try:
                    prep = self.session.blank_form()
                    claim, t = self._agent(item, meter, trace)
                    turns += t
                    break
                except Restart:
                    restarts += 1
                    self._restarted = True
                    self._on_restart()
                    trace.append({"restart": True, "t": round(time.time(), 2)})
                    self.session.close()
                    self._new_session(env)
                    continue
                except budget.SpendCapReached:
                    raise
                except budget.ItemBudgetExceeded as exc:
                    error = f"budget: {exc}"
                    break
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    break
            v0 = time.time()
            outcome = self._on_finish(claim, item, evidence)
            verify_s = time.time() - v0
            if outcome["claimed"] == "error" and error:
                outcome["status"] = error
        else:
            outcome = {"claimed": "stopped", "status": stop}
        (evidence / "trace.json").write_text(json.dumps(trace, indent=1, default=str)[:400_000])
        wall = time.time() - t0
        if outcome["claimed"] != "done":
            operator.ask(OperatorRequest(kind="stop", item_id=item.item_id, reason=outcome["status"], surface=self.name))
        return ItemResult(
            item_id=item.item_id,
            claimed=outcome["claimed"],
            product_status=outcome["status"],
            wall_s=round(wall, 2),
            model_calls=meter.calls,
            cost_usd=round(meter.usd, 6),
            evidence=str(evidence.relative_to(env.workdir)),
            detail={
                **budget.summarize(meter),
                **prep,
                "turns": turns,
                "restarts": restarts,
                "save_requests": self.save_requests,
                "saves_sent": self.saves_sent,
                "held": self.held,
                "save_log": self.save_log,
                "verify_s": round(verify_s, 2),
                "claim": claim,
                **{k: v for k, v in outcome.items() if k not in ("claimed", "status")},
            },
        )


def normalise(params: dict[str, str]) -> dict[str, str]:
    """The intake mapping B's operator configures: what the referral says,
    in the form the record stores (no truth from the harness)."""

    out = dict(params)
    for k in ("fname", "lname"):
        if k in out:
            v = " ".join(out[k].split())
            out[k] = v[:1].upper() + v[1:].lower() if v.isupper() or v.islower() else v
    dob = out.get("DOB", "")
    if "/" in dob:
        m, d, y = dob.split("/")
        out["DOB"] = f"{y}-{int(m):02d}-{int(d):02d}"
    out["street"] = out.get("street", "").upper()
    out["city"] = out.get("city", "").upper()
    return out


class ArmB(ArmA):
    name = "B_agent_openadapt"
    guarded = True

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.contract_path = Path(options["contract"])
        self._contract: Any = None
        self._before: Any = None
        self._params: dict[str, str] = {}
        self._pending: Any = None
        self._ledger_result: Optional[dict[str, Any]] = None
        self._ledger_block: Optional[str] = None
        self.recheck_queue: list[dict[str, Any]] = []
        self.late_path = Path(options.get("late_path", "late.jsonl"))

    def describe(self) -> dict[str, Any]:
        import hashlib

        d = super().describe()
        d.update(
            {
                "verifier": "openadapt_flow.verifier capture/verify_write (#1588) with the #1609 hold",
                "contract": str(self.contract_path),
                "contract_sha256": hashlib.sha256(self.contract_path.read_bytes()).hexdigest(),
            }
        )
        return d

    def _fresh_token(self) -> None:
        import os

        s = self._env.extra["rest_read_session"]()
        os.environ["INCREMENTAL_VALUE_REST_TOKEN"] = s.headers["Authorization"].removeprefix("Bearer ")
        s.close()

    def contract(self) -> Any:
        if self._contract is None:
            from openadapt_flow.verifier import Contract

            self._contract = Contract.load(str(self.contract_path))
        return self._contract

    # -- run 2: begin_write / verify_write with the durable ledger (#1643),
    # and the late recheck (#1639) -------------------------------------------

    def _run2(self) -> bool:
        frame = getattr(self.contract().config, "frame", None)
        database = getattr(frame, "database", None)
        return bool(database is not None and getattr(database, "recheck_after_s", None))

    def _ledger(self) -> Any:
        import os

        from openadapt_flow.write_ledger import FileLedger

        ldir = os.environ.get("INCREMENTAL_VALUE_LEDGER_DIR")
        if not ldir:
            raise SystemExit("run 2: set INCREMENTAL_VALUE_LEDGER_DIR to a fresh directory")
        return FileLedger(Path(ldir))

    def _actor(self) -> Any:
        from openadapt_flow.verifier import Actor

        return Actor("agent", "openadapt_actor")

    def _before_item(self, item: WorkItem, evidence: Path) -> Optional[str]:
        from openadapt_flow.verifier import BaselineUnreadable, capture

        self._warm_tokens()
        self._params = normalise(item.params)
        self._item_id = item.item_id
        self._evidence = evidence
        t0 = time.time()
        self._fresh_token()
        # OpenEMR writes its OAuth bookkeeping after the token response.
        time.sleep(2.0)
        if self._run2():
            from openadapt_flow.verifier import (
                AlreadyWritten,
                WriteBlocked,
                begin_write,
            )

            self._ledger_result = None
            self._ledger_block = None
            try:
                self._pending = begin_write(
                    self.contract(), self._params, actor=self._actor(),
                    ledger=self._ledger(), write_key=item.item_id, workdir=evidence,
                )
            except AlreadyWritten as exc:
                # A rerun of an item whose write the ledger already resolved
                # (a crash before the row was written): never send it again.
                self._pending = None
                self._ledger_result = dict(exc.result)
                self.saves_sent = 1
                self._capture_s = round(time.time() - t0, 2)
                return None
            except WriteBlocked as exc:
                self._pending = None
                return f"OpenAdapt: the write can't start ({exc}); nothing was written"
            self._capture_s = round(time.time() - t0, 2)
            return None
        try:
            self._before = capture(self.contract(), self._params)
        except BaselineUnreadable as exc:
            self._before = None
            return f"OpenAdapt: the record could not be read before the write ({exc}); nothing was written"
        # Survives a restart of the agent process.
        self._before.save(evidence / "baseline.json")
        self._capture_s = round(time.time() - t0, 2)
        return None

    def _on_restart(self) -> None:
        """The adapter process died: everything it held in memory is gone,
        including whether its save request was sent. Only the ledger
        remembers the open attempt."""

        if not self._run2() or getattr(self, "_pending", None) is None:
            return
        from openadapt_flow.verifier import AlreadyWritten, WriteBlocked, begin_write

        self._pending.release()
        self._pending = None
        # The fresh adapter asks the ledger before its next send, so the hold
        # stays on: a resend is refused while the earlier attempt is open.
        # (No new OAuth token here: minting one inside the write's window adds
        # token rows the frame rightly reports.)
        try:
            self._pending = begin_write(
                self.contract(), self._params, actor=self._actor(), ledger=self._ledger(),
                write_key=self._item_id, workdir=self._evidence / "after-restart",
            )
            self.saves_sent = 0  # a new attempt: its first save may go
        except AlreadyWritten as exc:
            self._ledger_result = dict(exc.result)
            self.saves_sent = 1  # hold every resend
        except WriteBlocked as exc:
            self._ledger_block = str(exc)
            self.saves_sent = 1

    def _on_finish(self, claim: Optional[dict[str, Any]], item: WorkItem, evidence: Path) -> dict[str, Any]:
        if self._run2():
            return self._on_finish_2(claim, item, evidence)
        return self._on_finish_1(claim, item, evidence)

    def _on_finish_2(self, claim: Optional[dict[str, Any]], item: WorkItem, evidence: Path) -> dict[str, Any]:
        from openadapt_flow.verifier import verify_write

        said_saved = claim is not None and claim.get("result") == "SAVED"
        # The item's token, minted before the baseline, is still valid: a new
        # one now would write OAuth rows inside the window.
        if self._ledger_block is not None:
            return {"claimed": "stopped", "status": f"OpenAdapt: held by the ledger ({self._ledger_block})",
                    "verifier": {"ledger": self._ledger_block[:300]}}
        if self._ledger_result is not None:
            r = self._ledger_result
            out = self._verdict_2(r, said_saved, evidence, via="ledger")
            # The reconciled attempt's own recheck state, beside its receipt
            # (verify_write's <workdir>/<write_id>/recheck.json).
            receipt = r.get("receipt_path")
            state = Path(receipt).parent / "recheck.json" if receipt else evidence / "recheck.json"
            self._queue_recheck(item.item_id, r, state)
            return out
        attempted = said_saved or self.saves_sent > 0
        result = verify_write(self._pending, write_attempted=attempted)
        self._pending = None
        r = result.to_dict()
        out = self._verdict_2(r, said_saved, evidence, via="verify_write")
        path = result.recheck.path if result.recheck is not None else None
        self._queue_recheck(item.item_id, r, Path(path) if path is not None else evidence / "no-recheck-state")
        return out

    def _queue_recheck(self, item_id: str, r: dict[str, Any], state_path: Path) -> None:
        """Every result with a recheck state goes in the queue: the verified
        ones are rechecked, and all of them excuse their own window for the
        earlier writes' rechecks (any outcome, #1639)."""

        if not state_path.is_file():
            if r.get("outcome") == "CONFIRMED":
                # A confirmed write in run 2 always has a recheck due; with no
                # state to recheck, it never passes (a late UNCERTAIN).
                q = {"item_id": item_id, "verdict_at": time.time()}
                self._late_row(q, {"outcome": "UNCERTAIN", "reasons": [f"no recheck state at {state_path.name}"]}, 0.0, 0.0)
            return
        self.recheck_queue.append({
            "item_id": item_id, "state_path": str(state_path), "params": dict(self._params),
            "due_at": r.get("recheck_due_at"), "verified": r.get("outcome") == "CONFIRMED",
            "verdict_at": time.time(),
        })

    def _verdict_2(self, r: dict[str, Any], said_saved: bool, evidence: Path, *, via: str) -> dict[str, Any]:
        (evidence / "result.json").write_text(json.dumps(r, indent=1, default=str)[:400_000])
        outcome = str(r.get("outcome") or "")
        v = {"outcome": outcome, "via": via, "reasons": list(r.get("reasons") or [])[:6],
             "capture_s": getattr(self, "_capture_s", None), "recheck_due_at": r.get("recheck_due_at")}
        if outcome == "CONFIRMED":
            return {"claimed": "done", "status": f"OpenAdapt: confirmed ({via})", "verifier": v}
        if outcome == "REFUTED" and r.get("retry_safe") and not said_saved:
            reason = "nothing moved"
            return {"claimed": "stopped", "status": f"RESULT: NOT SAVED (OpenAdapt: {reason})", "verifier": v}
        return {"claimed": "stopped", "status": f"OpenAdapt: {outcome}: {'; '.join(v['reasons'])[:300]}", "verifier": v}

    def after_item(self, ctx: Ctx, result: ItemResult) -> Optional[dict[str, Any]]:
        """Run every recheck that is due, at this item boundary, when the
        harness has let the item's background writes finish. In blocking
        mode, wait for this item's own recheck first."""


        if not self._run2():
            return None
        self.run_rechecks(wait=self._mode() == "blocking")
        return None

    def run_rechecks(self, *, wait: bool) -> None:
        from openadapt_flow.verifier import RecheckNotDue, RecheckState, recheck

        # The item's token is reused: a new one would write OAuth rows after
        # the post-save read, inside every recheck's window.
        remaining = []
        states = []
        for q in self.recheck_queue:
            try:
                states.append((q, RecheckState.load(Path(q["state_path"]), params=q["params"])))
            except Exception as exc:  # noqa: BLE001 - recorded as a late UNCERTAIN
                if q["verified"]:
                    self._late_row(q, {"outcome": "UNCERTAIN", "reasons": [f"state unreadable: {exc}"[:300]]}, 0.0, 0.0)
        for i, (q, state) in enumerate(states):
            if not q["verified"]:
                continue  # kept only to excuse its own window for earlier writes
            later = [s for _, s in states[i + 1 :]]
            t0 = time.time()
            due = _epoch(q.get("due_at"))
            waited = max(0.0, due - t0) if (wait and due) else 0.0
            try:
                late = recheck(self.contract(), state, params=q["params"], actor=self._actor(),
                               excused_by=later, wait=wait)
            except RecheckNotDue:
                remaining.append(q)
                continue
            except Exception as exc:  # noqa: BLE001 - a recheck that can't run never passes
                self._late_row(q, {"outcome": "UNCERTAIN", "reasons": [f"{type(exc).__name__}: {exc}"[:300]]},
                               waited, time.time() - t0 - waited)
                continue
            self._late_row(q, {"outcome": late.outcome, "reasons": list(late.reasons)[:6], "due_at": late.due_at,
                               "read_at": late.read_at, "excused": late.excused},
                           waited, time.time() - t0 - waited)
        # A rechecked entry is kept only as an excuse for earlier entries
        # still waiting; nothing before the first waiting one is needed.
        for q, _ in states:
            if q["verified"] and q not in remaining:
                q["verified"] = False
        waiting = [i for i, q in enumerate(self.recheck_queue) if q in remaining]
        self.recheck_queue = self.recheck_queue[waiting[0]:] if waiting else []

    def recheck_failed(self, item_id: str, exc: Exception) -> None:
        """Every queued, unrechecked verified write is UNCERTAIN: the
        recheck loop itself failed (the harness keeps going)."""

        for q in self.recheck_queue:
            if q["verified"]:
                self._late_row(q, {"outcome": "UNCERTAIN", "reasons": [f"recheck failed: {type(exc).__name__}: {exc}"[:300]]},
                               0.0, 0.0)
                q["verified"] = False

    def _late_row(self, q: dict[str, Any], late: dict[str, Any], waited: float, read_s: float) -> None:
        row = {"item_id": q["item_id"], **late, "mode": self._mode(),
               # PREREGISTRATION_2: the wait for the recheck to fall due, the
               # recheck's own read, and first verdict -> recheck verdict.
               "wait_s": round(waited, 2), "recheck_s": round(read_s, 2),
               "latency_s": round(time.time() - q["verdict_at"], 2)}
        with self.late_path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    @staticmethod
    def _mode() -> str:
        import os

        # run.py sets it from --run: blocking for run 2, boundary for the slice.
        return os.environ.get("INCREMENTAL_VALUE_RECHECK_MODE", "blocking")

    def close(self) -> None:
        if getattr(self, "recheck_queue", None):
            self.run_rechecks(wait=True)
        super().close()

    def _on_finish_1(self, claim: Optional[dict[str, Any]], item: WorkItem, evidence: Path) -> dict[str, Any]:
        from openadapt_flow.verifier import Actor, VerifierError, verify_write

        said_saved = claim is not None and claim.get("result") == "SAVED"
        attempted = said_saved or self.saves_sent > 0
        try:
            result = verify_write(
                self.contract(),
                self._before,
                params=self._params,
                actor=Actor("agent", "openadapt_actor"),
                write_attempted=attempted,
                authorization_ref=item.item_id,
            )
        except VerifierError as exc:
            return {"claimed": "stopped", "status": f"OpenAdapt: RECONCILIATION_REQUIRED ({exc})", "verifier": {"error": str(exc)[:300]}}
        v = {
            "status": result.status,
            "outcome": result.outcome,
            "checked": result.checked,
            "reasons": result.reasons[:6],
            "receipt_sha256": result.receipt_sha256,
            "capture_s": getattr(self, "_capture_s", None),
            "write_attempted": attempted,
        }
        (evidence / "receipt.json").write_text(json.dumps(result.receipt, indent=1, default=str))
        (evidence / "evidence.json").write_text(json.dumps(result.evidence, indent=1, default=str)[:400_000])
        if attempted and result.verified:
            # The record shows exactly the declared write, whatever the agent
            # concluded after a timeout or a restart.
            return {"claimed": "done", "status": f"OpenAdapt: verified {result.receipt_sha256[:12]}", "verifier": v}
        if not said_saved and not attempted and result.verified:
            reason = claim.get("reason", "") if claim else "no finish"
            return {"claimed": "stopped", "status": f"RESULT: NOT SAVED: {reason} (OpenAdapt: nothing moved)", "verifier": v}
        label = "REFUTED" if result.status == "refuted" else "RECONCILIATION_REQUIRED"
        return {"claimed": "stopped", "status": f"OpenAdapt: {label}: {'; '.join(result.reasons)[:300]}", "verifier": v}


def _epoch(stamp: Any) -> float:
    """An ISO-8601 UTC stamp as epoch seconds, or 0.0."""

    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def factory_a(options: dict[str, Any]) -> ArmA:
    return ArmA(options)


def factory_b(options: dict[str, Any]) -> ArmB:
    return ArmB(options)
