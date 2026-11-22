"""How the readiness runner starts the candidate for one task.

:class:`AgentLauncher` is the seam: ``start`` gets a :class:`LaunchJob` and
returns a :class:`RunningAgent` the runner polls, stops and reads.

On main there are two launchers:

* :class:`SubprocessLauncher` (``--agent-cmd``): any command. It gets the
  guarded browser and the task in its environment and drives the tab it
  finds there::

      OPENADAPT_GUARD_CDP_URL   the DevTools endpoint of the guarded browser (a
                                loopback relay URL carrying the run's token)
      OPENADAPT_GUARD_PAGE_URL  the URL the guarded tab was opened at
      OPENADAPT_TASK            the instruction, then the inputs as JSON
      OPENADAPT_TASK_ID         the task id
      OPENADAPT_USAGE_PATH      where it may write usage.json
      OPENADAPT_RUN_DIR         its own folder for this task

  ``usage.json`` (``openadapt.agent-usage/v1``): ``{calls, input_tokens,
  output_tokens, cost_usd, model}``. With an egress session (the default,
  see :mod:`openadapt_flow.readiness.egress`) the cost is the flight proxy's
  metered cost and ``usage.json`` is only a cross-check. Without one (the
  Python API's ``egress=None``), ``cost_usd`` is used as given; without it,
  tokens are priced with
  :data:`openadapt_flow.compile_agent.agents.PRICES_PER_MTOK` for a known
  ``model``. Otherwise the cost is unknown (``null``), never zero.

  The candidate runs under the task's egress session: its model base URLs
  point at the flight proxy (or nowhere, ``blocked``) and the process is
  wrapped in the OS egress block, so it can reach only the guarded browser's
  DevTools port and the proxy.

* :class:`ProgramLauncher` (``--program BUNDLE``): ``openadapt-flow run`` on
  the guarded browser with ``--approval-mode each_item`` and a one-second
  approval wait. No person answers, so the run stops before the save, as a
  declined item does; the approval request it wrote holds the form's values,
  which become the proposed write (``parse: "form"``, the form's labels as
  names). The shadow hold stays on the tab as the backstop.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

__all__ = [
    "USAGE_SCHEMA",
    "AgentLauncher",
    "AgentResult",
    "EgressOptions",
    "LaunchJob",
    "ProgramLauncher",
    "RunningAgent",
    "SubprocessLauncher",
    "price_usage",
    "read_usage",
]

USAGE_SCHEMA = "openadapt.agent-usage/v1"


@dataclass(frozen=True)
class LaunchJob:
    task_id: str
    instruction: str
    inputs: Mapping[str, Any]
    cdp_url: str
    page_url: str
    run_dir: Path
    #: Inputs added by ``readiness import`` to name the record (not program
    #: parameters).
    record_keys: tuple[str, ...] = ()

    @property
    def task_text(self) -> str:
        return (
            self.instruction.strip()
            + "\n\nInputs:\n"
            + json.dumps(dict(self.inputs), indent=2, sort_keys=True)
        )

    @property
    def usage_path(self) -> Path:
        return self.run_dir / "usage.json"


@dataclass
class AgentResult:
    exit_code: Optional[int]
    usage: Optional[dict[str, Any]] = None
    #: A write the candidate proposed without sending it (a program stopped
    #: before its save): fields in the proposed-write shape.
    proposed_fields: Optional[list[dict[str, Any]]] = None
    notes: list[str] = field(default_factory=list)
    #: The egress session's summary (:meth:`EgressSession.finish`), or None
    #: when the candidate ran without one.
    egress: Optional[dict[str, Any]] = None


class RunningAgent(Protocol):
    def poll(self) -> Optional[int]: ...

    def stop(self) -> None: ...

    def result(self) -> AgentResult: ...


class AgentLauncher(Protocol):
    label: str

    def start(self, job: LaunchJob) -> RunningAgent: ...


def price_usage(usage: Mapping[str, Any]) -> Optional[float]:
    """The run's model cost in USD, or None when it can't be known."""

    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return round(float(cost), 6)
    from openadapt_flow.compile_agent.agents import PRICES_PER_MTOK

    prices = PRICES_PER_MTOK.get(str(usage.get("model") or ""))
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if prices is None or not all(
        isinstance(n, int) and not isinstance(n, bool) and n >= 0
        for n in (tokens_in, tokens_out)
    ):
        return None
    return round(
        (tokens_in * prices["input"] + tokens_out * prices["output"]) / 1_000_000, 6
    )


def read_usage(path: Path) -> Optional[dict[str, Any]]:
    """``usage.json`` with its price, or None when there is none."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") not in (None, USAGE_SCHEMA):
        return None
    usage = {
        "calls": raw.get("calls"),
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "model": raw.get("model"),
    }
    usage["cost_usd"] = price_usage(raw)
    return usage


