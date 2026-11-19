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
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.protocol import ByocGovernanceError, ByocJob
from openadapt_flow.connector.secrets import LocalSecretStore, SecretStoreError
from openadapt_flow.connector.storage import CustomerStorage, extract_bundle_archive
from openadapt_flow.failure_signals import automation_failure_signal
from openadapt_flow.ir import RunReport
from openadapt_flow.runner.dispatch_envelope import (
    write_managed_dispatch_envelope_value,
)
from openadapt_flow.runtime.durable.authority import (
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
)

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
Runner = Callable[[list[str], Path, Mapping[str, str]], RunOutcome]

_MANAGED_DELIVERY_AUTHORITY_PATH = "/api/internal/managed-delivery-permit"


@dataclass
class ExecutionResult:
    status: str  # success | halt | failed
    metrics: dict[str, Any]
    halt: Optional[dict[str, Any]]
    report_ref: Optional[str]
    #: Local, operator-facing detail. NEVER sent: it can name paths or values.
    error: Optional[str] = None
    verified_bundle_sha256: Optional[str] = None
    failure_signal: Optional[dict[str, Any]] = None
    outcome: Optional[dict[str, Any]] = None
    #: Closed code that may leave the machine (ack ``error_code``).
    error_code: Optional[str] = None


# --- Wrap programs: admission and independent read-back -----------------------
WRAP_MANIFEST_NAME = "openadapt-wrap-publish.json"
WRAP_READBACK_RELPATH = "readback/verify.yaml"
WRAP_ADMISSION_NAME = "admission.json"
_WRAP_LABEL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_WRAP_RECEIPT_MAX = 256 * 1024


def wrap_readback_sha256(bundle_dir: Path) -> Optional[str]:
    """SHA-256 of a wrap bundle's verify.yaml, "" for a wrap bundle without one,
    None for any other bundle (same rule as the managed runner)."""
    manifest = bundle_dir / WRAP_MANIFEST_NAME
    readback = bundle_dir / WRAP_READBACK_RELPATH
    if not manifest.is_file() and not readback.is_file():
        return None
    if readback.is_symlink() or not readback.is_file():
        return ""
    return hashlib.sha256(readback.read_bytes()).hexdigest()


def wrap_readback_evidence(
    receipt: bytes, readback_sha256: Optional[str]
) -> Optional[dict[str, str]]:
    """The closed callback fields from Flow's effect receipt, or None."""
    if not readback_sha256 or not receipt or len(receipt) > _WRAP_RECEIPT_MAX:
        return None
    try:
        document = json.loads(receipt.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    labels = {
        k: document.get(k) for k in ("outcome", "effect_verdict", "verification_tier")
    }
    for value in labels.values():
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 40
            or not set(value) <= _WRAP_LABEL_CHARS
        ):
            return None
    return {
        "readback_sha256": readback_sha256,
        **labels,  # type: ignore[dict-item]
        "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
    }


def _run_flow_tool(argv: list[str], child_env: Mapping[str, str]) -> int:
    """Run one read-only Flow helper (``verify-run``); output stays local."""
    proc = subprocess.run(  # nosec - fixed argv and exact process-local env
        argv, capture_output=True, text=True, env=dict(child_env), timeout=300
    )
    return proc.returncode


# --- Cancellation (kill switch, control-plane cancel) ---------------------------
class _Children:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen[Any]] = {}
        self.cancelled: set[str] = set()


_CHILDREN = _Children()
_CURRENT = threading.local()


def cancel_job(job_id: str) -> bool:
    """Mark a job cancelled and terminate its governed child if one runs."""
    with _CHILDREN.lock:
        _CHILDREN.cancelled.add(job_id)
        proc = _CHILDREN.procs.get(job_id)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True
    return False


def job_cancelled(job_id: Optional[str]) -> bool:
    if not job_id:
        return False
    with _CHILDREN.lock:
        return job_id in _CHILDREN.cancelled


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
    managed_dispatch_file: Optional[Path] = None,
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
    if managed_dispatch_file is not None:
        argv += ["--managed-dispatch-file", str(managed_dispatch_file)]
    # Cloud-dispatched items have no per-item pause until the founder decides
    # otherwise (approval-mode design section 11). Say so explicitly, so the
    # run report records "not_gated" instead of waiting for a person nobody
    # is asked to be.
    argv += ["--approval-mode", "not_gated"]
    if settings.allow_unencrypted:
        # Local escape hatch, mirroring the governed run CLI; default OFF.
        argv.append("--allow-unencrypted")
    return argv


