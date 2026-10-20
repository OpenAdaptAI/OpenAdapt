"""The executor adapter, and the adapters that ship with the benchmark.

An executor is anything that can be told "do this task in the app at this
URL" and then says whether it succeeded: a computer-use agent, an agent
harness, a test script, an OpenAdapt program. The adapter is one method::

    class MyExecutor:
        name = "my-agent"

        def run(self, task: Task) -> Report:
            ...  # open task.url, do task.instruction, say what happened
            return Report(claimed_success=True, final_text="Saved the note.")

Pass it as ``--agent python:my_module:MyExecutor``. An executor in another
language uses ``--agent command:'<shell command>'`` instead: the command gets
the task in environment variables and writes its report as JSON.

The executor gets the page URL, the instruction and its parameters. It never
gets the fault, and the benchmark never reads anything from it except the
:class:`Report`. The score comes from the store.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

HERE = Path(__file__).resolve().parent
BENCHMARK_DIR = HERE.parent


@dataclass(frozen=True)
class Task:
    """What an executor is told. Deliberately no fault and no store access."""

    item: int
    url: str
    instruction: str
    params: dict[str, str]
    work_dir: Path


@dataclass
class Report:
    """What an executor says happened. The benchmark scores it, never trusts it."""

    claimed_success: bool
    final_text: str = ""
    model_calls: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Executor(Protocol):
    name: str

    def run(self, task: Task) -> Report: ...


# -- screen-script: a $0 executor that trusts the screen ------------------------


_READ_OUTCOME = """() => {
  const text = (id) => (document.getElementById(id)?.innerText || '').trim();
  return {banner: text('saved-banner'), error: text('save-error')};
}"""


class ScreenScript:
    """Clicks through the task with fixed selectors and believes the banner.

    This is the reference for "an executor that trusts the screen". It makes
    no model calls, so anyone can run the whole benchmark for $0. It is not a
    baseline from the scoreboard and is not reported as one.
    """

    name = "screen-script"

    def __init__(self, headed: bool = False) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not headed)

    def run(self, task: Task) -> Report:
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        # A missing control fails the item in seconds, not Playwright's 30 s.
        context.set_default_timeout(5000)
        try:
            page = context.new_page()
            page.goto(task.url, wait_until="networkidle")
            page.locator(".open-btn").first.click()
            page.locator("#new-encounter").click()
            page.locator("#type-triage").click()
            page.locator("#note").fill(task.params["note"])
            page.locator("#save-encounter").click()
            # One read of the page per poll: MockMed re-renders on save, so
            # an element found by one call can be gone by the next.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                seen = page.evaluate(_READ_OUTCOME)
                if seen["banner"]:
                    return Report(True, final_text=seen["banner"])
                if seen["error"]:
                    return Report(False, final_text=seen["error"])
                page.wait_for_timeout(100)
            return Report(False, final_text="no banner and no error within 5 s")
        except Exception as exc:  # noqa: BLE001 - an executor error is a row
            return Report(False, error=f"{type(exc).__name__}: {exc}")
        finally:
            context.close()

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


# -- command: any executor, any language ----------------------------------------


class Command:
    """Runs a shell command per item.

    Environment: ``SILENT_SAVES_URL``, ``SILENT_SAVES_INSTRUCTION``,
    ``SILENT_SAVES_PARAMS`` (JSON), ``SILENT_SAVES_ITEM`` and
    ``SILENT_SAVES_REPORT``, the path the command writes its report to::

        {"claimed_success": true, "final_text": "...",
         "model_calls": 7, "cost_usd": 0.05}

    No report file, or a report without ``claimed_success``, counts as a
    claimed failure, with the reason in the row.
    """

    def __init__(self, command: str, timeout_s: float = 600.0) -> None:
        self.argv = shlex.split(command)
        self.name = f"command:{self.argv[0]}"
        self.timeout_s = timeout_s

    def run(self, task: Task) -> Report:
        report_path = task.work_dir / "report.json"
        report_path.unlink(missing_ok=True)
        env = dict(
            os.environ,
            SILENT_SAVES_URL=task.url,
            SILENT_SAVES_INSTRUCTION=task.instruction,
            SILENT_SAVES_PARAMS=json.dumps(task.params),
            SILENT_SAVES_ITEM=str(task.item),
            SILENT_SAVES_REPORT=str(report_path),
        )
        try:
            proc = subprocess.run(
                self.argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return Report(False, error=f"timed out after {self.timeout_s:.0f} s")
        (task.work_dir / "stdout.txt").write_text(proc.stdout)
        (task.work_dir / "stderr.txt").write_text(proc.stderr)
        if not report_path.is_file():
            return Report(False, error=f"no report (exit {proc.returncode})")
        try:
            data = json.loads(report_path.read_text())
            claimed = data["claimed_success"]
        except (ValueError, KeyError) as exc:
            return Report(False, error=f"unreadable report: {exc}")
        return Report(
            claimed_success=claimed is True,
            final_text=str(data.get("final_text", ""))[:2000],
            model_calls=int(data.get("model_calls", 0) or 0),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
        )


# -- claude-computer-use: the scoreboard's agent, unchanged ---------------------


class ClaudeComputerUse:
    """The scoreboard's arm-1 agent (Claude Sonnet 5.5 computer use).

    It runs ``scoreboard.run._agent_item`` in a child process, exactly as the
    scoreboard's claims run does. Its spend goes to this benchmark's ledger
    (:mod:`.spend`), which stops before the arm's cap or the total stop. It
    costs money, so the runner refuses it without --allow-paid.
    """

    name = "claude-computer-use"
    paid = True

    def __init__(
        self, max_cost_usd: float = 0.25, arm: str = "agent", arm_cap_usd: float = 0.0
    ) -> None:
        if str(BENCHMARK_DIR) not in sys.path:
            sys.path.append(str(BENCHMARK_DIR))
        from scoreboard import run as scoreboard_run

        self._sb = scoreboard_run
        self.max_cost_usd = max_cost_usd
        self.arm = arm
        self.arm_cap_usd = arm_cap_usd

    def run(self, task: Task) -> Report:
        import multiprocessing

        from . import spend

        sb = self._sb
        spend.check(self.arm, self.max_cost_usd, self.arm_cap_usd)
        ledger = task.work_dir / "ledger.json"
        ctx = multiprocessing.get_context("spawn")
        box = ctx.Queue()
        proc = ctx.Process(
            target=sb._agent_item,
            args=(
                "mockmed",
                task.url,
                task.instruction,
                str(ledger),
                self.max_cost_usd,
                box,
            ),
        )
        proc.start()
        proc.join(sb.ITEM_TIMEOUT_S)
        result: dict[str, Any] = {}
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
            result = {"error": "ItemTimeout"}
        elif not box.empty():
            result = box.get()
        view = sb._ledger_view(ledger, result)
        spend.add(view.cost_usd, self.arm, f"item {task.item}")
        return Report(
            claimed_success=sb.reported_success(view, result.get("error")),
            final_text=(result.get("error") or view.final_text)[:2000],
            model_calls=len(view.calls),
            cost_usd=round(view.cost_usd, 6),
            error=result.get("error"),
        )


# -- openadapt-program: an admitted OpenAdapt program ---------------------------


class OpenAdaptProgram:
    """Replays an admitted MockMed program (``openadapt_flow.jit``), no model.

    ``cache`` is the JIT cache an admission was written to, such as the
    scoreboard's ``~/.cache/openadapt-scoreboard/mockmed/first_run/<run>/jit-cache``.
    An admission is bound to the origin its canary ran on, so run the
    benchmark with ``--port`` set to that origin's port (18765 for the
    scoreboard's). The program reads the store back itself and reports
    success only when that read-back confirms the save.
    """

    name = "openadapt-program"

    def __init__(self, cache: str, app: Any) -> None:
        self.cache = Path(cache).expanduser()
        if not (self.cache / "admission-keys").is_dir():
            raise SystemExit(f"no admitted program in {self.cache}")
        self.app = app

    def run(self, task: Task) -> Report:
        from openadapt_flow.jit import TaskSignature, WebApp, compile_agent
        from openadapt_flow.runtime.effects import RestRecordVerifier

        from .protocol import INSTRUCTION

        state = (
            self.app.fault,
            self.app.drift,
            self.app.slow_ms,
            self.app.idempotency_key,
        )
        jit = compile_agent(
            None,
            task=TaskSignature("mockmed.add_note", INSTRUCTION, {"note": str}),
            app=WebApp(
                url=task.url,
                record_reader=self.app.records,
                reset=lambda: self.app.prepare(*state),
            ),
            verifier=RestRecordVerifier(
                self.app.store_base, records_path="/api/db", records_key="records"
            ),
            policy="clinical-write",
            cache_dir=self.cache,
            key_dir=self.cache / "admission-keys",
            dialect="anthropic",
        )
        try:
            result = jit.run(**task.params)
        except Exception as exc:  # noqa: BLE001 - an executor error is a row
            return Report(False, error=f"{type(exc).__name__}: {exc}")
        return Report(
            claimed_success=bool(result.verified),
            final_text=str(result.halt_reason or result.detail or result.status)[:2000],
            model_calls=int(result.model_calls or 0),
        )


def load(
    spec: str,
    *,
    app: Any,
    headed: bool = False,
    max_cost_usd: float = 0.25,
    arm: str = "",
    arm_cap_usd: float = 0.0,
) -> Any:
    """Build an executor from ``--agent``."""

    kind, _, rest = spec.partition(":")
    builders: dict[str, Callable[[], Any]] = {
        "screen-script": lambda: ScreenScript(headed=headed),
        "claude-computer-use": lambda: ClaudeComputerUse(
            max_cost_usd=max_cost_usd, arm=arm or "agent", arm_cap_usd=arm_cap_usd
        ),
        "command": lambda: Command(rest),
        "openadapt-program": lambda: OpenAdaptProgram(rest, app),
        "python": lambda: _python(rest),
        "self-repair": lambda: _self_repair(
            app, rest, arm or "program_repair", arm_cap_usd, max_cost_usd
        ),
        "compile-agent": lambda: _compile_agent(
            app, rest, arm or "compile_agent", arm_cap_usd, max_cost_usd
        ),
    }
    if kind not in builders:
        raise SystemExit(
            f"unknown --agent {spec!r}; use one of: screen-script, "
            "claude-computer-use, compile-agent, self-repair, "
            "openadapt-program:<jit cache>, "
            "command:<shell command>, python:<module>:<class>"
        )
    if kind in ("command", "openadapt-program", "python") and not rest:
        raise SystemExit(f"--agent {kind}: needs a value after the colon")
    return builders[kind]()


def _compile_agent(app: Any, root: str, arm: str, cap: float, max_cost: float) -> Any:
    from .compile_arm import CompileAgent

    return CompileAgent(app, root, arm=arm, arm_cap_usd=cap, max_cost_usd=max_cost)


def _self_repair(app: Any, bundle: str, arm: str, cap: float, max_cost: float) -> Any:
    from .repair_arm import SelfRepairProgram

    return SelfRepairProgram(
        app, bundle, arm=arm, arm_cap_usd=cap, max_cost_usd=max_cost
    )


def _python(target: str) -> Any:
    module, _, attr = target.partition(":")
    if not attr:
        raise SystemExit("--agent python:<module>:<class or factory>")
    obj = getattr(importlib.import_module(module), attr)()
    if not isinstance(obj, Executor):
        raise SystemExit(f"{target} has no name and run(task) -> Report")
    return obj
