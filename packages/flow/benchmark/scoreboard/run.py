"""Run one arm of the scoreboard on one task.

    cd packages/flow/benchmark
    python -m scoreboard.run agent mockmed --run-id 2026-10-18-a --n 100
    python -m scoreboard.run first_run mockmed --run-id 2026-10-18-a
    python -m scoreboard.run program mockmed --run-id 2026-10-18-a --n 100
    python -m scoreboard.run agent openimis --run-id 2026-10-18-a --n 20

Arms (PROTOCOL.md):

* ``agent``: arm 1, the agent alone (Claude Sonnet 5.5 computer use) on a
  plain browser. Nothing records or checks it.
* ``first_run``: arm 2, the agent's first run under OpenAdapt: recorded by
  the JIT wrapper, read back, compiled, and taken through admission.
* ``program``: arm 3, the compiled program driving the UI.
* ``code_agent``: arm 7, the code-as-action agent's first run
  (``ClaudeAgent(vision="code")`` in the compile-agent harness), recorded by
  the same JIT wrapper as arm 2, read back, compiled and taken through the
  same admission. ``--n`` first runs, each on a fresh cache.

Every attempt is one row in ``raw/<task>/<arm>/<run id>/items.csv``. The
outcome comes from reading the system of record after the attempt.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402
from scoreboard.agent import (  # noqa: E402
    SYSTEM_TEMPLATE,
    ClaudeComputerUseAgent,
    PlainComputer,
    PlainRequest,
)

AGENT_MODEL = "claude-sonnet-5-5"
SPEND_FILE = HERE / "raw" / "spend.json"
# Claude API spend on this key before the scoreboard existed: the five
# launch-demo recordings (demo/cua_to_program/results/launch-*/agent_ledger.json).
PRIOR_SPEND_USD = 0.577
SPEND_CAP_USD = 30.0
FAILURE_WORDS = (
    "unable",
    "could not",
    "couldn't",
    "cannot",
    "can't",
    "failed",
    "error",
)
REVIEWER = "scoreboard-reviewer@example.invalid"
ITEM_TIMEOUT_S = 600


class ItemTimeout(Exception):
    pass


def _item_timeout(signum: int, frame: Any) -> None:
    raise ItemTimeout(f"item exceeded {ITEM_TIMEOUT_S} s")


# -- spend ------------------------------------------------------------------------


def spent() -> float:
    if SPEND_FILE.is_file():
        return float(json.loads(SPEND_FILE.read_text())["total_usd"])
    return PRIOR_SPEND_USD


def add_spend(usd: float, note: str) -> None:
    data = (
        json.loads(SPEND_FILE.read_text())
        if SPEND_FILE.is_file()
        else {
            "total_usd": PRIOR_SPEND_USD,
            "entries": [{"usd": PRIOR_SPEND_USD, "note": "launch-demo recordings"}],
        }
    )
    data["total_usd"] = round(data["total_usd"] + usd, 6)
    data["entries"].append({"usd": round(usd, 6), "note": note, "at": time.time()})
    SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPEND_FILE.write_text(json.dumps(data, indent=2) + "\n")


def model_key() -> Any:
    from openadapt_flow.jit import load_customer_model_key

    return load_customer_model_key("anthropic")


# -- tasks ---------------------------------------------------------------------------


def task_module(name: str) -> Any:
    if name == "mockmed":
        from scoreboard.tasks import mockmed

        return mockmed
    if name == "openimis":
        from scoreboard.tasks import openimis

        return openimis
    if name == "openemr_referral":
        from scoreboard.tasks import openemr

        return openemr
    raise SystemExit(f"unknown task {name!r}")


class Session:
    """What one item needs: a URL to open, a reset, values, a read-back."""

    def __init__(
        self,
        task: Any,
        tag: str,
        faults: bool,
        port: Optional[int] = None,
        ui_change_at: Optional[int] = None,
    ) -> None:
        self.task = task
        self.ui_change_at = ui_change_at
        self.tag = tag
        self.faults = faults
        self.server = None
        if task.NAME == "mockmed":
            # Arms 2 and 3 share the fixed port the admission is bound to;
            # the agent alone needs no admission and takes any free port.
            self.server = task.Server() if port is None else task.Server(port=port)

    def item(self, i: int) -> Any:
        if self.server is not None:
            return self.task.item(i, faults=self.faults)
        from scoreboard.tasks.mockmed import Item

        change = self.ui_change_at is not None and i >= self.ui_change_at
        return Item(
            item=i,
            values=self.task.item_values(i, self.tag),
            fault="ui_change" if change else "none",
            query_fault="ok",
            drift="",
        )

    def url(self, it: Any) -> str:
        if self.server is not None:
            return self.server.url(it)
        return self.task.claim_form_url()

    def before(self, it: Any) -> None:
        self.observed = ""
        if self.server is not None:
            self.server.reset()
            return
        # openIMIS: count this insuree's claims first, so a claim saved under
        # a mistyped claim number still reads back as a wrong save.
        ins = it.values["insurance_no"]
        self._insuree_claims = sum(
            1 for r in self.task.read_claims() if r["insurance_no"] == ins
        )
        if self.ui_change_at is not None:
            # The UI change is a script injected into the running app
            # (tasks/openimis.ui_change); it affects every client.
            want = it.fault == "ui_change"
            if want != self.task.ui_change_is_on():
                self.task.ui_change(want)
        # Record what the app actually served for this item, not only what was
        # intended: another run on the same app could have toggled it.
        self.observed = f"ui_change_on={self.task.ui_change_is_on()}; "

    def readback(self, it: Any) -> str:
        if self.server is not None:
            return self.task.check(self.server.records(), it.values)
        verdict = self.task.check(it.values)
        if verdict == "missing":
            ins = it.values["insurance_no"]
            now = sum(1 for r in self.task.read_claims() if r["insurance_no"] == ins)
            if now > getattr(self, "_insuree_claims", now):
                return "wrong"  # a claim landed, under another claim number
        return verdict

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


# -- arm 1: the agent alone ---------------------------------------------------------


def reported_success(
    agent: ClaudeComputerUseAgent, error: Optional[BaseException]
) -> bool:
    if error is not None or not agent.finished_at:
        return False
    text = (agent.final_text or "").lower()
    return not any(word in text for word in FAILURE_WORDS)


class _LedgerView:
    """The agent's numbers, read back from its ledger file."""

    def __init__(self, data: dict[str, Any], result: dict[str, Any]) -> None:
        calls = data.get("calls", [])
        self.calls = calls
        self.cost_usd = float(data.get("cost_usd", 0.0))
        self.final_text = result.get("final_text", data.get("final_text", "")) or ""
        self.started_at = data.get("started_at")
        self.finished_at = data.get("finished_at")
        self.tokens = {
            "input": sum(c["input_tokens"] for c in calls),
            "output": sum(c["output_tokens"] for c in calls),
            "cache_read": sum(c["cache_read_input_tokens"] for c in calls),
            "cache_write": sum(c["cache_creation_input_tokens"] for c in calls),
        }