def _subprocess_runner(
    argv: list[str], run_dir: Path, child_env: Mapping[str, str]
) -> RunOutcome:
    job_id = getattr(_CURRENT, "job_id", None)
    proc = subprocess.Popen(  # nosec - fixed argv and exact process-local env
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(child_env),
    )
    if job_id:
        with _CHILDREN.lock:
            _CHILDREN.procs[job_id] = proc
            cancelled = job_id in _CHILDREN.cancelled
        if cancelled:
            proc.terminate()
    try:
        proc.wait()
    finally:
        if job_id:
            with _CHILDREN.lock:
                _CHILDREN.procs.pop(job_id, None)
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
    outcome = report.get("execution_outcome")
    if outcome is not None:
        if outcome == "VERIFIED" and returncode == 0 and report.get("success") is True:
            return "success"
        if outcome in {"HALTED", "COMPLETED_UNVERIFIED", "ROLLED_BACK"}:
            return "halt"
        return "failed"
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


def precise_outcome_from_report(report: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Project a valid local report onto its closed PHI-free outcome envelope."""

    try:
        envelope = RunReport.model_validate(report).outcome_envelope
    except (TypeError, ValueError):
        return None
    return envelope.model_dump(mode="json") if envelope is not None else None


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
            report.get("execution_outcome") == "HALTED"
            or (
                report.get("execution_outcome") is None
                and (
                    report.get("terminal_outcome") == "halt"
                    or ("halt" in report and report["halt"] is not None)
                )
            )
        )
        else 0,
        "heals": numeric(report.get("heal_count"), 0),
        "model_calls": numeric(report.get("model_calls"), 0),
        "cost_usd": numeric(report.get("est_model_cost_usd"), 0.0),
    }


def halt_object(report: dict[str, Any]) -> Optional[dict[str, Any]]:
    outcome = report.get("execution_outcome")
    if outcome is not None and outcome != "HALTED":
        return None
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


def _assert_managed_authority_is_pinned(
    job: ByocJob, settings: ConnectorSettings
) -> None:
    """Keep the run capability on the enrolled control-plane origin."""

    if job.managed_dispatch is None:
        return
    try:
        configured = urlsplit(settings.control_plane_url)
        delivered = urlsplit(job.managed_delivery_authority_url or "")
        configured_port = configured.port or (
            443 if configured.scheme == "https" else None
        )
        delivered_port = delivered.port or (
            443 if delivered.scheme == "https" else None
        )
    except ValueError as exc:
        raise ByocGovernanceError(
            "byoc managed delivery authority URL is invalid"
        ) from exc
    if (
        configured.scheme != "https"
        or not configured.hostname
        or configured.username is not None
        or configured.password is not None
        or bool(configured.query)
        or bool(configured.fragment)
        or delivered.scheme != "https"
        or delivered.hostname != configured.hostname
        or delivered_port != configured_port
        or delivered.path != _MANAGED_DELIVERY_AUTHORITY_PATH
    ):
        raise ByocGovernanceError(
            "byoc managed delivery authority is not pinned to the enrolled "
            "control plane (fail closed)"
        )


def execute_job(
    job: ByocJob,
    settings: ConnectorSettings,
    storage: CustomerStorage,
    *,
    runner: Runner = _subprocess_runner,
    require_run_token: bool = True,
    on_secrets: Optional[Callable[[list[str]], None]] = None,
) -> ExecutionResult:
    """Run one governed BYOC job end to end, inside the customer perimeter.

    Fail-closed on every governance requirement. Never raises for a job-level
    refusal: returns a ``failed`` :class:`ExecutionResult` with a closed
    ``error_code`` (which may leave the machine) and a local ``error`` detail
    (which never does).
    """
    _CURRENT.job_id = job.lease_job_id
    try:
        return _execute_job(
            job,
            settings,
            storage,
            runner=runner,
            require_run_token=require_run_token,
            on_secrets=on_secrets,
        )
    finally:
        _CURRENT.job_id = None


def _refused(job: ByocJob, code: str, detail: str, **extra: Any) -> ExecutionResult:
    return ExecutionResult(
        "failed", {}, None, job.report_ref(), detail, error_code=code, **extra
    )


def _execute_job(
    job: ByocJob,
    settings: ConnectorSettings,
    storage: CustomerStorage,
    *,
    runner: Runner,
    require_run_token: bool,
    on_secrets: Optional[Callable[[list[str]], None]],
) -> ExecutionResult:
    # 1. Governance gates (fail closed BEFORE any bundle is fetched or run).
    try:
        job.ensure_governed(require_run_token=require_run_token)
        _assert_managed_authority_is_pinned(job, settings)
    except ByocGovernanceError as exc:
        return _refused(job, "governance_refused", str(exc))
    admission = job.program_admission()
    if admission is not None and (
        admission["public_key_hex"] not in settings.pinned_admission_keys()
    ):
        # The control plane can name an admission key, but only this machine's
        # own configuration decides which admitters it trusts.
        return _refused(
            job,
            "admission_key_not_pinned",
            "the program's admission key is not pinned in trusted_admission_keys",
        )
    if not _grounding_env_available(job):
        gm = job.grounding_model
        return _refused(
            job,
            "grounding_unavailable",
            f"org requires grounding model {gm.model or gm.provider!r} but its "
            f"api key env {gm.api_key_env!r} is not set on this connector",
        )
    try:
        secret_env = LocalSecretStore(settings.secrets_dir).resolve(
            job.secrets_ref or settings.default_secrets_ref
        )
    except SecretStoreError as exc:
        return _refused(job, "secrets_unavailable", str(exc))
    if on_secrets is not None and secret_env:
        on_secrets(list(secret_env.values()))
    signers_json: Optional[str] = None
    if settings.qualification_signers_file:
        try:
            signers_json = Path(settings.qualification_signers_file).read_text(
                encoding="utf-8"
            )
        except OSError:
            return _refused(
                job, "signers_unavailable", "qualification signer registry unreadable"
            )

    with tempfile.TemporaryDirectory(prefix="oa-byoc-") as tmp:
        tmp_path = Path(tmp)
        bundle_archive = tmp_path / "approved-bundle.zip"
        bundle_scratch = tmp_path / "bundle"
        run_dir = tmp_path / "run"
        readback_dir = tmp_path / "readback"
        bundle_scratch.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        readback_dir.mkdir(parents=True, exist_ok=True)

        _write_policy_audit(job, run_dir)

        # 2. Copy and verify the EXACT approved archive bytes from the customer's
        # storage before extraction.
        try:
            fetched_archive = storage.fetch_bundle_archive(
                job.storage.bundle_ref if job.storage else None, bundle_archive
            )
            observed_sha256 = _sha256_file(fetched_archive)
            if observed_sha256 != job.bundle_sha256:
                raise RuntimeError("bundle archive SHA-256 does not match dispatch")
            extract_bundle_archive(fetched_archive, bundle_scratch)
            bundle_dir = bundle_scratch
        except Exception as exc:  # storage failure: fail closed
            return _refused(
                job,
                "bundle_fetch_failed",
                f"customer-storage bundle fetch failed: {type(exc).__name__}",
            )

        readback_sha256 = wrap_readback_sha256(bundle_dir)
        key_dir: Optional[Path] = None
        if admission is not None:
            admission_file = bundle_dir / WRAP_ADMISSION_NAME
            if (
                not admission_file.is_file()
                or admission_file.is_symlink()
                or not readback_sha256
            ):
                return _refused(
                    job,
                    "program_admission_refused",
                    "the archive is not an admitted program with a read-back check",
                    verified_bundle_sha256=observed_sha256,
                )
            if (
                hashlib.sha256(admission_file.read_bytes()).hexdigest()
                != admission["admission_sha256"]
            ):
                return _refused(
                    job,
                    "program_admission_refused",
                    "the archive's admission is not the dispatched one",
                    verified_bundle_sha256=observed_sha256,
                )
            key_dir = tmp_path / "jit-keys"
            if settings.admission_key_dir:
                # The customer's own registry: a private copy per run, so the
                # child can never change the operator's trust anchors.
                shutil.copytree(settings.admission_key_dir, key_dir, symlinks=False)
            else:
                (key_dir / "keys").mkdir(parents=True)
                (key_dir / "keys" / "ed25519.pub").write_text(
                    admission["public_key_hex"] + "\n", encoding="utf-8"
                )

        try:
            params_file = _write_params_file(job.params, run_dir)
            managed_dispatch_file = None
            child_env = os.environ.copy()
            # Never let a long-running Connector inherit stale authority from
            # its service environment. Add the exact run capability only for
            # the child that also receives the matching dispatch envelope.
            for name in (
                REMOTE_AUTHORITY_URL_ENV,
                REMOTE_AUTHORITY_TOKEN_ENV,
                "OPENADAPT_JIT_KEY_DIR",
                "BYOC_CONNECTOR_TOKEN",
            ):
                child_env.pop(name, None)
            child_env.update(secret_env)
            if signers_json is not None:
                child_env["OPENADAPT_QUALIFICATION_SIGNERS_JSON"] = signers_json
            if key_dir is not None:
                child_env["OPENADAPT_JIT_KEY_DIR"] = str(key_dir)
            if job.managed_dispatch is not None:
                managed_dispatch_file = write_managed_dispatch_envelope_value(
                    run_dir / "managed-dispatch.json", job.flow_dispatch()
                )
                child_env[REMOTE_AUTHORITY_URL_ENV] = str(
                    job.managed_delivery_authority_url
                )
                child_env[REMOTE_AUTHORITY_TOKEN_ENV] = str(job.run_token)

            # Independent read-back, before: a private baseline of the system
            # of record. It holds raw records and never leaves this directory.
            baseline = readback_dir / "baseline.json"
            readback_params = run_dir / "readback-params.json"
            if readback_sha256:
                readback_params.write_text(
                    json.dumps(
                        {k: v for k, v in job.params.items() if k != "target_kind"}
                    ),
                    encoding="utf-8",
                )
                os.chmod(readback_params, 0o600)
                verify = str(bundle_dir / WRAP_READBACK_RELPATH)
                rc = _run_flow_tool(
                    [
                        sys.executable,
                        "-m",
                        "openadapt_flow",
                        "verify-run",
                        "--verify",
                        verify,
                        "--params",
                        str(readback_params),
                        "--capture-baseline",
                        str(baseline),
                    ],
                    child_env,
                )
                if rc != 0:
                    baseline.unlink(missing_ok=True)

            # 3. The governed, fail-closed child invocation.
            argv = build_run_argv(
                job,
                settings,
                Path(bundle_dir),
                run_dir,
                params_file,
                managed_dispatch_file,
            )
            if job_cancelled(job.lease_job_id):
                return _refused(
                    job,
                    "cancelled_by_control_plane",
                    "cancelled before the governed child started",
                    verified_bundle_sha256=observed_sha256,
                )
            outcome = runner(argv, run_dir, child_env)
        except Exception as exc:
            return _refused(
                job,
                "child_invocation_failed",
                f"governed child invocation failed: {type(exc).__name__}",
                verified_bundle_sha256=observed_sha256,
            )

        report = outcome.report or {}
        status = status_from_report(outcome.returncode, report)
        error_code: Optional[str] = None
        report_target_kind = report.get("execution_target_kind")
        if job_cancelled(job.lease_job_id):
            status = "failed"
            error_code = "cancelled_by_control_plane"
            outcome_error = "cancelled by the control plane while running"
        elif report_target_kind != job.target_kind:
            status = "failed"
            error_code = "target_kind_mismatch"
            outcome_error = (
                "governed child report execution_target_kind does not match "
                "the signed dispatch target_kind (fail closed)"
            )
        else:
            outcome_error = None
            if status == "failed":
                error_code = "governed_run_failed"

        # Independent read-back, after: rule on the run with the same
        # verify.yaml. Only hashes and closed labels of the receipt leave.
        readback_evidence: Optional[dict[str, str]] = None
        if readback_sha256 and baseline.is_file() and error_code is None:
            run_record = readback_dir / "run-record.json"
            run_record.write_text('{"executor_kind":"flow_replay"}', encoding="utf-8")
            receipt_path = readback_dir / "effect-receipt.json"
            rc = _run_flow_tool(
                [
                    sys.executable,
                    "-m",
                    "openadapt_flow",
                    "verify-run",
                    "--verify",
                    str(bundle_dir / WRAP_READBACK_RELPATH),
                    "--params",
                    str(readback_params),
                    "--run-record",
                    str(run_record),
                    "--baseline",
                    str(baseline),
                    "--authorization-ref",
                    job.run_id,
                    "--out",
                    str(readback_dir),
                ],
                child_env,
            )
            if rc == 0 and receipt_path.is_file():
                readback_evidence = wrap_readback_evidence(
                    receipt_path.read_bytes(), readback_sha256
                )

        # 4. The PHI-bearing report body goes to the CUSTOMER'S store, never ours.
        report_ref: Optional[str] = job.report_ref()
        halt = halt_object(report)
        outcome_envelope = precise_outcome_from_report(report)
        if error_code is not None and outcome_envelope is not None:
            # A cancelled or mismatched run never reports a success envelope.
            outcome_envelope = None
        try:
            storage.write_report(report_ref, report)
        except Exception as exc:
            # A storage fault says nothing about what the child did, so it
            # never becomes a runner failure: a run that actuated would show
            # as Failed and a re-run would repeat the write. Without a durable
            # report the run can't claim success either, so a completed run
            # stops for a person to check the record. A child failure stays a
            # failure. Never send a path to an object that wasn't written.
            storage_error = (
                f"customer-storage report write failed: {type(exc).__name__}"
            )
            outcome_error = (
                f"{outcome_error}; {storage_error}" if outcome_error else storage_error
            )
            report_ref = None
            if status == "success":
                status = "halt"
                outcome_envelope = None
                halt = halt or {"outcome": "halt"}
                error_code = error_code or "report_write_failed"

        metrics = metrics_from_report(report)
        if readback_evidence is not None:
            metrics["wrap_readback"] = readback_evidence
        return ExecutionResult(
            status=status,
            metrics=metrics,
            halt=halt,
            report_ref=report_ref,
            error=outcome_error,
            verified_bundle_sha256=observed_sha256,
            failure_signal=automation_failure_signal(report, status, job.target_kind),
            outcome=outcome_envelope,
            error_code=error_code,
        )
