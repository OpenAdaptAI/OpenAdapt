"""Shell out to the governed ``openadapt-flow run`` CLI and map the outcome.

This module is the only execution path in openadapt-agent. It NEVER
reimplements replay: every run is a subprocess invocation of
``openadapt-flow run`` (the fail-closed deployment verb), so flow's
admission gates (certification, identity arming, effect contracts,
encryption, integrity pinning) apply exactly as they would from a terminal.

Exit-code contract of ``openadapt-flow run`` (v1.9/v1.10):

- ``0``  — run executed and every step verified: **success**.
- ``1``  — run executed and stopped: **halt** (evidence in the run
  directory's ``report.json`` / ``REPORT.md``, including the structured
  ``halt`` observation when present).
- ``2``  — **governed refusal**: an admission gate refused the bundle (or
  the bundle could not be loaded safely). Nothing was executed.

A halt or refusal is always surfaced as such — never as success. Even an
exit code of 0 is cross-checked against the persisted ``report.json``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "FlowRunner",
    "RunOutcome",
    "RunnerConfig",
    "classify_outcome",
    "default_flow_cli",
    "is_safe_run_id",
]

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TAIL_CHARS = 4000


def default_flow_cli() -> tuple[str, ...]:
    """Invoke openadapt-flow inside THIS interpreter's environment.

    ``python -m openadapt_flow`` (with the server's own interpreter) is
    the reliable default: openadapt-flow is a hard dependency of this
    package, whereas a bare ``openadapt-flow`` PATH lookup can silently
    resolve to a different (older) installation in another environment.
    Operators can still override with ``--flow-cli``.
    """
    return (sys.executable, "-m", "openadapt_flow")


@dataclass(frozen=True)
class RunnerConfig:
    """Operator-fixed execution settings (set at server start, not per call)."""

    flow_cli: tuple[str, ...] = field(default_factory=default_flow_cli)
    runs_dir: Path = Path("runs")
    url: Optional[str] = None
    deployment_config: Optional[str] = None  # --config YAML
    policy: Optional[str] = None  # --policy NAME-OR-PATH
    timeout_s: float = 600.0
    allow_url_override: bool = False
    extra_run_args: tuple[str, ...] = ()


@dataclass
class RunOutcome:
    """Structured result of one governed run attempt."""

    status: str  # "success" | "halt" | "refused" | "timeout" | "error"
    workflow: str
    run_id: Optional[str] = None
    run_dir: Optional[str] = None
    report_path: Optional[str] = None
    exit_code: Optional[int] = None
    detail: str = ""
    halt: Optional[dict] = None
    summary: dict = field(default_factory=dict)
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "success": self.status == "success",
            "workflow": self.workflow,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "report_path": self.report_path,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "halt": self.halt,
            "summary": self.summary,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _tail(text: str) -> str:
    return text[-_TAIL_CHARS:] if text else ""


def _report_summary(report: dict) -> dict:
    return {
        "workflow_name": report.get("workflow_name"),
        "steps_total": len(report.get("results") or []),
        "steps_ok": sum(1 for r in (report.get("results") or []) if r.get("ok")),
        "heal_count": report.get("heal_count"),
        "model_calls": report.get("model_calls"),
        "total_ms": report.get("total_ms"),
        "terminal_outcome": report.get("terminal_outcome"),
        "screenshots_may_leave_box": report.get("screenshots_may_leave_box"),
        "governed_authorization_id": report.get("governed_authorization_id"),
    }


def _failing_step(report: dict) -> Optional[dict]:
    for result in report.get("results") or []:
        if not result.get("ok") and not result.get("skipped"):
            return {
                "step_id": result.get("step_id"),
                "intent": result.get("intent"),
                "error": result.get("error"),
                "safety_halt": result.get("safety_halt"),
            }
    return None


def classify_outcome(
    workflow: str,
    exit_code: int,
    report: Optional[dict],
    *,
    run_id: Optional[str] = None,
    run_dir: Optional[str] = None,
    report_path: Optional[str] = None,
    stdout: str = "",
    stderr: str = "",
) -> RunOutcome:
    """Map a finished ``openadapt-flow run`` process to a :class:`RunOutcome`.

    Pure function (no I/O) so the mapping is unit-testable. The invariant:
    ``status == "success"`` requires BOTH exit code 0 AND a persisted
    report whose ``success`` flag is true — anything else surfaces as a
    halt, refusal, or error with evidence pointers.
    """
    outcome = RunOutcome(
        status="error",
        workflow=workflow,
        run_id=run_id,
        run_dir=run_dir,
        report_path=report_path,
        exit_code=exit_code,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )

    if exit_code == 2:
        outcome.status = "refused"
        outcome.detail = (
            "Governed refusal: an openadapt-flow admission gate refused this "
            "bundle before execution (or the bundle could not be loaded "
            "safely). Nothing was executed. See stdout_tail for the coverage "
            "report naming the failing gate."
        )
        return outcome

    if exit_code == 0:
        if report is None:
            outcome.status = "error"
            outcome.detail = (
                "openadapt-flow run exited 0 but no report.json was found in "
                "the run directory; refusing to report success without "
                "evidence."
            )
            return outcome
        if report.get("success") is True:
            outcome.status = "success"
            outcome.summary = _report_summary(report)
            outcome.detail = "Run completed; every executed step verified."
            return outcome
        # Exit 0 with a non-success report: trust the report, not the code.
        outcome.status = "halt"
        outcome.summary = _report_summary(report)
        outcome.halt = report.get("halt")
        outcome.detail = (
            "Process exited 0 but the persisted run report does not mark the "
            "run successful; treating as a halt (never fabricating success)."
        )
        return outcome

    # Any other nonzero exit (canonically 1): the run executed and stopped.
    outcome.status = "halt"
    if report is not None:
        outcome.summary = _report_summary(report)
        outcome.halt = report.get("halt")
        failing = _failing_step(report)
        if outcome.halt:
            outcome.detail = (
                f"Run halted at state {outcome.halt.get('state_id')!r} "
                f"({outcome.halt.get('intent')!r}): "
                f"{outcome.halt.get('reason')!r}. Evidence: report.json / "
                "REPORT.md in run_dir."
            )
        elif failing is not None:
            outcome.detail = (
                f"Run failed at step {failing.get('step_id')!r} "
                f"({failing.get('intent')!r}): {failing.get('error')!r}. "
                "Evidence: report.json / REPORT.md in run_dir."
            )
            outcome.halt = failing
        else:
            outcome.detail = (
                "Run did not complete successfully; see report.json in "
                "run_dir for step-level evidence."
            )
    else:
        outcome.detail = (
            "Run exited nonzero before a report.json was written; see "
            "stdout_tail / stderr_tail."
        )
    return outcome


class FlowRunner:
    """Execute one governed run per call via the ``openadapt-flow`` CLI."""

    def __init__(self, config: RunnerConfig):
        self.config = config

    def _build_command(
        self, bundle_dir: Path, run_dir: Path, params_file: Path, url: Optional[str]
    ) -> list[str]:
        cmd = [
            *self.config.flow_cli,
            "run",
            str(bundle_dir),
            "--run-dir",
            str(run_dir),
            "--params-file",
            str(params_file),
        ]
        if url:
            cmd += ["--url", url]
        if self.config.deployment_config:
            cmd += ["--config", self.config.deployment_config]
        if self.config.policy:
            cmd += ["--policy", self.config.policy]
        cmd += list(self.config.extra_run_args)
        return cmd

    def run(
        self,
        *,
        workflow: str,
        bundle_dir: Path,
        params: dict[str, str],
        url_override: Optional[str] = None,
    ) -> RunOutcome:
        """Run the bundle once. Params travel via ``--params-file`` (never argv)."""
        url = self.config.url
        if url_override:
            if not self.config.allow_url_override:
                return RunOutcome(
                    status="refused",
                    workflow=workflow,
                    detail=(
                        "URL override rejected: the server was not started "
                        "with --allow-url-override. The target URL is fixed "
                        "by the operator."
                    ),
                )
            url = url_override

        runs_root = Path(self.config.runs_dir)
        runs_root.mkdir(parents=True, exist_ok=True)
        run_id = f"{workflow}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_dir = runs_root / run_id
        report_path = run_dir / "report.json"

        params_fd, params_name = tempfile.mkstemp(
            prefix="openadapt_agent_params_", suffix=".json"
        )
        params_file = Path(params_name)
        try:
            with os.fdopen(params_fd, "w") as fh:
                json.dump({k: str(v) for k, v in (params or {}).items()}, fh)
            cmd = self._build_command(Path(bundle_dir), run_dir, params_file, url)
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                return RunOutcome(
                    status="timeout",
                    workflow=workflow,
                    run_id=run_id,
                    run_dir=str(run_dir),
                    report_path=str(report_path) if report_path.exists() else None,
                    detail=(
                        f"Run exceeded the per-call timeout of "
                        f"{self.config.timeout_s:.0f}s and was killed. The "
                        "target system may be in a partially-executed state; "
                        "inspect the run directory before retrying."
                    ),
                    stdout_tail=_tail(
                        exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                    ),
                    stderr_tail=_tail(
                        exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                    ),
                )
            except FileNotFoundError:
                return RunOutcome(
                    status="error",
                    workflow=workflow,
                    detail=(
                        f"openadapt-flow CLI not found ({self.config.flow_cli[0]!r}); "
                        "install openadapt-flow in the server's environment."
                    ),
                )
        finally:
            try:
                params_file.unlink(missing_ok=True)
            except OSError:
                pass

        report: Optional[dict] = None
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text())
            except (OSError, json.JSONDecodeError):
                report = None

        return classify_outcome(
            workflow,
            proc.returncode,
            report,
            run_id=run_id,
            run_dir=str(run_dir),
            report_path=str(report_path) if report_path.is_file() else None,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )

    def certify(self, bundle_dir: Path) -> dict:
        """Evaluate certification status via ``openadapt-flow certify``.

        Read-only with respect to the target system: certification
        evaluates the bundle against a policy without executing anything.
        Returns ``{"certified": None, ...}`` when no policy/config is
        configured (flow's certify requires one).
        """
        if not (self.config.policy or self.config.deployment_config):
            return {
                "certified": None,
                "detail": (
                    "Not evaluated: no --policy/--config configured on the "
                    "server. openadapt-flow certify requires a policy."
                ),
            }
        cmd = [*self.config.flow_cli, "certify", str(bundle_dir)]
        if self.config.policy:
            cmd += ["--policy", self.config.policy]
        if self.config.deployment_config:
            cmd += ["--config", self.config.deployment_config]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.config.timeout_s
            )
        except subprocess.TimeoutExpired:
            return {"certified": None, "detail": "certify timed out"}
        except FileNotFoundError:
            return {"certified": None, "detail": "openadapt-flow CLI not found"}
        return {
            "certified": proc.returncode == 0,
            "exit_code": proc.returncode,
            "detail": _tail(proc.stdout or "") or _tail(proc.stderr or ""),
        }


def is_safe_run_id(run_id: str) -> bool:
    """Run ids are single path components — no separators or traversal."""
    return bool(_RUN_ID_RE.match(run_id)) and ".." not in run_id
