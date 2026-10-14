"""Shell out to the governed ``openadapt-flow run`` CLI and map the outcome.

This module is the run path for operator bundles. It NEVER reimplements
replay: every bundle run is a subprocess invocation of ``openadapt-flow run``
(the fail-closed deployment verb), so Flow's admission gates (certification,
identity arming, effect contracts, encryption, integrity pinning) apply
exactly as they would from a terminal. The synthetic sandbox calls the same
Flow run gate in-process (see :mod:`openadapt_agent.sandbox`).

Exit-code contract of ``openadapt-flow run``:

- ``0``  — run reached a normal terminal state. The persisted
  ``execution_outcome`` decides whether the result is verified.
- ``1``  — run executed and stopped: **halt** (evidence in the run
  directory's ``report.json`` / ``REPORT.md``, including the structured
  ``halt`` observation when present).
- ``2``  — **governed refusal**: an admission gate refused the bundle (or
  the bundle could not be loaded safely). Nothing was executed.

Results are derived by :mod:`openadapt_agent.contract` from Flow's
``transaction_outcome``. A halt or refusal is never success, a verified run
is never an error, and an uncertain save is never "nothing changed". Even an
exit code of 0 is cross-checked against the persisted ``report.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_agent.contract import (
    REASONS,
    TRANSACTION_REASONS,
    build_result,
    parse_failed_checks,
    reason_for_run,
)

__all__ = [
    "FlowRunner",
    "RunOutcome",
    "RunnerConfig",
    "classify_outcome",
    "classify_report_status",
    "default_flow_cli",
    "is_safe_run_id",
    "legacy_reason",
    "new_run_id",
    "public_outcome_message",
    "public_report_summary",
    "status_for_reason",
]

_PRECISE_EXECUTION_OUTCOMES = frozenset(
    {
        "VERIFIED",
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    }
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TAIL_CHARS = 4000
_LOG = logging.getLogger(__name__)
# Legacy ``status`` values a caller may still hold, mapped to the contract
# reason that makes no claim beyond what the status itself proves.
_STATUS_FALLBACK_REASONS = {
    "success": "saved_and_checked",
    "halt": "result_unreadable",
    "refused": "not_ready_to_run",
    "timeout": "timed_out",
    "error": "result_unreadable",
}
def legacy_reason(
    status: str,
    execution_outcome: Optional[str] = None,
    transaction_outcome: Optional[str] = None,
) -> str:
    """Contract reason for a legacy status when no fuller evidence is at hand.

    The coarse ``HALTED`` label never selects "nothing was written": without a
    transaction outcome a halt is reported as uncertain.
    """
    if status == "halt":
        if transaction_outcome in TRANSACTION_REASONS:
            return TRANSACTION_REASONS[transaction_outcome]
        if execution_outcome == "COMPLETED_UNVERIFIED":
            return "not_checked"
        if execution_outcome == "ROLLED_BACK":
            return "change_reversed"
    return _STATUS_FALLBACK_REASONS.get(status, "result_unreadable")


def public_outcome_message(
    status: str,
    execution_outcome: Optional[str],
    transaction_outcome: Optional[str] = None,
) -> str:
    """Return fixed public copy for one classified terminal result."""
    reason = legacy_reason(status, execution_outcome, transaction_outcome)
    return REASONS[reason].what_happened


def status_for_reason(reason: str, *, refused: bool = False, timed_out: bool = False) -> str:
    """Legacy ``status`` that agrees with a contract reason."""
    if refused:
        return "refused"
    if timed_out:
        return "timeout"
    if reason == "saved_and_checked":
        return "success"
    if reason in {"result_unreadable", "platform_error", "app_unreachable"}:
        return "error"
    return "halt"


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
    execution_outcome: Optional[str] = None
    transaction_outcome: Optional[str] = None
    #: Closed contract reason (``openadapt_agent.contract.REASONS``).
    reason: Optional[str] = None
    #: Closed refusal check codes, never Flow's own text.
    failed_checks: list[str] = field(default_factory=list)
    #: The persisted report, kept so a later attention lookup can refine the
    #: reason. Never exported unless protected export is enabled.
    report: Optional[dict] = field(default=None, repr=False)
    refused_before_start: bool = False
    timed_out: bool = False
    process_started: Optional[bool] = True

    def contract_reason(self) -> str:
        """The contract reason for this outcome, falling back to the status."""
        if self.reason in REASONS:
            return self.reason
        return legacy_reason(self.status, self.execution_outcome, self.transaction_outcome)

    def apply_attention(self, attention: Optional[Mapping[str, Any]]) -> None:
        """Refine the reason with Flow's Needs Attention state for this run."""
        if attention is None or self.refused_before_start or self.timed_out:
            return
        if not isinstance(self.report, dict):
            return
        self.reason = reason_for_run(
            exit_code=self.exit_code,
            report=self.report,
            process_started=self.process_started,
            attention=attention,
        )

    def contract(
        self,
        *,
        mode: str = "production",
        request_id: Optional[str] = None,
        workflow: Optional[str] = None,
        needs_attention_id: Optional[str] = None,
    ) -> dict:
        """Project the partner contract result for this outcome."""
        model_calls = self.summary.get("model_calls") if isinstance(self.summary, dict) else None
        total_ms = self.summary.get("total_ms") if isinstance(self.summary, dict) else None
        return build_result(
            self.contract_reason(),
            workflow=workflow if workflow is not None else self.workflow,
            run_id=self.run_id,
            request_id=request_id,
            mode=mode,
            execution_outcome=self.execution_outcome,
            transaction_outcome=self.transaction_outcome,
            failed_checks=self.failed_checks,
            needs_attention_id=needs_attention_id,
            model_calls=model_calls if isinstance(model_calls, int) else None,
            seconds=(
                total_ms / 1000.0
                if isinstance(total_ms, (int, float)) and not isinstance(total_ms, bool)
                else None
            ),
        )

    def to_dict(self, *, include_protected: bool = False) -> dict:
        """Project an MCP-safe legacy result; raw local evidence is explicit opt-in.

        The legacy keys stay for existing callers. The partner contract fields
        (``outcome``, ``safe_to_retry``, ``what_happened``, ...) are added
        alongside them, and ``message`` is the contract's plain sentence.
        """
        contract = self.contract()
        result = {
            "schema_version": 1,
            "status": self.status,
            "success": self.status == "success",
            "sealed": False,
            # Kept for older callers. A verified local run is reported as done
            # with ``proof: "local"``; it is never turned into a failure.
            "requires_seal": False,
            "frames_included": False,
            "workflow_id": self.workflow,
            "run_id": self.run_id,
            "message": contract["what_happened"],
            "summary": _sanitize_public_summary(self.summary),
        }
        for key in (
            "outcome",
            "label",
            "safe_to_retry",
            "what_happened",
            "next_action",
            "next_step",
            "reason",
            "record_changed",
            "proof",
            "contract_version",
            "failed_checks",
        ):
            if key in contract:
                result[key] = contract[key]
        if self.execution_outcome is not None:
            result["execution_outcome"] = self.execution_outcome
        if self.transaction_outcome is not None:
            result["transaction_outcome"] = self.transaction_outcome
        if include_protected:
            result["protected"] = {
                "workflow": self.workflow,
                "run_dir": self.run_dir,
                "report_path": self.report_path,
                "exit_code": self.exit_code,
                "detail": self.detail,
                "halt": self.halt,
                "stdout_tail": self.stdout_tail,
                "stderr_tail": self.stderr_tail,
            }
        return result