def _ledger_view(ledger: Path, result: dict[str, Any]) -> _LedgerView:
    data = json.loads(ledger.read_text()) if ledger.is_file() else {}
    return _LedgerView(data, result)


def _agent_item(
    task_name: str, url: str, instruction: str, ledger: str, max_cost: float, box: Any
) -> None:
    """One agent-alone item, in a child process (see arm_agent)."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    task = task_module(task_name)
    agent = ClaudeComputerUseAgent(
        ledger_path=Path(ledger),
        model=AGENT_MODEL,
        system=SYSTEM_TEMPLATE.format(app=task.APP),
        max_cost_usd=max_cost,
    )
    error = None
    backend, close = PlaywrightBackend.launch(url, headless=True)
    try:
        task.setup(backend.page)
        agent.run(
            PlainRequest(instruction, model_key=model_key()),
            PlainComputer(backend.page),
        )
    except Exception as exc:  # noqa: BLE001 - recorded in the row
        error = f"{type(exc).__name__}: {exc}"
    finally:
        box.put({"error": error, "final_text": agent.final_text})
        close()


def arm_agent(args: argparse.Namespace) -> int:

    task = task_module(args.task)
    out = schema.run_dir(task.NAME, "agent", args.run_id)
    schema.write_meta(
        out,
        task=task.NAME,
        arm="agent",
        model=AGENT_MODEL,
        n=args.n,
        faults=args.faults,
        max_cost_per_item=args.max_cost,
    )
    sess = Session(task, args.tag, args.faults, port=0, ui_change_at=args.ui_change_at)
    model_key()  # fail fast without the customer key; each item reloads it
    try:
        for i in range(args.start, args.n + 1):
            if spent() + args.max_cost > SPEND_CAP_USD:
                print(
                    f"stopping: spend {spent():.2f} + {args.max_cost} would pass the cap"
                )
                break
            it = sess.item(i)
            # A harness failure before the agent's first model call (a browser
            # that hangs at launch or sign-in) is not the agent's doing: retry
            # the item, up to 3 attempts. Every attempt is a row.
            for attempt in range(1 + args.attempt_offset, 4 + args.attempt_offset):
                sess.before(it)
                load0 = schema.load_1m()
                w0 = time.time()
                ledger = (
                    out
                    / "ledgers"
                    / (
                        f"item-{i:03d}.json"
                        if attempt == 1 and not args.attempt_offset
                        else f"item-{i:03d}-{attempt}.json"
                    )
                )
                # Each item runs in its own process: a browser call that blocks
                # (it happened, for an hour, at launch and at close) is killed
                # after ITEM_TIMEOUT_S and becomes an error row.
                ctx = multiprocessing.get_context("spawn")
                box = ctx.Queue()
                proc = ctx.Process(
                    target=_agent_item,
                    args=(
                        task.NAME,
                        sess.url(it),
                        task.instruction(it.values),
                        str(ledger),
                        args.max_cost,
                        box,
                    ),
                )
                proc.start()
                proc.join(ITEM_TIMEOUT_S)
                result: dict[str, Any] = {}
                if proc.is_alive():
                    proc.terminate()
                    proc.join(10)
                    if proc.is_alive():
                        proc.kill()
                    result["error"] = f"ItemTimeout: item exceeded {ITEM_TIMEOUT_S} s"
                else:
                    try:
                        result = box.get(timeout=5)
                    except Exception:  # noqa: BLE001
                        result = {"error": f"the item process exited {proc.exitcode}"}
                agent = _ledger_view(ledger, result)
                error = result.get("error")
                wall = time.time() - w0
                add_spend(agent.cost_usd, f"{task.NAME} agent {args.run_id} item {i}")
                readback = sess.readback(it)
                ok = (not error) and reported_success(agent, None)
                tok = agent.tokens
                item_s = (agent.finished_at or time.time()) - (agent.started_at or w0)
                row = schema.Row(
                    task=task.NAME,
                    arm="agent",
                    run_id=args.run_id,
                    item=i,
                    attempt=attempt,
                    fault=it.fault,
                    reported_success=ok,
                    readback=readback,
                    outcome=schema.classify(ok, readback, it.fault),
                    model_calls=len(agent.calls),
                    input_tokens=tok["input"],
                    output_tokens=tok["output"],
                    cache_read_tokens=tok["cache_read"],
                    cache_write_tokens=tok["cache_write"],
                    cost_usd=round(agent.cost_usd, 6),
                    item_s=round(item_s, 3),
                    wall_s=round(wall, 3),
                    approvals_asked=0,
                    host_load_1m=load0,
                    timed=schema.timed(load0, schema.load_1m()),
                    detail=(sess.observed + (error if error else agent.final_text))[
                        :300
                    ],
                )
                schema.append(out / "items.csv", row)
                print(
                    f"{task.NAME} agent {i:3d} {row.fault:12s} {row.outcome:13s} "
                    f"{row.item_s:6.1f}s ${row.cost_usd:.3f} load {load0}",
                    flush=True,
                )
                if not (error and not agent.calls):
                    break
    finally:
        sess.close()
    return 0


# -- arms 2 and 3 on MockMed, through the JIT wrapper -------------------------------


def _mockmed_jit(
    sess: Session, cache: Path, it: Any, agent: Any = None, dialect: str = "anthropic"
) -> Any:
    from openadapt_flow.jit import TaskSignature, WebApp, compile_agent
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from scoreboard.tasks import mockmed

    server = sess.server
    return compile_agent(
        agent,
        task=TaskSignature(mockmed.TASK_ID, mockmed.INSTRUCTION, mockmed.PARAMS),
        app=WebApp(
            url=server.url(it),
            record_reader=server.records,
            reset=server.reset,
        ),
        verifier=RestRecordVerifier(
            server.base, records_path="/api/db", records_key="records"
        ),
        policy="clinical-write",
        cache_dir=cache,
        key_dir=cache / "admission-keys",
        model_key=model_key() if agent is not None else None,
        dialect=dialect,
    )


def _admitter(key_dir: Path) -> Any:
    from openadapt_flow.jit import authority

    os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, "scoreboard-only")
    pem = key_dir / "scoreboard-ci.pem"
    if not pem.is_file():
        key_dir.mkdir(parents=True, exist_ok=True)
        pub = authority.generate_encrypted_key(pem)
        authority.register_principal(
            key_dir,
            role="admitter_key",
            principal_id="ci:scoreboard",
            public_key_hex=pub,
        )
    return authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)


def _openemr_jit(
    cache: Path, values: dict[str, str], agent: Any = None, dialect: str = "anthropic"
) -> Any:
    from openadapt_flow.jit import TaskSignature, WebApp, compile_agent
    from scoreboard.tasks import openemr as T

    return compile_agent(
        agent,
        task=TaskSignature(T.TASK_ID, T.INSTRUCTION, {p: str for p in T.PARAMS}),
        app=WebApp(
            url=T.entry_url(),
            record_reader=T.patients,
            setup=T.setup,
            capture_settle_timeout_s=10.0,
        ),
        verifier=T.oracle_verifier(values),
        policy="clinical-write",
        cache_dir=cache,
        key_dir=cache / "admission-keys",
        model_key=model_key() if agent is not None else None,
        dialect=dialect,
    )


def _admission(
    jit: Any, params: list[str], canary_values: dict[str, str], cache: Path
) -> dict[str, Any]:
    """Confirm parameters, review, attended canary, admit; record what happens."""

    out: dict[str, Any] = {}
    try:
        confirmed = jit.confirm_params(
            principal_id=REVIEWER, accept_params=params, confirm="ok"
        )
        jit.review(
            principal_id=REVIEWER, reviewed_steps=confirmed["steps"], confirm="ok"
        )
        out["steps"] = len(confirmed["steps"])
        canary = jit.canary(principal_id=REVIEWER, **canary_values)
        out.update(
            canary_status=canary.status,
            canary_detail=canary.detail,
            canary_halt=canary.halt_reason,
        )
        if canary.verified:
            jit.admit_with_key(_admitter(cache / "admission-keys"))
            out["admitted"] = True
    except Exception as exc:  # noqa: BLE001 - the refusal is the result
        out["admission_error"] = f"{type(exc).__name__}: {exc}"[:600]
    out.setdefault("admitted", False)
    candidate = jit.candidate() or {}
    out["params"] = candidate.get("params")
    return out


def arm_first_run(args: argparse.Namespace) -> int:
    task = task_module(args.task)
    out = schema.run_dir(task.NAME, "first_run", args.run_id)
    cache = schema.cache_dir(task.NAME, "first_run", args.run_id)
    if cache.exists():
        shutil.rmtree(cache)
    schema.write_meta(out, task=task.NAME, arm="first_run", model=AGENT_MODEL)
    agent = ClaudeComputerUseAgent(
        ledger_path=out / "ledger.json",
        model=AGENT_MODEL,
        system=SYSTEM_TEMPLATE.format(app=task.APP),
        max_cost_usd=args.max_cost,
    )
    sess = None
    if task.NAME == "mockmed":
        sess = Session(task, args.tag, faults=False)
        it = sess.item(0)
        values, canary_values, params = it.values, sess.item(999).values, ["note"]
        jit = _mockmed_jit(sess, cache, it, agent)
        readback_fn = lambda: sess.readback(it)  # noqa: E731
    elif task.NAME == "openemr_referral":
        values, canary_values = task.item_values(0), task.item_values(999)
        params = list(task.PARAMS)
        jit = _openemr_jit(cache, values, agent)
        before_ids = {p["id"] for p in task.patients()}
        readback_fn = lambda: task.check(values, before_ids)  # noqa: E731
    else:
        raise SystemExit(
            "openIMIS arm 2 is imported from the launch demo (importers.py)"
        )
    try:
        load0 = schema.load_1m()
        w0 = time.time()
        result = jit.run(**values)
        wall = time.time() - w0
        add_spend(agent.cost_usd, f"{task.NAME} first_run {args.run_id}")
        readback = readback_fn()
        summary: dict[str, Any] = {
            "record_status": result.status,
            "detail": result.detail,
        }
        if result.candidate is not None:
            summary.update(_admission(jit, params, canary_values, cache))
        (out / "first_run.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n"
        )
        ok = result.status == "needs_human_admit" and reported_success(agent, None)
        tok = agent.tokens
        row = schema.Row(
            task=task.NAME,
            arm="first_run",
            run_id=args.run_id,
            item=0,
            attempt=1,
            fault="none",
            reported_success=ok,
            readback=readback,
            outcome=schema.classify(ok, readback, "none"),
            model_calls=len(agent.calls),
            input_tokens=tok["input"],
            output_tokens=tok["output"],
            cache_read_tokens=tok["cache_read"],
            cache_write_tokens=tok["cache_write"],
            cost_usd=round(agent.cost_usd, 6),
            item_s=round(
                (agent.finished_at or time.time()) - (agent.started_at or w0), 3
            ),
            wall_s=round(wall, 3),
            approvals_asked=1 if summary.get("admitted") else 0,
            host_load_1m=load0,
            timed=schema.timed(load0, schema.load_1m()),
            detail=json.dumps(
                {
                    k: summary.get(k)
                    for k in (
                        "record_status",
                        "steps",
                        "canary_status",
                        "admitted",
                        "admission_error",
                    )
                },
                default=str,
            )[:300],
        )
        schema.append(out / "items.csv", row)
        print(json.dumps(summary, indent=2, default=str)[:3000])
    finally:
        if sess is not None:
            sess.close()
    return 0


def arm_program(args: argparse.Namespace) -> int:
    task = task_module(args.task)
    if task.NAME != "mockmed":
        raise SystemExit(
            "the openIMIS program arm is imported from the launch demo (importers.py)"
        )
    out = schema.run_dir(task.NAME, "program", args.run_id)
    cache = schema.cache_dir(task.NAME, "first_run", args.program_from)
    if not cache.is_dir():
        raise SystemExit(f"no admitted program in {cache}; run first_run first")
    schema.write_meta(
        out, task=task.NAME, arm="program", program_from=args.program_from, n=args.n
    )
    sess = Session(task, args.tag, args.faults)
    paused_from: Optional[int] = None
    try:
        for i in range(args.start, args.n + 1):
            it = sess.item(i)
            if paused_from is not None:
                schema.append(
                    out / "items.csv",
                    schema.Row(
                        task=task.NAME,
                        arm="program",
                        run_id=args.run_id,
                        item=i,
                        attempt=0,
                        fault=it.fault,
                        reported_success=False,
                        readback="not_checked",
                        outcome="paused",
                        model_calls=0,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        cost_usd=0.0,
                        item_s=0.0,
                        wall_s=0.0,
                        approvals_asked=0,
                        host_load_1m=schema.load_1m(),
                        timed=False,
                        detail=f"batch paused for a person at item {paused_from}",
                    ),
                )
                continue
            for attempt in range(1, args.max_attempts + 1):
                jit = _mockmed_jit(sess, cache, it)
                load0 = schema.load_1m()
                w0 = time.time()
                result = jit.run(**it.values)  # resets the store first
                wall = time.time() - w0
                readback = sess.readback(it)
                ok = result.verified
                report = result.report
                total_ms = (
                    getattr(report, "total_ms", None) if report is not None else None
                )
                row = schema.Row(
                    task=task.NAME,
                    arm="program",
                    run_id=args.run_id,
                    item=i,
                    attempt=attempt,
                    fault=it.fault,
                    reported_success=ok,
                    readback=readback,
                    outcome=schema.classify(ok, readback, it.fault),
                    model_calls=int(result.model_calls or 0),
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                    item_s=round((total_ms or 0) / 1000, 3),
                    wall_s=round(wall, 3),
                    approvals_asked=0,
                    host_load_1m=load0,
                    timed=schema.timed(load0, schema.load_1m()),
                    detail=(result.halt_reason or result.detail or result.status)[:300],
                )
                schema.append(out / "items.csv", row)
                print(
                    f"mockmed program {i:3d}.{attempt} {row.fault:12s} {row.outcome:13s} "
                    f"{row.item_s:6.2f}s load {load0}",
                    flush=True,
                )
                if ok or readback != "missing":
                    break
            if args.pause_after_change_halt and it.fault == "ui_change" and not ok:
                paused_from = i
    finally:
        sess.close()
    return 0


# -- arm 7: the code-as-action agent's first run -----------------------------------

#: The identity the save gate checks around every save (compile-agent harness).
#: A new OpenEMR patient's identity is typed, not shown, so the gate checks the
#: save happens in the new-patient confirm dialog.
CODE_AGENT_ON_SCREEN = {
    "mockmed": ["Jane Sample", "MRN P1"],
    "openemr_referral": ["Create New Patient"],
}
#: OpenEMR items for arm 7 start here: clear of arm 2 (item 0, canary 999) and
#: of the charts a trial run made on the fixture (items 701 to 705).
OPENEMR_CODE_ITEM_BASE = 720


def _program_check(jit: Any, trace: Any) -> dict[str, Any]:
    from openadapt_flow.compile_agent.code_action import (
        check_trace_against_ir,
        distilled_events,
    )
    from openadapt_flow.ir import Workflow

    try:
        recording = (jit.candidate() or {}).get("compiled_recording")
        return check_trace_against_ir(
            trace.to_dict(),
            Workflow.load(jit.bundle_dir),
            distilled=distilled_events(Path(recording) if recording else None),
        )
    except Exception as exc:  # noqa: BLE001 - reported with the row
        return {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def arm_code_agent(args: argparse.Namespace) -> int:
    from openadapt_flow.compile_agent import ClaudeAgent, SaveGate
    from openadapt_flow.compile_agent.harness import record_readback
    from openadapt_flow.compile_agent.pipeline import _HarnessedAgent

    task = task_module(args.task)
    if task.NAME not in CODE_AGENT_ON_SCREEN:
        raise SystemExit("arm 7 runs on mockmed and openemr_referral")
    out = schema.run_dir(task.NAME, "code_agent", args.run_id)
    schema.write_meta(
        out,
        task=task.NAME,
        arm="code_agent",
        model=AGENT_MODEL,
        n=args.n,
        vision="code",
        effort="medium",
        max_cost_per_item=args.max_cost,
        on_screen=CODE_AGENT_ON_SCREEN[task.NAME],
    )
    # Arm 7 needs no admission bound to an origin, so it takes a free port,
    # unless it keeps a program for arm 3: that runs on the fixed origin.
    sess = (
        Session(
            task,
            args.tag,
            faults=args.faults,
            port=None if args.keep_program else 0,
        )
        if task.NAME == "mockmed"
        else None
    )
    on_screen = CODE_AGENT_ON_SCREEN[task.NAME]
    try:
        for i in range(args.start, args.n + 1):
            if spent() + args.max_cost > SPEND_CAP_USD:
                print(f"stopping: spend {spent():.2f} + {args.max_cost} passes the cap")
                break
            # Outside the source tree (schema.CACHE): recordings, bundles and a
            # throwaway admission key are never committed.
            cache = (
                schema.cache_dir(task.NAME, "code_agent", args.run_id) / f"item-{i:03d}"
            )
            if cache.exists():
                shutil.rmtree(cache)
            if sess is not None:
                it = sess.item(i)
                values, canary_values, params = (
                    it.values,
                    sess.item(999).values,
                    ["note"],
                )
                readback_fn = lambda: sess.readback(it)  # noqa: E731
                # Read after every save in run 1 (the harness's read-back). The
                # JIT resets the store before run 1, which leaves it empty.
                save_readback = record_readback(
                    sess.server.records,
                    {"patient_id": "p1", "type": "Triage"},
                    lambda: [],
                    values=values,
                )
                fault = it.fault
            else:
                values = task.item_values(OPENEMR_CODE_ITEM_BASE + i)
                canary_values = task.item_values(OPENEMR_CODE_ITEM_BASE + 100 + i)
                params = list(task.PARAMS)
                before = task.patients()
                before_ids = {p["id"] for p in before}
                readback_fn = lambda: task.check(values, before_ids)  # noqa: E731
                save_readback = record_readback(
                    task.patients,
                    {"email": values["email"], "lname": values["lname"]},
                    lambda: before,
                    # The state is typed as its label and stored as its code.
                    values={k: v for k, v in values.items() if k != "state_label"},
                )
                fault = "none"
            inner = ClaudeAgent(
                vision="code",
                model=AGENT_MODEL,
                effort="medium",
                max_cost_usd=args.max_cost,
                max_turns=60,
                principal_id=f"agent:{AGENT_MODEL}:code",
            )
            agent = _HarnessedAgent(
                inner,
                gate=SaveGate(on_screen),
                keep=[*on_screen, *values.values()],
                evidence_dir=cache / "gates",
                principal_id=inner.principal_id,
                readback=save_readback,
            )
            if sess is not None:
                jit = _mockmed_jit(sess, cache, it, agent, dialect="generic")
            else:
                jit = _openemr_jit(cache, values, agent, dialect="generic")
            error: Optional[BaseException] = None
            load0 = schema.load_1m()
            w0 = time.time()
            signal.signal(signal.SIGALRM, _item_timeout)
            signal.alarm(ITEM_TIMEOUT_S)
            try:
                result = jit.run(**values)
            except Exception as exc:  # noqa: BLE001 - recorded, the batch goes on
                error, result = exc, None
            finally:
                signal.alarm(0)
            wall = time.time() - w0
            load1 = schema.load_1m()
            ledger = getattr(inner, "last_ledger", None)
            cost = float(ledger.cost_usd()) if ledger is not None else 0.0
            add_spend(cost, f"{task.NAME} code_agent {args.run_id} item {i}")
            readback = readback_fn()
            trace = getattr(inner, "last_trace", None)
            summary: dict[str, Any] = {
                "record_status": getattr(result, "status", None),
                "detail": getattr(result, "detail", None)
                or (str(error) if error else None),
                "gates": [
                    {
                        k: v
                        for k, v in g.items()
                        if k in ("control", "decision", "missing")
                    }
                    for g in (agent.harness.gate_log if agent.harness else [])
                ],
                "readbacks": [
                    {k: v for k, v in r.items() if k != "at"}
                    for r in (agent.harness.readback_log if agent.harness else [])
                ],
            }
            if result is not None and result.candidate is not None:
                summary["program_check"] = _program_check(jit, trace)
                summary.update(_admission(jit, params, canary_values, cache))
            text = (ledger.summary or "") if ledger is not None else ""
            ok = (
                error is None
                and result is not None
                and result.status == "needs_human_admit"
                and ledger is not None
                and ledger.finished
                and not any(w in text.lower() for w in FAILURE_WORDS)
                # Amendment 11: the arm reports success only when its own
                # read-back after the last save shows the save landed.
                and bool(summary["readbacks"])
                and summary["readbacks"][-1]["outcome"] == "landed"
            )
            ledger_doc = {
                "ledger": ledger.to_dict() if ledger is not None else None,
                "harness": agent.harness.stats.to_dict() if agent.harness else None,
                "trace": trace.summary() if trace is not None else None,
                "scripts": trace.log if trace is not None else None,
                "result": summary,
            }
            (out / "ledgers").mkdir(parents=True, exist_ok=True)
            (out / "ledgers" / f"item-{i:03d}.json").write_text(
                json.dumps(ledger_doc, indent=2, default=str) + "\n"
            )
            row = schema.Row(
                task=task.NAME,
                arm="code_agent",
                run_id=args.run_id,
                item=i,
                attempt=1,
                fault=fault,
                reported_success=ok,
                readback=readback,
                outcome=schema.classify(ok, readback, fault),
                model_calls=ledger.model_calls if ledger is not None else 0,
                input_tokens=ledger.total("input_tokens") if ledger else 0,
                output_tokens=ledger.total("output_tokens") if ledger else 0,
                cache_read_tokens=ledger.total("cache_read_input_tokens")
                if ledger
                else 0,
                cache_write_tokens=(
                    ledger.total("cache_creation_input_tokens") if ledger else 0
                ),
                cost_usd=round(cost, 6),
                item_s=round(ledger.wall_seconds, 3) if ledger is not None else 0.0,
                wall_s=round(wall, 3),
                approvals_asked=1 if summary.get("admitted") else 0,
                host_load_1m=load0,
                timed=schema.timed(load0, load1),
                detail=json.dumps(
                    {
                        "record_status": summary.get("record_status"),
                        "program_check": (summary.get("program_check") or {}).get(
                            "passed"
                        ),
                        "canary_status": summary.get("canary_status"),
                        "admitted": summary.get("admitted"),
                        "readbacks": [r["outcome"] for r in summary["readbacks"]],
                        "error": f"{type(error).__name__}: {error}" if error else None,
                    },
                    default=str,
                )[:300],
            )
            schema.append(out / "items.csv", row)
            keep = (
                args.keep_program
                and i == args.start
                and summary.get("admitted")
                and sess is not None
            )
            if keep:
                # Arm 3 replays this program: `program --program-from code-<id>`.
                kept = schema.cache_dir(task.NAME, "first_run", f"code-{args.run_id}")
                if kept.exists():
                    shutil.rmtree(kept)
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cache), str(kept))
                print(f"kept the admitted program for arm 3: {kept}", flush=True)
            else:
                # The admission keys and cache stay out of the repository.
                shutil.rmtree(cache, ignore_errors=True)
            print(
                f"{task.NAME} code_agent {i:3d} {row.outcome:13s} {row.model_calls} calls "
                f"{row.item_s:6.1f}s ${row.cost_usd:.3f} load {load0}->{load1} "
                f"admitted={summary.get('admitted')} "
                f"program={(summary.get('program_check') or {}).get('passed')}",
                flush=True,
            )
    finally:
        if sess is not None:
            sess.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("arm", choices=["agent", "first_run", "program", "code_agent"])
    parser.add_argument("task", choices=["mockmed", "openimis", "openemr_referral"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--tag", default="SB", help="openIMIS claim-number prefix")
    parser.add_argument("--max-cost", type=float, default=0.75, help="per item, USD")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--attempt-offset",
        type=int,
        default=0,
        help="agent arm: number this run's attempts after ones already recorded",
    )
    parser.add_argument("--no-faults", dest="faults", action="store_false")
    parser.add_argument("--program-from", default=None, help="first_run id")
    parser.add_argument("--pause-after-change-halt", action="store_true")
    parser.add_argument(
        "--keep-program",
        action="store_true",
        help=(
            "code_agent on mockmed: keep the first item's admitted program (on the "
            "fixed origin) for `program --program-from code-<run id>`"
        ),
    )
    parser.add_argument(
        "--ui-change-at",
        type=int,
        default=None,
        help="openIMIS: apply the Save relabel from this item on (MockMed: fixed at 50)",
    )
    args = parser.parse_args()
    if args.arm == "program" and not args.program_from:
        args.program_from = args.run_id
    return {
        "agent": arm_agent,
        "first_run": arm_first_run,
        "program": arm_program,
        "code_agent": arm_code_agent,
    }[args.arm](args)


if __name__ == "__main__":
    raise SystemExit(main())
