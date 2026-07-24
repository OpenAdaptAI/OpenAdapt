"""Execute one governed BYOC job locally, inside the customer perimeter.

The Connector never grows a private execution path: a dispatched run is the SAME
fail-closed ``openadapt-flow run`` admission gate + shared governed Replayer the
local CLI uses (identity gates + effect verification + halt-don't-guess intact),
in a child process (crash isolation). ``run`` REFUSES to execute unless every
admission gate holds; the delivered policy adds a second, dispatch-level fail
closed on top.

Fail-closed application of the control-plane-delivered policy:

* :meth:`ByocJob.ensure_governed` must pass (policy, callback token, closed
  substrate, and exact archive SHA present) or the job is refused before any
  GUI is touched; customer storage must then return the exact approved ZIP
  bytes, whose digest is checked before safe extraction;
* if the org enabled a grounding rung (``grounding_model.enabled``) whose
  ``api_key_env`` is NOT set in the Connector's own environment, the job is
  refused — the org required a governed control this machine cannot honor, so we
  halt rather than silently run without it;
* the resolved policy is written to ``<run_dir>/governed_policy.json`` for the
  operator's local audit trail.

The PHI-bearing report.json is written to the CUSTOMER'S storage and stays
local; only PHI-free status/metrics are returned for the callback.

NOTE (remaining, cross-runner): fully MATERIALIZING every delivered safety key
into the engine's runtime config (so e.g. the delivered grounding endpoint is
the one the Replayer dials) is the same "runner-side consumption" half the Modal
and Windows runners have not finished either — today the operator's local
deployment profile (``--config``) is authoritative for the engine's runtime
posture and the delivered policy governs DISPATCH. See RUNNER_CLIENT_LIBRARY.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.protocol import ByocGovernanceError, ByocJob
from openadapt_flow.connector.storage import CustomerStorage, extract_bundle_archive

#: A run-gate refusal (fail-closed admission denied) exits 2 before the replay
#: creates report.json.
GATE_REFUSED_EXIT = 2


@dataclass
class RunOutcome:
    """The result of a single child ``run`` invocation."""

    returncode: int
    report: dict[str, Any] = field(default_factory=dict)


#: A runner maps argv -> RunOutcome. The default shells the governed ``run`` CLI
#: in a child process; tests inject a fake to avoid launching a GUI.
Runner = Callable[[list[str], Path], RunOutcome]


@dataclass
class ExecutionResult:
    status: str  # success | halt | failed
    metrics: dict[str, Any]
    halt: Optional[dict[str, Any]]
    report_ref: Optional[str]
    error: Optional[str] = None
    verified_bundle_sha256: Optional[str] = None


def _grounding_env_available(job: ByocJob) -> bool:
    gm = job.grounding_model
    if not gm.enabled:
        return True
    if not gm.api_key_env:
        # Enabled but no key env named — the endpoint may be keyless (local); the
        # engine's own grounding gate is the backstop. Do not block here.
        return True
    return bool(os.environ.get(gm.api_key_env))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_run_argv(
    job: ByocJob,
    settings: ConnectorSettings,
    bundle_dir: Path,
    run_dir: Path,
    params_file: Optional[Path],
) -> list[str]:
    """The exact governed CLI invocation for a verified BYOC dispatch.

    Everything security-relevant is pinned from LOCAL, operator-owned material:
    the deployment profile (``--config``) and the policy come from the
    Connector's config; the bundle is the one resolved from the customer's own
    storage. The child re-runs the whole fail-closed admission gate regardless of
    what this library already checked (defense in depth).
    """
    argv = [
        sys.executable,
        "-m",
        "openadapt_flow",
        "run",
        str(bundle_dir),
        "--run-dir",
        str(run_dir),
    ]
    if settings.profile:
        argv += ["--config", settings.profile]
    policy = settings.policy
    if policy:
        argv += ["--policy", policy]
    argv += ["--backend", job.target_kind]
    if params_file is not None:
        argv += ["--params-file", str(params_file)]
    if job.target_url:
        argv += ["--url", job.target_url]
    if settings.allow_unencrypted:
        # Local escape hatch, mirroring the governed run CLI; default OFF.
        argv.append("--allow-unencrypted")
    return argv


def _subprocess_runner(argv: list[str], run_dir: Path) -> RunOutcome:
    proc = subprocess.run(argv, capture_output=True, text=True)  # nosec - fixed argv
    report_path = run_dir / "report.json"
    report: dict[str, Any] = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    return RunOutcome(returncode=proc.returncode, report=report)


def status_from_report(returncode: int, report: dict[str, Any]) -> str:
    """Map (exit code, report.json) -> control-plane status (matches the runner).

    success : returncode 0 and report.success truthy.
    halt    : a non-success run whose report marks a halt.
    failed  : anything else (a hard failure, a crash, a gate refusal, or a
              missing report).
    """
    if report and (
        report.get("terminal_outcome") == "halt"
        or ("halt" in report and report["halt"] is not None)
    ):
        return "halt"
    if (
        returncode == 0
        and report.get("success") is True
        and report.get("terminal_outcome") in (None, "success")
    ):
        return "success"
    return "failed"


def metrics_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """PHI-free structural metrics only (counts/durations, never free text)."""
    results = report.get("results") or []

    def numeric(value: Any, default: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return value

    return {
        "steps": len(results),
        "steps_ok": sum(1 for r in results if r.get("ok")),
        "halts": 1
        if (
            report.get("terminal_outcome") == "halt"
            or ("halt" in report and report["halt"] is not None)
        )
        else 0,
        "heals": numeric(report.get("heal_count"), 0),
        "model_calls": numeric(report.get("model_calls"), 0),
        "cost_usd": numeric(report.get("est_model_cost_usd"), 0.0),
    }


def halt_object(report: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "halt" in report and report["halt"] is not None:
        return report["halt"]
    if report.get("terminal_outcome") == "halt":
        return {"outcome": "halt"}
    return None


def _write_params_file(params: dict[str, Any], run_dir: Path) -> Optional[Path]:
    """Write runtime params to a 0600 file so values stay OFF the process table.

    Drops the internal ``target_kind`` routing hint (not a workflow param).
    """
    clean = {k: v for k, v in params.items() if k != "target_kind"}
    if not clean:
        return None
    pf = run_dir / "params.json"
    pf.write_text(json.dumps(clean), encoding="utf-8")
    try:
        os.chmod(pf, 0o600)
    except OSError:  # pragma: no cover
        pass
    return pf


def _write_policy_audit(job: ByocJob, run_dir: Path) -> None:
    """Record the delivered governed policy locally for the operator audit."""
    try:
        (run_dir / "governed_policy.json").write_text(
            json.dumps(
                {
                    "run_id": job.run_id,
                    "org_id": job.org_id,
                    "target_kind": job.target_kind,
                    "safety": job.safety,
                    "grounding_model": job.grounding_model.model_dump(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover
        pass


def execute_job(
    job: ByocJob,
    settings: ConnectorSettings,
    storage: CustomerStorage,
    *,
    runner: Runner = _subprocess_runner,
    require_run_token: bool = True,
) -> ExecutionResult:
    """Run one governed BYOC job end to end, inside the customer perimeter.

    Fail-closed on every governance requirement. Never raises for a job-level
    refusal — returns a ``failed`` :class:`ExecutionResult` with a PHI-free
    reason so the caller can report + ack it and move on.
    """
    # 1. Governance gates (fail closed BEFORE any bundle is fetched or run).
    try:
        job.ensure_governed(require_run_token=require_run_token)
    except ByocGovernanceError as exc:
        return ExecutionResult("failed", {}, None, job.report_ref(), str(exc))
    if not _grounding_env_available(job):
        gm = job.grounding_model
        return ExecutionResult(
            "failed",
            {},
            None,
            job.report_ref(),
            f"org requires grounding model {gm.model or gm.provider!r} but its "
            f"api key env {gm.api_key_env!r} is not set on this connector "
            "(fail closed; halting rather than running without the governed rung)",
        )

    with tempfile.TemporaryDirectory(prefix="oa-byoc-") as tmp:
        tmp_path = Path(tmp)
        bundle_archive = tmp_path / "approved-bundle.zip"
        bundle_scratch = tmp_path / "bundle"
        run_dir = tmp_path / "run"
        bundle_scratch.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

        _write_policy_audit(job, run_dir)

        # 2. Copy and verify the EXACT approved archive bytes from the customer's
        # storage before extraction. The content digest inside workflow.json is
        # a separate semantic binding; neither can substitute for this signed
        # archive-byte identity.
        try:
            fetched_archive = storage.fetch_bundle_archive(
                job.storage.bundle_ref if job.storage else None, bundle_archive
            )
            observed_sha256 = _sha256_file(fetched_archive)
            if observed_sha256 != job.bundle_sha256:
                raise RuntimeError("bundle archive SHA-256 does not match dispatch")
            extract_bundle_archive(fetched_archive, bundle_scratch)
            bundle_dir = bundle_scratch
        except Exception as exc:  # storage failure — fail closed, PHI-free msg
            return ExecutionResult(
                "failed",
                {},
                None,
                job.report_ref(),
                f"customer-storage bundle fetch failed: {type(exc).__name__}",
            )

        try:
            params_file = _write_params_file(job.params, run_dir)

            # 3. The governed, fail-closed child invocation.
            argv = build_run_argv(job, settings, Path(bundle_dir), run_dir, params_file)
            outcome = runner(argv, run_dir)
        except Exception as exc:
            return ExecutionResult(
                "failed",
                {},
                None,
                job.report_ref(),
                f"governed child invocation failed: {type(exc).__name__}",
                verified_bundle_sha256=observed_sha256,
            )

        report = outcome.report or {}
        status = status_from_report(outcome.returncode, report)
        report_target_kind = report.get("execution_target_kind")
        if report_target_kind != job.target_kind:
            status = "failed"
            outcome_error = (
                "governed child report execution_target_kind does not match "
                "the signed dispatch target_kind (fail closed)"
            )
        else:
            outcome_error = None

        # 4. The PHI-bearing report body goes to the CUSTOMER'S store, never ours.
        report_ref = job.report_ref()
        try:
            storage.write_report(report_ref, report)
        except Exception as exc:  # no durable report means the run is not complete
            return ExecutionResult(
                "failed",
                metrics_from_report(report),
                halt_object(report),
                report_ref,
                f"customer-storage report write failed: {type(exc).__name__}",
                verified_bundle_sha256=observed_sha256,
            )

        return ExecutionResult(
            status=status,
            metrics=metrics_from_report(report),
            halt=halt_object(report),
            report_ref=report_ref,
            error=outcome_error,
            verified_bundle_sha256=observed_sha256,
        )