def _tail(text: str) -> str:
    return text[-_TAIL_CHARS:] if text else ""


def _sanitize_public_summary(values: dict) -> dict:
    summary: dict[str, int | float | bool] = {}
    for key in (
        "steps_total",
        "steps_ok",
        "steps_skipped",
        "heal_count",
        "model_calls",
    ):
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            summary[key] = value
    total_ms = values.get("total_ms")
    if isinstance(total_ms, (int, float)) and not isinstance(total_ms, bool) and total_ms >= 0:
        summary["total_ms"] = total_ms
    screenshots_egress = values.get("screenshots_may_leave_box")
    if isinstance(screenshots_egress, bool):
        summary["screenshots_may_leave_box"] = screenshots_egress
    return summary


def public_report_summary(report: object) -> dict:
    """Return count/boolean metrics only; never report labels or text."""
    if not isinstance(report, dict):
        return {}
    results = report.get("results")
    results = results if isinstance(results, list) else []
    summary: dict[str, int | float | bool] = {
        "steps_total": len(results),
        "steps_ok": sum(
            1 for result in results if isinstance(result, dict) and result.get("ok") is True
        ),
        "steps_skipped": sum(
            1 for result in results if isinstance(result, dict) and result.get("skipped") is True
        ),
    }
    for key in ("heal_count", "model_calls"):
        value = report.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            summary[key] = value
    total_ms = report.get("total_ms")
    if isinstance(total_ms, (int, float)) and not isinstance(total_ms, bool) and total_ms >= 0:
        summary["total_ms"] = total_ms
    screenshots_egress = report.get("screenshots_may_leave_box")
    if isinstance(screenshots_egress, bool):
        summary["screenshots_may_leave_box"] = screenshots_egress
    return _sanitize_public_summary(summary)


