"""The Connector loop: enroll -> (poll -> execute -> callback -> ack)*.

Each leased job is run end to end inside the customer perimeter through the
governed engine, then reported PHI-free. The loop is fail-closed: any failure
still posts a PHI-free ``failed`` callback and releases the lease, so a job never
silently strands and a crash never leaks anything upward.

The PHI-free callback body matches the runner contract (openadapt-cloud
``/api/internal/run-callback``): identifiers, the immutable bundle binding the
control plane verifies, structural metrics, and a storage PATH into the
customer's own store — NEVER the report body, screenshots, OCR text, or any
patient identifier.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from openadapt_flow.connector.client import ConnectorClient
from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.executor import ExecutionResult, Runner, execute_job
from openadapt_flow.connector.protocol import ByocJob, parse_job
from openadapt_flow.connector.storage import CustomerStorage, build_storage
from openadapt_flow.failure_signals import (
    automation_failure_signal,
    failure_signal_sharing_enabled,
)

#: storage_factory(job) -> CustomerStorage. Injected in tests; the default builds
#: from settings + the job's backend hint.
StorageFactory = Callable[[ByocJob], CustomerStorage]
CALLBACK_ATTEMPTS = 3


def phi_free_callback_body(job: ByocJob, result: ExecutionResult) -> dict[str, Any]:
    """Build the PHI-free callback body for a completed/failed job.

    Carries ONLY identifiers, the immutable bundle binding, structural metrics,
    a storage PATH (a key into the CUSTOMER store, opaque to us), and a PHI-free
    error code. Never the report body, halt reason text, or typed values.
    """
    body: dict[str, Any] = {
        "run_id": job.run_id,
        "org_id": job.org_id,
        "workflow_id": job.workflow_id,
        "bundle_version_id": job.bundle_version_id,
        "runtime_validation_id": job.runtime_validation_id,
        "mode": job.mode,
        "status": result.status,
        "report_path": result.report_ref,
        "metrics": result.metrics,
        # A boolean-only error code; never the free-text failure detail (which may
        # name internal paths). error_code is the exact enum run-callback accepts.
        "error_code": "runner_failure" if result.status == "failed" else None,
    }
    if result.verified_bundle_sha256 is not None:
        # Never echo the job's untrusted digest claim. This field exists only
        # after the connector copied, hashed, and matched the exact archive.
        body["bundle_sha256"] = result.verified_bundle_sha256
    if result.halt is not None:
        # run-callback reads only halt.present; the structured block is additive.
        body["halt"] = {"present": True}
    if result.outcome is not None:
        body["outcome"] = result.outcome
    if result.status != "success" and failure_signal_sharing_enabled():
        body["failure_signal"] = result.failure_signal or automation_failure_signal(
            None, result.status, job.target_kind
        )
    return body


def handle_job(
    client: ConnectorClient,
    settings: ConnectorSettings,
    job: ByocJob,
    *,
    runner: Runner,
    storage_factory: StorageFactory,
    require_run_token: bool,
) -> dict[str, Any]:
    """Run one leased job end to end: execute -> PHI-free callback -> ack.

    Never raises: an execution failure still reports PHI-free and is ACKed only
    after Cloud accepts that outcome. A callback/ACK delivery failure leaves the
    lease to expire as terminal ``lease_expired_uncertain`` without re-offer.
    """
    try:
        storage = storage_factory(job)
        result = execute_job(
            job, settings, storage, runner=runner, require_run_token=require_run_token
        )
    except Exception as exc:  # fail closed: build a failed result, still report
        result = ExecutionResult(
            "failed", {}, None, job.report_ref(), f"{type(exc).__name__}"
        )

    # The callback is the authoritative run outcome. Retry briefly for a
    # transient transport failure, but NEVER ACK the lease unless Cloud accepted
    # it. An unacked lease expires to `lease_expired_uncertain` and is not
    # automatically re-offered, preventing blind duplicate actuation after a
    # crash or partition.
    callback_accepted = False
    callback_body = phi_free_callback_body(job, result)
    for attempt in range(CALLBACK_ATTEMPTS):
        try:
            client.run_callback(callback_body, run_token=job.run_token)
            callback_accepted = True
            break
        except Exception:  # noqa: BLE001 - bounded retry, then leave lease unacked
            if attempt + 1 < CALLBACK_ATTEMPTS:
                time.sleep(0.25 * (2**attempt))

    if job.lease_job_id and callback_accepted:
        ack_status = "failed" if result.status == "failed" else "done"
        try:
            client.ack(job.lease_job_id, ack_status, result.error)
        except Exception:  # noqa: BLE001 - terminal uncertainty, never re-offered
            pass

    return {
        "job_id": job.lease_job_id,
        "run_id": job.run_id,
        "status": result.status,
        "callback_accepted": callback_accepted,
    }


def _default_storage_factory(settings: ConnectorSettings) -> StorageFactory:
    def factory(job: ByocJob) -> CustomerStorage:
        hint = job.storage.backend if job.storage else None
        return build_storage(settings, hint)

    return factory


def run_once(
    client: ConnectorClient,
    settings: ConnectorSettings,
    *,
    runner: Runner,
    storage_factory: Optional[StorageFactory] = None,
    require_run_token: bool = True,
) -> Optional[dict[str, Any]]:
    """Poll once; run+report one job if leased; return the result or None."""
    envelope = client.poll(settings.poll_wait_s)
    if not envelope:
        return None
    payload = dict(envelope.get("payload") or {})
    job = parse_job(payload, lease_job_id=envelope.get("id"))
    factory = storage_factory or _default_storage_factory(settings)
    return handle_job(
        client,
        settings,
        job,
        runner=runner,
        storage_factory=factory,
        require_run_token=require_run_token,
    )


def run_loop(
    client: ConnectorClient,
    settings: ConnectorSettings,
    *,
    runner: Runner,
    storage_factory: Optional[StorageFactory] = None,
    once: bool = False,
    require_run_token: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    """Enroll (if needed) then poll->execute->callback->ack until interrupted.

    ``once`` runs a single poll cycle (cron-style). Returns a process exit code.
    """
    if not client.token:
        return 1
    factory = storage_factory or _default_storage_factory(settings)
    log(f"[connector] polling {settings.control_plane_url} (org {settings.org_id})")
    handled = 0
    try:
        while True:
            result = run_once(
                client,
                settings,
                runner=runner,
                storage_factory=factory,
                require_run_token=require_run_token,
            )
            if result is not None:
                handled += 1
                log(f"[connector] job done: {result}")
            if once:
                break
    except KeyboardInterrupt:  # pragma: no cover - operator Ctrl-C
        log("[connector] interrupted; exiting")
    log(f"[connector] loop exiting; jobs handled this session: {handled}")
    return 0
