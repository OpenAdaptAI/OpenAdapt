"""Guard an agent you already run: measured on MockMed, synthetic, no model calls.

The outside agent is a scripted Playwright "agent" (:mod:`.agent`) in its own
Chromium. The only change to it is the DevTools port. For each scenario and
arm it adds a Triage note to patient p1 in MockMed (:mod:`.app`):

* ``agent_alone``: no OpenAdapt. What lands, and what the agent's screen says.
* ``guard``: ``openadapt-flow guard`` (a separate process, as a developer runs
  it) attached over CDP, fail-closed.
* ``observe_only``: the same guard with ``--observe-only``.

Scenarios: the ten ``benchmark/effect_e2e`` fault classes injected at the
store; three that MockMed's page delivers (it sends the save twice, paints
success before the server answers, gives up after 1.2 s); and two the agent
makes (it opens the wrong patient, or types another note than the task's).
Every run is judged by the independent ground truth of ``effect_e2e``, which
reads the SQLite file directly. The page runs with ``idempotency=demo`` (a
stable key on the save), as #1335's teach test does: the ``clinical-write``
policy refuses to run a compiled save without one.

Run 2: the first ``ok`` guarded run is handed to the teach dir, a person (a
named stand-in here; this is the step a person takes) confirms the proposed
record check and admits the draft after an attended canary, and the program
is called as a ``taught_*`` tool over an MCP session for each store fault.

    python -m benchmark.agent_guard.run --n 3

Writes ``results.json`` and ``RESULTS.md`` next to this file. Localhost only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from benchmark.agent_guard.agent import ScriptedAgent
from benchmark.agent_guard.app import App, serve
from benchmark.effect_e2e import ground_truth

__all__ = ["SCENARIOS", "Scenario", "outcome", "run_benchmark"]

HERE = Path(__file__).resolve().parent
NOTE = "Agent guard benchmark triage note"
OTHER_NOTE = "A note that belongs to another task"
PERSON = "Bench Reviewer"
ARMS = ("agent_alone", "guard", "observe_only")
#: The guard's progress line for its recording (``openadapt-flow guard``).
_KEPT = re.compile(r"guard: recording kept (\d+) browser events")


@dataclass(frozen=True)
class Scenario:
    name: str
    family: str
    blurb: str
    store_fault: Optional[str] = "ok"
    page_fault: str = "ok"
    seed: bool = False
    patient: str = "p1"
    note: str = NOTE


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("ok", "effect_e2e", "a clean accepted write"),
    Scenario("no_persist", "effect_e2e", "2xx, nothing persisted", "no_persist"),
    Scenario("partial", "effect_e2e", "row persisted, note dropped", "partial"),
    Scenario("duplicate", "effect_e2e", "one request lands twice", "duplicate"),
    Scenario("wrong_record", "effect_e2e", "the write lands on p2", "wrong_record"),
    Scenario(
        "stale",
        "effect_e2e",
        "last write wins over a concurrent row",
        "stale",
        seed=True,
    ),
    Scenario(
        "collateral_unaudited",
        "effect_e2e",
        "right row, plus a stray billing row",
        "collateral_unaudited",
    ),
    Scenario("optimistic", "effect_e2e", "server refuses with 409", "optimistic"),
    Scenario("session", "effect_e2e", "session expired (401)", "session"),
    Scenario("timeout", "effect_e2e", "commits, answers after 1.5 s", "timeout"),
    Scenario(
        "page_duplicate",
        "page",
        "the page sends the save twice",
        store_fault=None,
        page_fault="duplicate",
    ),
    Scenario(
        "page_optimistic",
        "page",
        "the page paints success, the server refuses",
        store_fault=None,
        page_fault="optimistic",
    ),
    Scenario(
        "page_timeout",
        "page",
        "the page gives up after 1.2 s; the row commits",
        store_fault=None,
        page_fault="timeout",
    ),
    Scenario("agent_wrong_patient", "agent", "the agent opens p2", patient="p2"),
    Scenario(
        "agent_wrong_value", "agent", "the agent types another note", note=OTHER_NOTE
    ),
)


# -- files the developer writes -------------------------------------------------


def write_setup(
    work: Path,
    base: str,
    *,
    teach_dir: Optional[Path],
    db_path: Optional[Path] = None,
) -> dict[str, Path]:
    """verify.yaml, guard.yaml and task.json, as a developer writes them."""

    work.mkdir(parents=True, exist_ok=True)
    verify = work / "verify.yaml"
    verify.write_text(
        "schema_version: openadapt.verify/v1\n"
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/records\n"
        "  records_key: records\n"
        "effects:\n"
        "  - kind: record_written\n"
        "    match:\n"
        "      patient_id: {param: patient_id}\n"
        "      type: {param: type}\n"
        "      note: {param: note}\n"
        "    expected_count: 1\n"
        "    count_new_only: true\n"
        "    forbid_collateral_loss: true\n"
        "    timeout_s: 1.0\n"
    )
    guard = work / "guard.yaml"
    teach = (
        "teach:\n"
        f"  dir: {teach_dir}\n"
        "  name: triage_note\n"
        + (f"  db: {db_path}\n" if db_path else "")
        + f"  key_dir: {work / 'keys'}\n"
        if teach_dir
        else ""
    )
    guard.write_text(
        "schema_version: openadapt.agent-guard/v1\n"
        f"app_url: {base}/?fault=ok&idempotency=demo#tasks\n"
        "verify: verify.yaml\n"
        'screen_selector: "#patient-banner"\n' + teach
    )
    task = work / "task.json"
    task.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.guard-task/v1",
                "task_id": "triage-p1-0001",
                "expected_record": {"patient_id": "p1", "name": "Jane Sample"},
                "intended_fields": {"type": "Triage", "note": NOTE},
            },
            indent=2,
        )
        + "\n"
    )
    return {"verify": verify, "guard": guard, "task": task}


def setup_lines(files: dict[str, Path]) -> dict[str, int]:
    def count(path: Path) -> int:
        return sum(1 for line in path.read_text().splitlines() if line.strip())

    agent_src = (HERE / "agent.py").read_text().splitlines()
    return {
        # The one line the agent's browser launch changes (marked in agent.py).
        "agent_code": sum(
            1
            for line in agent_src
            if "# OpenAdapt guard" in line and "--remote-debugging-port" in line
        ),
        "guard_yaml": count(files["guard"]),
        "verify_yaml": count(files["verify"]),
        "task_json_per_task": count(files["task"]),
        "command": 1,
    }


# -- one run ---------------------------------------------------------------------


def _post(base: str, path: str, body: dict) -> None:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


class _GuardProcess:
    """``openadapt-flow guard`` in its own process, as a developer starts it."""

    def __init__(self, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openadapt_flow", "guard", *args, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.stderr: list[str] = []
        #: Browser events the guard's recording has kept a frame for.
        self.kept = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)
            self.lines.put(line)
            match = _KEPT.match(line)
            if match:
                self.kept = int(match.group(1))

    def wait_kept(self, events: int, timeout_s: float = 30.0) -> None:
        """Wait until the recording has kept ``events`` browser events.

        Past the timeout the agent acts anyway; the recorder then refuses
        the run, which the caller sees as ``no_recording``.
        """

        deadline = time.monotonic() + timeout_s
        while self.kept < events and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.05)

    def wait_attached(self, timeout_s: float = 90.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            if "guard: attached" in line:
                return
        raise RuntimeError("the guard did not attach:\n" + "".join(self.stderr))

    def finish(self, wait_s: float) -> dict[str, Any]:
        try:
            out, _ = self.proc.communicate(timeout=wait_s)
        except subprocess.TimeoutExpired:
            # No save went through (it was blocked): stop it as a person would.
            self.proc.send_signal(signal.SIGINT)
            try:
                out, _ = self.proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                out, _ = self.proc.communicate()
        try:
            return {"exit": self.proc.returncode, **json.loads(out)}
        except ValueError:
            return {"exit": self.proc.returncode, "error": "".join(self.stderr)[-2000:]}


def outcome(row: dict[str, Any]) -> str:
    gt_correct = row["truth"]["correct"]
    if row["arm"] == "agent_alone":
        saved = row["screen_after"] == "saved"
        if gt_correct:
            return "clean" if saved else "false_alarm"
        if row["truth"]["fault_class"] == "absent" and not saved:
            return "visible_error"
        return "silent_wrong" if saved else "visible_error"
    blocked = row["blocked"]
    if blocked and (
        gt_correct or (row["truth"]["fault_class"] == "absent" and row["writes"] == 0)
    ):
        return "blocked"
    if gt_correct:
        return "clean" if row["verdict"] == "VERIFIED" else "false_alarm"
    return "got_through" if row["verdict"] == "VERIFIED" else "caught_by_readback"


def run_one(
    app: App,
    scenario: Scenario,
    arm: str,
    work: Path,
    port: int,
    *,
    think_s: float,
    files: dict[str, Path],
    teach: bool,
) -> dict[str, Any]:
    _post(
        app.base_url,
        "/api/reset",
        {"seed_concurrent": scenario.seed, "fault": scenario.store_fault},
    )
    before = ground_truth.capture(app.db_path)
    agent = ScriptedAgent(port=port, think_s=think_s)
    guard: Optional[_GuardProcess] = None
    try:
        agent.open(
            f"{app.base_url}/?fault={scenario.page_fault}&idempotency=demo#tasks"
        )
        if arm != "agent_alone":
            args = [
                "--cdp",
                f"http://127.0.0.1:{port}",
                "--config",
                str(files["guard"]),
                "--task",
                str(files["task"]),
                "--out",
                str(work),
                "--timeout",
                "60",
            ]
            if arm == "observe_only":
                args.append("--observe-only")
            if not teach:
                args.append("--no-teach")
            guard = _GuardProcess(args)
            guard.wait_attached()
            if teach:
                # Act only once the recording has kept the last action.
                agent.before_action = guard.wait_kept
        started = time.monotonic()
        acted = agent.add_triage_note(patient=scenario.patient, note=scenario.note)
        # A handed-over run compiles the draft after the read-back.
        wait_s = 180.0 if teach else 8.0
        result = guard.finish(wait_s=wait_s) if guard is not None else {}
        run1_s = round(time.monotonic() - started, 3)
    finally:
        agent.close()
        if guard is not None and guard.proc.poll() is None:
            guard.proc.kill()
    time.sleep(0.2)
    after = ground_truth.capture(app.db_path)
    truth = ground_truth.judge(before, after, intended_note=NOTE)
    summary = (result.get("run") or {}) if result else {}
    row = {
        "scenario": scenario.name,
        "family": scenario.family,
        "arm": arm,
        "truth": {"correct": truth.correct, "fault_class": truth.fault_class},
        "screen_after": acted.screen_after,
        "agent_notice": acted.notice,
        "agent_actions": acted.actions,
        "agent_seconds": acted.seconds,
        "run1_seconds": run1_s,
        "writes": app.writes,
        "verdict": summary.get("verdict"),
        "blocked": [b["code"] for b in summary.get("blocked") or []],
        "would_block": [b["code"] for b in summary.get("would_block") or []],
        "hold_ms": summary.get("hold_ms") or [],
        "readback_ms": summary.get("readback_ms"),
        "guard_exit": result.get("exit") if result else None,
        "guard_seconds": summary.get("seconds"),
        "guard_ended": summary.get("ended"),
        "guard_notes": summary.get("notes") or [],
        "model_calls": 0,
        "load_avg_1m": round(os.getloadavg()[0], 2),
        "handoff": result.get("handoff") if result else None,
    }
    if result and "error" in result:
        row["guard_error"] = result["error"]
    row["outcome"] = outcome(row)
    return row


# -- run 2 -------------------------------------------------------------------------


def _person_admits(task_dir: Path) -> dict[str, Any]:
    """The person's terminal path: ``compile-agent admit``. They give their
    name, confirm the proposed record check, review the steps, attend the
    canary and admit. A named stand-in types the answers here, as in #1335's
    tests; a person's admission needs a terminal, which a script has none of.
    """

    import contextlib
    import io
    from unittest import mock

    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.cli import cmd_compile_agent

    answers = iter([PERSON, "ok", "ok", "ok"])
    args = build_parser().parse_args(
        [
            "compile-agent",
            "admit",
            str(task_dir),
            "--admit-as",
            PERSON,
            "--drop-field",
            "key",
            "--drop-field",
            "source",
            "--canary-param",
            "note=Canary triage note",
            "--json",
        ]
    )
    out = io.StringIO()
    with (
        mock.patch("openadapt_flow.jit.lifecycle._has_terminal", lambda: True),
        contextlib.redirect_stdout(out),
    ):
        code = cmd_compile_agent(
            args, ask=lambda _prompt: next(answers), interactive=lambda: True
        )
    text = out.getvalue()
    try:
        result = json.loads(text[text.index("\n{") + 1 :])
    except ValueError:
        result = {"status": "error", "output": text[-3000:]}
    result["exit"] = code
    return result


def run2(
    app: App,
    task_dir: Path,
    verify: Path,
    teach_dir: Path,
    runs: Path,
    *,
    pairs: int = 0,
    run1: Optional[Any] = None,
) -> dict:
    """A person confirms and admits; then the program is an MCP tool.

    With ``pairs``, ``run1()`` (a guarded run 1 of ``ok``) and one tool call
    alternate that many times, so the two are timed under the same load.
    """

    import anyio

    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.mcp import build_server
    from openadapt_flow.agent.runner import RunnerConfig
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig
    from openadapt_flow.compile_agent.pipeline import FirstRunCompiler

    _post(app.base_url, "/api/reset", {"fault": None})
    compiler = FirstRunCompiler.load(task_dir)
    proposed = compiler.proposed()
    started = time.monotonic()
    admitted = _person_admits(task_dir)
    admit_s = round(time.monotonic() - started, 3)
    if admitted.get("status") != "admitted":
        return {"admitted": False, "detail": admitted, "proposed": proposed}
    runs.mkdir(parents=True, exist_ok=True)
    workflows = AgentBridge(
        None, RunnerConfig(runs_dir=runs), allow_run=True, mode="production"
    )
    teach = TeachBridge(
        TeachConfig(verify=verify, cache_dir=teach_dir, url=compiler.url),
        workflows=workflows,
    )
    server = build_server(workflows, teach=teach)
    rows: list[dict[str, Any]] = []

    async def scenario() -> None:
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams

        async with create_client_server_memory_streams() as (client, transport):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server.run, *transport, server.create_initialization_options()
                )
                async with ClientSession(*client) as session:
                    await session.initialize()
                    tools = [t.name for t in (await session.list_tools()).tools]
                    (tool,) = [t for t in tools if t.startswith("taught_")]
                    for item in SCENARIOS:
                        if item.family != "effect_e2e":
                            continue
                        _post(
                            app.base_url,
                            "/api/reset",
                            {"seed_concurrent": item.seed, "fault": item.store_fault},
                        )
                        before = ground_truth.capture(app.db_path)
                        t0 = time.monotonic()
                        res = await session.call_tool(
                            tool,
                            {
                                "inputs": {"note": NOTE},
                                "request_id": f"bench-{item.name}-0001",
                            },
                        )
                        seconds = round(time.monotonic() - t0, 3)
                        payload = json.loads(res.content[0].text)
                        await anyio.sleep(0.2)
                        truth = ground_truth.judge(
                            before,
                            ground_truth.capture(app.db_path),
                            intended_note=NOTE,
                        )
                        done = payload.get("outcome") == "done"
                        if truth.correct:
                            result = "clean" if done else "false_alarm"
                        else:
                            result = "got_through" if done else "caught_by_readback"
                        rows.append(
                            {
                                "scenario": item.name,
                                "load_avg_1m": round(os.getloadavg()[0], 2),
                                "tool": tool,
                                "seconds": seconds,
                                "tool_outcome": payload.get("outcome"),
                                "tool_reply": {
                                    k: payload.get(k)
                                    for k in ("outcome", "reason", "error", "status")
                                    if payload.get(k) is not None
                                },
                                "model_calls": payload.get("model_calls"),
                                "truth": {
                                    "correct": truth.correct,
                                    "fault_class": truth.fault_class,
                                },
                                "outcome": result,
                            }
                        )
                    for k in range(pairs):
                        assert run1 is not None
                        first = await anyio.to_thread.run_sync(run1)
                        _post(app.base_url, "/api/reset", {"fault": None})
                        load = round(os.getloadavg()[0], 2)
                        t0 = time.monotonic()
                        res = await session.call_tool(
                            tool,
                            {
                                "inputs": {"note": NOTE},
                                "request_id": f"bench-pair-{k:04d}",
                            },
                        )
                        payload = json.loads(res.content[0].text)
                        paired.append(
                            {
                                "run1_agent_seconds": first["agent_seconds"],
                                "run1_outcome": first["outcome"],
                                "run1_load_avg_1m": first["load_avg_1m"],
                                "run2_seconds": round(time.monotonic() - t0, 3),
                                "run2_outcome": payload.get("outcome"),
                                "run2_model_calls": payload.get("model_calls"),
                                "run2_load_avg_1m": load,
                            }
                        )
                        print(f"pair {k}: {paired[-1]}", flush=True)
                tasks.cancel_scope.cancel()

    paired: list[dict[str, Any]] = []
    anyio.run(scenario)
    _post(app.base_url, "/api/reset", {"fault": None})
    return {
        "admitted": True,
        "admit_seconds": admit_s,
        "proposed": proposed,
        "admission": admitted.get("admission"),
        "runs": rows,
        "paired": paired,
    }


# -- the whole study ---------------------------------------------------------------


def run_benchmark(
    n: int,
    think_s: float,
    arms: tuple[str, ...],
    port: int,
    *,
    pairs: int = 0,
    previous: Optional[dict[str, Any]] = None,
) -> dict:
    """The matrix, then run 2. With ``previous`` (an earlier results.json),
    the matrix is kept from it and only run 2 (and the pairs) run again."""

    results: dict[str, Any] = {
        "schema": "openadapt.agent-guard-benchmark/v1",
        "date": time.strftime("%Y-%m-%d"),
        "n": n,
        "think_s": think_s,
        "arms": list(arms),
        "model_calls": 0,
    }
    with tempfile.TemporaryDirectory(prefix="agent-guard-bench-") as tmp:
        work = Path(tmp)
        app = serve(work / "app.sqlite")
        try:
            teach_dir = work / "teach"
            files = write_setup(
                work, app.base_url, teach_dir=teach_dir, db_path=app.db_path
            )
            plain = write_setup(work / "plain", app.base_url, teach_dir=None)
            results["setup_lines"] = setup_lines(files)
            results["setup_files"] = {
                name: path.read_text() for name, path in files.items()
            }
            rows: list[dict[str, Any]] = []
            handoff: Optional[dict[str, Any]] = None
            plan = (
                [(0, s, "guard") for s in SCENARIOS if s.name == "ok"]
                if previous is not None
                else [(t, s, a) for t in range(n) for s in SCENARIOS for a in arms]
            )
            for trial, scenario, arm in plan:
                port += 1
                teach = handoff is None and arm == "guard" and scenario.name == "ok"
                row = run_one(
                    app,
                    scenario,
                    arm,
                    work / "runs" / f"{scenario.name}-{arm}-{trial}",
                    port,
                    think_s=think_s,
                    files=files if teach else plain,
                    teach=teach,
                )
                row["trial"] = trial
                if teach:
                    handoff = row.get("handoff")
                rows.append(row)
                print(
                    f"{trial} {scenario.name:22s} {arm:13s} {row['outcome']:20s}"
                    f" {row['run1_seconds']:6.2f}s",
                    flush=True,
                )
            if previous is not None:
                results.update(
                    {
                        k: previous[k]
                        for k in ("n", "think_s", "arms", "date")
                        if k in previous
                    }
                )
                results["runs"] = previous["runs"]
                results["run2_rerun_date"] = time.strftime("%Y-%m-%d %H:%M")
            else:
                results["runs"] = rows
            results["handoff"] = handoff
            counter = {"port": port + 1000}

            def guarded_ok() -> dict[str, Any]:
                counter["port"] += 1
                ok = next(s for s in SCENARIOS if s.name == "ok")
                return run_one(
                    app,
                    ok,
                    "guard",
                    work / "pairs" / f"ok-{counter['port']}",
                    counter["port"],
                    think_s=think_s,
                    files=plain,
                    teach=False,
                )

            if handoff and handoff.get("status") == "draft":
                results["run2"] = run2(
                    app,
                    Path(handoff["task_dir"]),
                    files["verify"],
                    teach_dir,
                    work / "taught-runs",
                    pairs=pairs,
                    run1=guarded_ok,
                )
        finally:
            app.stop()
        # Paths under the temporary folder name nothing anyone can open later.
        results = json.loads(
            json.dumps(results, default=str).replace(str(work), "<work>")
        )
    results["summary"] = summarize(results)
    return results


def _median(values: list[float]) -> Optional[float]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return (
        values[mid]
        if len(values) % 2
        else round((values[mid - 1] + values[mid]) / 2, 3)
    )


def summarize(results: dict[str, Any]) -> dict[str, Any]:
    rows = results.get("runs") or []
    table: dict[str, dict[str, Any]] = {}
    for row in rows:
        cell = table.setdefault(row["scenario"], {}).setdefault(row["arm"], [])
        cell.append(row["outcome"])
    per_scenario = {
        s: {arm: sorted(set(v)) for arm, v in arms.items()} for s, arms in table.items()
    }
    counts: dict[str, dict[str, int]] = {}
    for scenario in SCENARIOS:
        if scenario.name == "ok":
            continue
        for arm, outcomes in per_scenario.get(scenario.name, {}).items():
            label = outcomes[0] if len(outcomes) == 1 else "mixed:" + "/".join(outcomes)
            key = f"{arm}:{scenario.family}"
            counts.setdefault(key, {})
            counts[key][label] = counts[key].get(label, 0) + 1
    ok = [r for r in rows if r["scenario"] == "ok"]
    timing = {
        arm: {
            "run1_seconds_median": _median(
                [r["run1_seconds"] for r in ok if r["arm"] == arm]
            ),
            "agent_actions": _median(
                [r["agent_actions"] for r in ok if r["arm"] == arm]
            ),
            "hold_ms_median": _median(
                [h for r in ok if r["arm"] == arm for h in r["hold_ms"]]
            ),
            "readback_ms_median": _median(
                [r["readback_ms"] for r in ok if r["arm"] == arm]
            ),
        }
        for arm in results.get("arms", [])
    }
    run2 = results.get("run2") or {}
    run2_rows = run2.get("runs") or []
    ok2 = [r for r in run2_rows if r["scenario"] == "ok"]
    return {
        "per_scenario": per_scenario,
        "counts": counts,
        "timing_ok": timing,
        "run2": {
            "admitted": run2.get("admitted"),
            "ok_seconds": ok2[0]["seconds"] if ok2 else None,
            "ok_model_calls": ok2[0]["model_calls"] if ok2 else None,
            "per_scenario": {r["scenario"]: r["outcome"] for r in run2_rows},
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--think", type=float, default=1.0)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--out", type=Path, default=HERE)
    parser.add_argument(
        "--pairs",
        type=int,
        default=0,
        help="alternate a guarded run 1 and a run-2 tool call this many times",
    )
    parser.add_argument(
        "--keep-matrix",
        type=Path,
        help="an earlier results.json: keep its matrix, run only run 2 again",
    )
    args = parser.parse_args(argv)
    arms = tuple(a for a in args.arms.split(",") if a)
    previous = json.loads(args.keep_matrix.read_text()) if args.keep_matrix else None
    results = run_benchmark(
        args.n, args.think, arms, args.port, pairs=args.pairs, previous=previous
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(json.dumps(results["summary"], indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