def _report_summary(report: object) -> dict:
    return {
        **public_report_summary(report),
    }


def _failing_step(report: dict) -> Optional[dict]:
    results = report.get("results")
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        if not result.get("ok") and not result.get("skipped"):
            return {
                "step_id": result.get("step_id"),
                "intent": result.get("intent"),
                "error": result.get("error"),
                "safety_halt": result.get("safety_halt"),
            }
    return None


def classify_report_status(report: object) -> tuple[str, Optional[str]]:
    """Classify a persisted Flow report without weakening precise outcomes.

    Flow versions before the precise outcome contract exposed only the legacy
    ``success`` flag. New reports are authoritative through
    ``execution_outcome``: only ``VERIFIED`` can become agent-facing success.
    In particular, Demo can intentionally retain ``success=true`` while its
    precise outcome is ``COMPLETED_UNVERIFIED``.
    """

    if not isinstance(report, dict):
        return "error", None

    precise = report.get("execution_outcome")
    if precise is None:
        success = report.get("success")
        if success is True:
            return "success", None
        if success is False:
            return "halt", None
        return "error", None
    if not isinstance(precise, str) or precise not in _PRECISE_EXECUTION_OUTCOMES:
        return "error", None

    success = report.get("success")
    if not isinstance(success, bool):
        return "error", precise

    profile = report.get("execution_profile")
    if profile is not None and profile not in {"demo", "standard", "regulated"}:
        return "error", precise

    production_eligible = report.get("production_eligible")
    if production_eligible is not None and not isinstance(production_eligible, bool):
        return "error", precise
    if production_eligible is True and (
        precise != "VERIFIED" or profile not in {"standard", "regulated"}
    ):
        return "error", precise

    envelope = report.get("outcome_envelope")
    if envelope is not None:
        if not isinstance(envelope, dict) or envelope.get("outcome") != precise:
            return "error", precise
        for report_key, envelope_key in (
            ("execution_profile", "profile"),
            ("production_eligible", "production_eligible"),
            ("execution_completed", "execution_completed"),
            ("model_calls", "model_calls"),
            ("external_network_calls", "external_network_calls"),
        ):
            if report_key in report and envelope.get(envelope_key) != report.get(report_key):
                return "error", precise

    if precise == "VERIFIED":
        # A production-eligible VERIFIED run saved the change and read it back.
        # Local MCP mints no Seal, so the proof is local; the run is still a
        # success. Reporting it as an error would invite a duplicate write.
        return ("success" if success is True else "error"), precise
    if precise in {"HALTED", "FAILED", "ROLLED_BACK"} and success is True:
        return "error", precise
    if precise == "FAILED":
        return "error", precise
    return "halt", precise