class _Process:
    def __init__(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self._log = os.fdopen(fd, "wb")
        self.proc = subprocess.Popen(
            list(argv),
            env={**os.environ, **env},
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.proc.wait(timeout=5)
        self._log.close()


def _cdp_port(cdp_url: str) -> int:
    from urllib.parse import urlsplit

    port = urlsplit(cdp_url).port
    if port is None:
        raise ValueError("the DevTools URL has no port")
    return int(port)


class _RunningCommand:
    def __init__(
        self, job: LaunchJob, argv: Sequence[str], egress: Optional["EgressOptions"] = None
    ) -> None:
        self.job = job
        self.egress = None
        argv = list(argv)
        extra: dict[str, str] = {}
        if egress is not None:
            from openadapt_flow.readiness.egress import EgressSession

            self.egress = EgressSession(
                egress.mode,
                job.run_dir,
                cdp_port=_cdp_port(job.cdp_url),
                spend_cap_usd=egress.spend_cap_usd,
                upstreams=egress.upstreams,
                scrubber=egress.scrubber,
            ).start()
            extra = self.egress.env
            argv = self.egress.wrap(argv)
        env = {
            "OPENADAPT_GUARD_CDP_URL": job.cdp_url,
            "OPENADAPT_GUARD_PAGE_URL": job.page_url,
            "OPENADAPT_TASK": job.task_text,
            "OPENADAPT_TASK_ID": job.task_id,
            "OPENADAPT_USAGE_PATH": str(job.usage_path),
            "OPENADAPT_RUN_DIR": str(job.run_dir),
            **extra,
        }
        if self.egress is not None:
            env["OPENADAPT_EGRESS"] = self.egress.mode
        try:
            self._process = _Process(argv, env, job.run_dir / "agent.log")
        except Exception:
            if self.egress is not None:
                self.egress.finish()
            raise

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def stop(self) -> None:
        self._process.stop()

    def _egress_summary(self) -> Optional[dict[str, Any]]:
        return self.egress.finish() if self.egress is not None else None

    def result(self) -> AgentResult:
        return AgentResult(
            exit_code=self._process.proc.returncode,
            usage=read_usage(self.job.usage_path),
            egress=self._egress_summary(),
        )


@dataclass(frozen=True)
class EgressOptions:
    """How every task's candidate reaches a model (see
    :mod:`openadapt_flow.readiness.egress`)."""

    mode: str = "scrubbed"
    spend_cap_usd: Optional[float] = None
    upstreams: Mapping[str, str] = field(default_factory=dict)
    #: An already checked scrubber (tests); None loads the real one.
    scrubber: Any = field(default=None, compare=False)


class SubprocessLauncher:
    """Run ``--agent-cmd`` for each task (see the module docstring).

    ``egress`` defaults to ``scrubbed``; pass ``None`` only from Python, for
    a candidate whose traffic is controlled some other way."""

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        label: Optional[str] = None,
        egress: Optional[EgressOptions] = EgressOptions(),
    ):
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.argv:
            raise ValueError("the agent command is empty")
        self.label = label or Path(self.argv[0]).name
        self.egress = egress

    def start(self, job: LaunchJob) -> RunningAgent:
        return _RunningCommand(job, self.argv, self.egress)


def approval_fields(run_dir: Path) -> Optional[list[dict[str, Any]]]:
    """The form values in the run's approval request, as proposed fields."""

    folder = run_dir / "item-approval"
    found = None
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("schema_version") != "openadapt.item-approval-request/v1":
            continue
        if doc.get("fields"):
            found = [
                {"name": str(f.get("label")), "value": f.get("after"), "parse": "form"}
                for f in doc["fields"]
            ]
    return found


class _RunningProgram(_RunningCommand):
    def result(self) -> AgentResult:
        run_dir = self.job.run_dir / "program-run"
        usage: Optional[dict[str, Any]] = None
        try:
            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            calls = report.get("model_calls")
            if isinstance(calls, int) and calls == 0:
                usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                         "model": None, "cost_usd": 0.0}
        except (OSError, ValueError, AttributeError):
            usage = None
        fields = approval_fields(run_dir)
        return AgentResult(
            exit_code=self._process.proc.returncode,
            usage=usage,
            proposed_fields=fields,
            notes=[] if fields else ["the program wrote no approval request with fields"],
            egress=self._egress_summary(),
        )


class ProgramLauncher:
    """Run a compiled program up to its save (see the module docstring)."""

    def __init__(
        self,
        bundle: Path | str,
        *,
        python_cmd: Sequence[str] = (sys.executable,),
        label: Optional[str] = None,
        egress: Optional[EgressOptions] = EgressOptions(),
    ) -> None:
        self.bundle = Path(bundle)
        self.python_cmd = list(python_cmd)
        self.label = label or f"program:{self.bundle.name}"
        self.egress = egress

    def flow_args(self, job: LaunchJob) -> list[str]:
        """The ``openadapt-flow`` arguments. They hold the relay URL with its
        token and the task's inputs, so they never go on a command line."""

        argv = [
            "run",
            str(self.bundle),
            "--browser-cdp-endpoint",
            job.cdp_url,
            "--browser-page-url",
            job.page_url,
            "--approval-mode",
            "each_item",
            "--approval-wait",
            "1",
            "--run-dir",
            str(job.run_dir / "program-run"),
        ]
        for key, value in sorted(job.inputs.items()):
            if key in job.record_keys:
                continue
            argv += ["--param", f"{key}={value}"]
        return argv

    def argv(self, job: LaunchJob) -> list[str]:
        """The child's command line: the program-args file only (see
        :mod:`openadapt_flow.readiness.program_child`)."""

        from openadapt_flow.readiness.program_child import write_args

        path = write_args(job.run_dir / "program-args.json", self.flow_args(job))
        return [*self.python_cmd, "-m", "openadapt_flow.readiness.program_child", str(path)]

    def start(self, job: LaunchJob) -> RunningAgent:
        argv = self.argv(job)
        try:
            return _RunningProgram(job, argv, self.egress)
        except BaseException:
            # The child never started, so nothing will read and delete it.
            (job.run_dir / "program-args.json").unlink(missing_ok=True)
            raise


def wait_exit(agent: RunningAgent, timeout_s: float) -> Optional[int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        code = agent.poll()
        if code is not None:
            return code
        time.sleep(0.05)
    return None
