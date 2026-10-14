"""Parallels local-VM environment for the flow-on-WAA harness.

Runs the SAME demonstrate-then-replay + hybrid eval modes LOCALLY at **$0**
against a Parallels Windows VM (native Apple-Silicon virt) -- so we can de-risk
the runner + backend + replay on a real local Windows desktop BEFORE spending
Azure $ on the WAA (cloud) environment. Same runner, same per-task metrics.

Reuses openadapt-flow's :class:`openadapt_flow.backends.parallels_vm.ParallelsVM`
(a ``prlctl`` wrapper: snapshot/revert-safe, host-side capture, in-session
``win_agent`` launch via ``launch_agent``) and the ``win_agent`` server from
openadapt-flow PR #95. Once ``launch_agent`` is up it exposes the SAME
``/screenshot`` + ``/execute_windows`` contract WAA does, so the identical
``WindowsBackend`` replay path works unchanged.

Parallels supplies the ENVIRONMENT (a real local Windows desktop) but NOT WAA's
standardized tasks/verifiers -- those are supplied here: trivial built-in tasks
on Notepad/Calculator, verified by reading ground-truth in-guest state (mirrors
how WAA verifies real OS state, just with our own checks).

Environments at a glance:
    Parallels  -> LOCAL,  $0 (native virt), this module.
    WAA/Azure  -> CLOUD,  $  (VM-hours + agent tokens), replay_runner + cost.

Snapshot safety: **snapshot -> run -> revert**. We take our OWN snapshot of the
clean state, run the (mutating) task, then ``revert`` to it. We NEVER stop or
delete the user's VM or their snapshots -- ``ParallelsVM`` exposes no delete, so
the contract is revert-only by construction.

Opt-in: everything here is SKIPPED unless ``OPENADAPT_PARALLELS=1`` (or
``ParallelsConfig(enabled=True)``). Heavy imports (``openadapt_flow``) are lazy
and the VM is injectable, so this is unit-testable with a fake VM -- no prlctl,
no real VM, no mutation.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_evals.flow.replay_runner import (
    FlowTask,
    PerTaskReplayMetrics,
    aggregate_replay_metrics,
    run_demonstrate_then_replay,
)

logger = logging.getLogger(__name__)

#: Opt-in gate. Nothing here runs against a real VM unless this is truthy.
PARALLELS_ENV_VAR = "OPENADAPT_PARALLELS"

#: The user's suspended Parallels "Windows 11" VM (from project memory). Only a
#: default -- override via ``OPENADAPT_PARALLELS_VM_UUID`` or ``ParallelsConfig``.
DEFAULT_VM_UUID = "d4f9c29a-52e1-4793-9334-7e971c3d0ab3"

#: win_agent port (ParallelsVM.SHIM_PORT default).
DEFAULT_AGENT_PORT = 5000

# A verifier: (vm) -> (success, reason). Reads ground-truth in-guest state.
# A verifier returns (verdict, reason). `verdict is None` means the check
# could not be performed at all -- the exec channel failed, the guest agent is
# dead, the VM was reverted -- which the runner records as UNSCORED rather than
# as a failed task. Returning False for "I could not look" pins the verified
# success rate at 0% regardless of ground truth.
VerifyFn = Callable[[Any], "tuple[Optional[bool], Optional[str]]"]


def parallels_enabled(config: "Optional[ParallelsConfig]" = None) -> bool:
    """True when the Parallels environment is opted in (env var or config)."""
    if config is not None and config.enabled:
        return True
    return os.environ.get(PARALLELS_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


@dataclass
class ParallelsConfig:
    """Configuration for a Parallels local-VM eval session."""

    vm_uuid: str = field(
        default_factory=lambda: os.environ.get("OPENADAPT_PARALLELS_VM_UUID", DEFAULT_VM_UUID)
    )
    prlctl: str = "/usr/local/bin/prlctl"
    agent_port: int = DEFAULT_AGENT_PORT
    agent_token: str = field(default_factory=lambda: secrets.token_hex(16))
    snapshot_name: str = ""  # filled in at session start if empty
    settle_s: float = 6.0
    launch_wait_s: float = 25.0
    enabled: bool = False  # explicit override of the env-var gate


@dataclass
class ParallelsTask:
    """A trivial local task: a demo bundle to replay + OUR ground-truth verifier."""

    task_id: str
    instruction: str
    verify: VerifyFn
    bundle_dir: Optional[Path] = None
    params: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in trivial tasks + ground-truth verifiers (in-guest state reads).
# ---------------------------------------------------------------------------

_NOTEPAD_OUT = r"C:\oa\eval\notepad_out.txt"
_NOTEPAD_EXPECTED = "openadapt-flow replay ok"


def _exec_output(proc: Any, what: str) -> "tuple[Optional[str], Optional[str]]":
    """Return ``(stdout, None)`` or ``(None, why_it_could_not_be_read)``.

    ``exec_ps``/``exec_cmd`` hand back a CompletedProcess-shaped object whose
    ``returncode`` used to be discarded. A dead ``win_agent``, a ``prlctl``
    failure, an auth rejection and a reverted VM all produce empty stdout,
    which then compares unequal to the expected value and is recorded as the
    task having failed -- ground-truth verification silently degrading into a
    measured failure.
    """
    returncode = getattr(proc, "returncode", None)
    if returncode not in (0, None):
        stderr = (getattr(proc, "stderr", "") or "").strip()[:200]
        return None, f"{what} could not run (exit {returncode}): {stderr!r}"
    return (getattr(proc, "stdout", "") or ""), None


def verify_notepad_file(
    vm: Any, *, path: str = _NOTEPAD_OUT, expected: str = _NOTEPAD_EXPECTED
) -> "tuple[Optional[bool], Optional[str]]":
    """Success iff the Notepad task wrote ``expected`` into ``path`` in-guest."""
    ps = f"if (Test-Path '{path}') {{ Get-Content -Raw '{path}' }} else {{ 'MISSING' }}"
    proc = vm.exec_ps(ps)
    out, unavailable = _exec_output(proc, "notepad file read")
    if unavailable is not None:
        return None, unavailable
    out = out.strip()
    ok = expected in out
    return ok, f"notepad file contents: {out!r}"


def verify_calculator_open(vm: Any) -> "tuple[Optional[bool], Optional[str]]":
    """Success iff a Calculator window/process is present in-guest (trivial check)."""
    # One /FI per call: `tasklist` ANDs multiple IMAGENAME filters, so asking
    # for CalculatorApp.exe AND Calculator.exe is unsatisfiable and always
    # prints "No tasks are running which match" -- this check could never
    # return True, pinning the task's verified success rate at 0%.
    reasons = []
    for image in ("CalculatorApp.exe", "Calculator.exe"):
        proc = vm.exec_cmd(f'tasklist /FI "IMAGENAME eq {image}"')
        out, unavailable = _exec_output(proc, "tasklist")
        if unavailable is not None:
            return None, unavailable
        reasons.append(f"{image}: {out.strip()[:80]!r}")
        if "alculator" in out:
            return True, f"tasklist: {out.strip()[:120]!r}"
    return False, "tasklist: " + "; ".join(reasons)


def builtin_tasks(bundles_dir: Optional[Path] = None) -> list[ParallelsTask]:
    """Return the built-in trivial local tasks.

    A task's ``bundle_dir`` is ``<bundles_dir>/<task_id>`` when that directory
    holds a compiled ``workflow.json`` (record + compile the demo first, same as
    WAA); otherwise it stays None and the runner flags "no compiled bundle".
    """

    def _bundle(task_id: str) -> Optional[Path]:
        if bundles_dir is None:
            return None
        cand = Path(bundles_dir) / task_id
        return cand if (cand / "workflow.json").exists() else None

    specs = [
        ("notepad_write", "Open Notepad, type a line, and save it to a file.",
         verify_notepad_file),
        ("calculator_open", "Open the Calculator app.", verify_calculator_open),
    ]
    return [
        ParallelsTask(task_id=tid, instruction=instr, verify=vfn, bundle_dir=_bundle(tid))
        for tid, instr, vfn in specs
    ]


# ---------------------------------------------------------------------------
# Session: snapshot -> run -> revert, launch win_agent, expose the replay path.
# ---------------------------------------------------------------------------


class ParallelsSession:
    """A snapshot-safe, opt-in-gated Parallels eval session.

    Lifecycle (``with`` block):
      enter  -> ensure VM running, snapshot the clean state, launch win_agent
                (session 1) and return its URL as :attr:`server_url`.
      body   -> replay/hybrid drive the SAME WindowsBackend against that URL.
      exit   -> revert to the entry snapshot (clean reset). NEVER stop/delete.

    Args:
        config: Session configuration.
        vm: An injected ``ParallelsVM``-like object (for tests). When None, a
            real ``openadapt_flow.backends.parallels_vm.ParallelsVM`` is built
            lazily (requires the ``flow`` extra + prlctl + the VM).
    """

    def __init__(self, config: Optional[ParallelsConfig] = None, *, vm: Any = None) -> None:
        self.config = config or ParallelsConfig()
        self._vm = vm
        self._snapshot_id: Optional[str] = None
        self.server_url: Optional[str] = None

    @property
    def vm(self) -> Any:
        if self._vm is None:
            raise RuntimeError("ParallelsSession not entered (no VM)")
        return self._vm

    def _build_vm(self) -> Any:
        try:
            from openadapt_flow.backends.parallels_vm import ParallelsVM
        except ImportError as e:  # pragma: no cover - only without the extra/PR95
            raise ImportError(
                "openadapt-flow (with the win_agent/ParallelsVM support from PR #95) "
                "is required for the Parallels environment. Install: "
                "pip install 'openadapt-evals[flow]'"
            ) from e
        return ParallelsVM(self.config.vm_uuid, prlctl=self.config.prlctl)

    def __enter__(self) -> "ParallelsSession":
        if not parallels_enabled(self.config):
            raise RuntimeError(
                f"Parallels environment is opt-in: set {PARALLELS_ENV_VAR}=1 "
                "(or ParallelsConfig(enabled=True)) to run it."
            )
        if self._vm is None:
            self._vm = self._build_vm()
        vm = self._vm

        vm.ensure_running(settle_s=self.config.settle_s)
        name = self.config.snapshot_name or f"oa-flow-eval-{int(time.time())}"
        # snapshot -> run -> revert: snapshot the CURRENT (clean) state now.
        self._snapshot_id = vm.snapshot(name, "openadapt-flow eval clean state (revert-only)")
        logger.info("Parallels: snapshotted clean state as %s (%s)", name, self._snapshot_id)

        self.server_url = vm.launch_agent(
            port=self.config.agent_port,
            host="0.0.0.0",
            token=self.config.agent_token,
            wait_s=self.config.launch_wait_s,
        )
        logger.info("Parallels: win_agent up at %s", self.server_url)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        vm = self._vm
        if vm is not None and self._snapshot_id is not None:
            # Clean per-run reset. Revert-only: we never stop or delete the VM
            # or any snapshot (the user's VM must survive untouched).
            try:
                vm.revert(self._snapshot_id)
                logger.info("Parallels: reverted to %s (clean reset)", self._snapshot_id)
            except Exception:  # noqa: BLE001
                logger.exception("Parallels: revert to %s failed", self._snapshot_id)

    # -- the replay path (same WindowsBackend contract as WAA) --------------

    def replay_fn(
        self, bundle_dir: Path, server_url: str, run_dir: Path, params: Optional[dict]
    ) -> Any:
        """Replay a bundle via WindowsBackend against the win_agent (with token)."""
        from openadapt_flow.ir import Workflow
        from openadapt_flow.runtime import Replayer

        backend = self._make_backend(server_url)
        workflow = Workflow.load(bundle_dir)
        try:
            return Replayer(backend).run(
                workflow, params=params or {}, bundle_dir=bundle_dir, run_dir=run_dir
            )
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def _make_backend(self, server_url: str) -> Any:
        from openadapt_flow.backends.windows_backend import WindowsBackend

        # auth_token is a win_agent/PR #95 addition to WindowsBackend; degrade
        # gracefully on an older flow that lacks it (unauthenticated agent).
        try:
            return WindowsBackend(server_url=server_url, auth_token=self.config.agent_token)
        except TypeError:
            logger.warning(
                "WindowsBackend has no auth_token (pre-PR#95 flow); using unauthenticated agent"
            )
            return WindowsBackend(server_url=server_url)


def make_parallels_evaluator(
    vm: Any, tasks_by_id: "dict[str, ParallelsTask]"
) -> "Callable[[str], tuple[bool, Optional[str]]]":
    """Build a WAA-style verifier callable over our built-in ground-truth checks."""

    def _evaluate(task_id: str) -> "tuple[Optional[bool], Optional[str]]":
        task = tasks_by_id.get(task_id)
        if task is None:
            # We were never asked to score a task we know about; that is a
            # harness bug, not a task the replay failed.
            return None, f"unknown parallels task {task_id!r}"
        return task.verify(vm)

    return _evaluate


def run_parallels_replay(
    tasks: Optional[Sequence[ParallelsTask]] = None,
    config: Optional[ParallelsConfig] = None,
    *,
    vm: Any = None,
    replay_fn: Optional[Callable] = None,
    run_root: Path | str = "parallels_results",
) -> "tuple[list[PerTaskReplayMetrics], dict]":
    """Demonstrate-then-replay on the LOCAL Parallels VM, scored by OUR verifiers.

    Same runner + metrics as WAA; only the environment (local $0 vs cloud $) and
    the verifier (ours vs WAA's) differ. Snapshot-safe and opt-in gated.

    Args:
        tasks: Local tasks (defaults to :func:`builtin_tasks`).
        config: Session configuration.
        vm: Injected ``ParallelsVM``-like object (tests). Real VM built when None.
        replay_fn: Override the replay step (defaults to the session's real
            WindowsBackend replay; inject a fake for tests).
        run_root: Directory for per-task replay dirs.

    Returns:
        (per-task metrics, aggregate summary).
    """
    config = config or ParallelsConfig()
    tasks = list(tasks) if tasks is not None else builtin_tasks()
    tasks_by_id = {t.task_id: t for t in tasks}

    with ParallelsSession(config, vm=vm) as sess:
        flow_tasks = [
            FlowTask(task_id=t.task_id, bundle_dir=t.bundle_dir, params=t.params)
            for t in tasks
        ]
        evaluator = make_parallels_evaluator(sess.vm, tasks_by_id)
        metrics = run_demonstrate_then_replay(
            flow_tasks, sess.server_url or "", run_root,
            replay_fn=replay_fn or sess.replay_fn, evaluator=evaluator,
        )
    return metrics, aggregate_replay_metrics(metrics)