def classify_outcome(
    workflow: str,
    exit_code: int,
    report: object | None,
    *,
    run_id: Optional[str] = None,
    run_dir: Optional[str] = None,
    report_path: Optional[str] = None,
    stdout: str = "",
    stderr: str = "",
) -> RunOutcome:
    """Map a finished ``openadapt-flow run`` process to a :class:`RunOutcome`.

    Pure function (no I/O) so the mapping is unit-testable. The contract
    reason comes from :func:`openadapt_agent.contract.reason_for_run`, which
    reads Flow's ``transaction_outcome``; the legacy ``status`` is derived from
    that reason so the two never disagree. ``status == "success"`` requires
    exit code 0 AND a consistent persisted ``VERIFIED`` report.
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
        report=report if isinstance(report, dict) else None,
    )
    if isinstance(report, dict):
        _status, outcome.execution_outcome = classify_report_status(report)
        transaction = report.get("transaction_outcome")
        if isinstance(transaction, str):
            outcome.transaction_outcome = transaction

    refused = exit_code == 2 and not isinstance(report, dict)
    outcome.refused_before_start = refused
    outcome.reason = reason_for_run(
        exit_code=exit_code,
        report=report if isinstance(report, dict) else None,
        refused_before_start=refused,
    )
    if exit_code == 2:
        outcome.failed_checks = parse_failed_checks(f"{stdout}\n{stderr}")
    if refused:
        outcome.status = "refused"
        outcome.detail = (
            "Governed refusal: an openadapt-flow admission gate refused this "
            "bundle before execution (or the bundle could not be loaded "
            "safely). Nothing was executed. See stdout_tail for the coverage "
            "report naming the failing gate."
        )
        return outcome

    outcome.status = status_for_reason(outcome.reason)
    if isinstance(report, dict):
        outcome.summary = _report_summary(report)
        outcome.halt = report.get("halt") if outcome.status != "success" else None
    if outcome.status == "success":
        outcome.detail = "Run completed; every executed step verified."
        return outcome
    if report is None:
        outcome.detail = (
            "openadapt-flow run exited without a report.json in the run "
            "directory; refusing to report success without evidence. See "
            "stdout_tail / stderr_tail."
        )
        return outcome
    if outcome.execution_outcome == "COMPLETED_UNVERIFIED":
        outcome.detail = (
            "Execution completed, but the persisted evidence did not prove "
            "VERIFIED success; review the local run before any retry."
        )
        return outcome
    failing = _failing_step(report) if isinstance(report, dict) else None
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
            "The persisted report has no consistent verified terminal outcome; "
            "see report.json in run_dir for step-level evidence."
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
        run_id: Optional[str] = None,
    ) -> RunOutcome:
        """Run the bundle once. Params travel via ``--params-file`` (never argv).

        ``run_id`` lets a caller hand out the id before the run finishes. It
        must be a single safe path component; a fresh one is made otherwise.
        """
        url = self.config.url
        if run_id is None or not is_safe_run_id(run_id):
            run_id = new_run_id()
        if url_override:
            if not self.config.allow_url_override:
                return RunOutcome(
                    status="refused",
                    workflow=workflow,
                    run_id=run_id,
                    reason="not_ready_to_run",
                    failed_checks=["url_override_not_allowed"],
                    refused_before_start=True,
                    process_started=False,
                    detail=(
                        "URL override rejected: the server was not started "
                        "with --allow-url-override. The target URL is fixed "
                        "by the operator."
                    ),
                )
            url = url_override

        runs_root = Path(self.config.runs_dir)
        runs_root.mkdir(parents=True, exist_ok=True)
        run_dir = runs_root / run_id
        report_path = run_dir / "report.json"

        params_fd, params_name = tempfile.mkstemp(prefix="openadapt_agent_params_", suffix=".json")
        params_file = Path(params_name)
        try:
            with os.fdopen(params_fd, "w") as fh:
                json.dump({k: str(v) for k, v in (params or {}).items()}, fh)
            cmd = self._build_command(Path(bundle_dir), run_dir, params_file, url)
            try:
                # Decode with replacement: a strict decode error would surface
                # after Flow already ran and read as a launch failure.
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.config.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                return RunOutcome(
                    status="timeout",
                    workflow=workflow,
                    run_id=run_id,
                    run_dir=str(run_dir),
                    report_path=str(report_path) if report_path.exists() else None,
                    reason="timed_out",
                    timed_out=True,
                    detail=(
                        f"Run exceeded the per-call timeout of "
                        f"{self.config.timeout_s:.0f}s and was killed. The "
                        "target system may be in a partially-executed state; "
                        "inspect the run directory before retrying."
                    ),
                    stdout_tail=_tail(_text(exc.stdout)),
                    stderr_tail=_tail(_text(exc.stderr)),
                )
            except FileNotFoundError:
                return RunOutcome(
                    status="error",
                    workflow=workflow,
                    run_id=run_id,
                    reason="platform_error",
                    process_started=False,
                    detail=(
                        f"openadapt-flow CLI not found ({self.config.flow_cli[0]!r}); "
                        "install openadapt-flow in the server's environment."
                    ),
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                # subprocess.run raises these while creating the child, so
                # the governed run normally never started. If Flow already
                # made its run directory, it did start and may have acted.
                _LOG.exception("governed Flow subprocess failed locally")
                started = run_dir.exists()
                return RunOutcome(
                    status="error",
                    workflow=workflow,
                    run_id=run_id,
                    run_dir=str(run_dir),
                    report_path=(str(report_path) if report_path.exists() else None),
                    reason="result_unreadable" if started else "platform_error",
                    process_started=None if started else False,
                    detail=f"{type(exc).__name__}: {exc}",
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
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            _LOG.exception("Flow certification subprocess failed locally")
            return {
                "certified": None,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        return {
            "certified": proc.returncode == 0,
            "exit_code": proc.returncode,
            "detail": _tail(proc.stdout or "") or _tail(proc.stderr or ""),
        }


def _text(value: object) -> str:
    """Captured process output as text, never raising on odd bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def is_safe_run_id(run_id: str) -> bool:
    """Run ids are single path components — no separators or traversal."""
    return (
        isinstance(run_id, str)
        and len(run_id) <= 128
        and bool(_RUN_ID_RE.match(run_id))
        and ".." not in run_id
    )


def new_run_id() -> str:
    """A fresh opaque run id (``run-`` plus 24 hex characters)."""
    return f"run-{uuid.uuid4().hex[:24]}"
