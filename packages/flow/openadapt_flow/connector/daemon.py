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

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

import httpx

from openadapt_flow.connector.client import (
    ConnectorClient,
    ConnectorClientError,
    ConnectorControl,
    RunsStopped,
)
from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.egress import EgressViolation
from openadapt_flow.connector.executor import (
    ExecutionResult,
    Runner,
    cancel_job,
    execute_job,
)
from openadapt_flow.connector.protocol import ByocJob, ByocJobParseError, parse_job
from openadapt_flow.connector.storage import CustomerStorage, build_storage
from openadapt_flow.failure_signals import (
    automation_failure_signal,
    failure_signal_sharing_enabled,
)

#: storage_factory(job) -> CustomerStorage. Injected in tests; the default builds
#: from settings + the job's backend hint.
StorageFactory = Callable[[ByocJob], CustomerStorage]
CALLBACK_ATTEMPTS = 3
HEARTBEAT_S = 15.0
STOPPED_BACKOFF_S = 15.0
MAX_BACKOFF_S = 60.0
#: persist_token(new_token) writes the rotated token durably before use.
TokenPersister = Callable[[str], None]


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
    """Run one leased job end to end: execute -> metadata-only callback -> ack.

    Never raises: an execution failure still reports and is ACKed only after
    Cloud accepts that outcome. A callback/ACK delivery failure leaves the
    lease to expire as terminal ``lease_expired_uncertain`` without re-offer.
    """
    # The executor supplies its observed archive digest once the child could
    # have written to the system of record.
    child_bundle_sha256: Optional[str] = None

    def child_start(verified_sha256: str) -> None:
        nonlocal child_bundle_sha256
        child_bundle_sha256 = verified_sha256

    try:
        storage = storage_factory(job)
        result = execute_job(
            job,
            settings,
            storage,
            runner=runner,
            require_run_token=require_run_token,
            on_secrets=client.add_secret_values,
            on_child_start=child_start,
        )
    except Exception as exc:  # The outcome may be unknown after child handoff.
        result = ExecutionResult(
            "halt" if child_bundle_sha256 else "failed",
            {},
            {"present": True} if child_bundle_sha256 else None,
            None,
            type(exc).__name__,
            verified_bundle_sha256=child_bundle_sha256,
            error_code="connector_exception",
        )

    # The callback is the authoritative run outcome. Retry briefly for a
    # transient transport failure, but NEVER ACK the lease unless Cloud accepted
    # it. An unacked lease expires to `lease_expired_uncertain` and is not
    # automatically re-offered, preventing blind duplicate actuation.
    callback_accepted = False
    local_diagnostics_unavailable = result.local_diagnostics_unavailable
    try:
        callback_body = phi_free_callback_body(job, result)
    except Exception:  # noqa: BLE001 - nothing unchecked is ever sent
        callback_body = None
    transport_failures = 0
    while callback_body is not None and transport_failures < CALLBACK_ATTEMPTS:
        try:
            client.run_callback(callback_body, run_token=job.run_token)
            callback_accepted = True
            break
        except EgressViolation:
            if result.error_code == "egress_refused":
                break  # Even the minimal binding was refused. Leave the lease unacked.
            # Discard every report-derived field. Cloud requires the observed
            # archive digest for a halt, and runner_failure for a pre-hash failure.
            result = ExecutionResult(
                "halt" if child_bundle_sha256 else "failed",
                {},
                {"present": True} if child_bundle_sha256 else None,
                None,
                verified_bundle_sha256=child_bundle_sha256,
                error_code="egress_refused",
            )
            callback_body = phi_free_callback_body(job, result)
            callback_body.pop("failure_signal", None)
            if child_bundle_sha256:
                callback_body["error_code"] = "egress_refused"
        except Exception:  # noqa: BLE001 - bounded retry, then leave lease unacked
            transport_failures += 1
            if transport_failures < CALLBACK_ATTEMPTS:
                time.sleep(0.25 * (2 ** (transport_failures - 1)))

    if job.lease_job_id and callback_accepted:
        ack_status = "failed" if result.status == "failed" else "done"
        code = result.error_code or (
            "governed_run_failed" if ack_status == "failed" else None
        )
        try:
            client.ack(job.lease_job_id, ack_status, code)
        except Exception:  # noqa: BLE001 - terminal uncertainty, never re-offered
            pass

    summary = {
        "job_id": job.lease_job_id,
        "run_id": job.run_id,
        "status": result.status,
        "error_code": result.error_code,
        "callback_accepted": callback_accepted,
    }
    if local_diagnostics_unavailable:
        summary["local_diagnostics_unavailable"] = True
    return summary


def _default_storage_factory(settings: ConnectorSettings) -> StorageFactory:
    def factory(job: ByocJob) -> CustomerStorage:
        hint = job.storage.backend if job.storage else None
        return build_storage(settings, hint)

    return factory


def _job_from_envelope(
    client: ConnectorClient, envelope: dict[str, Any]
) -> Optional[ByocJob]:
    """Parse a leased envelope; refuse (ack failed) a payload we cannot parse."""
    payload = dict(envelope.get("payload") or {})
    lease_id = envelope.get("id")
    try:
        return parse_job(payload, lease_job_id=lease_id)
    except ByocJobParseError:
        if isinstance(lease_id, str):
            try:
                client.ack(lease_id, "failed", "job_parse_failed")
            except Exception:  # noqa: BLE001
                pass
        return None


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
    job = _job_from_envelope(client, envelope)
    if job is None:
        return {
            "job_id": envelope.get("id"),
            "status": "failed",
            "error_code": "job_parse_failed",
        }
    factory = storage_factory or _default_storage_factory(settings)
    return handle_job(
        client,
        settings,
        job,
        runner=runner,
        storage_factory=factory,
        require_run_token=require_run_token,
    )


class _ActiveJobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.futures: dict[str, Future[dict[str, Any]]] = {}

    def ids(self) -> list[str]:
        with self.lock:
            return list(self.futures)

    def reap(self, log: Callable[[str], None]) -> int:
        done = 0
        with self.lock:
            for job_id, fut in list(self.futures.items()):
                if fut.done():
                    self.futures.pop(job_id)
                    done += 1
                    try:
                        log(f"[connector] job done: {fut.result()}")
                    except Exception as exc:  # noqa: BLE001
                        log(f"[connector] job crashed: {type(exc).__name__}")
        return done


def apply_control(
    control: ConnectorControl, active: list[str], log: Callable[[str], None]
) -> None:
    """Terminate children the control plane cancelled, or all on a kill switch."""
    targets = (
        set(active)
        if control.runs_stopped
        else set(active) & set(control.cancel_job_ids)
    )
    for job_id in sorted(targets):
        if cancel_job(job_id):
            log(f"[connector] stopped job {job_id} on control-plane request")


def maybe_rotate(
    client: ConnectorClient,
    persist_token: Optional[TokenPersister],
    log: Callable[[str], None],
    *,
    persist_pending: Optional[TokenPersister] = None,
    force: bool = False,
    attempts: int = 3,
) -> bool:
    """Rotate the token when the control plane recommends it and we can persist.

    The new token is minted here and only its hash is sent. It is written to
    ``pending_token`` first, so a crash or a lost response can be finished on
    the next start by repeating the same request (the server answers
    ``already_rotated``) instead of stranding the connector.
    """
    if (not force and not client.control.rotate_recommended) or persist_token is None:
        return False
    from openadapt_flow.connector.client import mint_token

    new_token = mint_token()
    if persist_pending is not None:
        persist_pending(new_token)
    return finish_rotation(client, new_token, persist_token, log, attempts=attempts)


def finish_rotation(
    client: ConnectorClient,
    new_token: str,
    persist_token: TokenPersister,
    log: Callable[[str], None],
    *,
    attempts: int = 3,
) -> bool:
    for attempt in range(attempts):
        try:
            client.rotate(new_token)
            persist_token(new_token)  # durable before first use
            client.token = new_token
            client.control.rotate_recommended = False
            log("[connector] token rotated and persisted")
            return True
        except Exception as exc:  # noqa: BLE001 - keep the old token; retry
            log(f"[connector] token rotation attempt failed: {type(exc).__name__}")
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
    return False


def run_loop(
    client: ConnectorClient,
    settings: ConnectorSettings,
    *,
    runner: Runner,
    storage_factory: Optional[StorageFactory] = None,
    once: bool = False,
    require_run_token: bool = True,
    log: Callable[[str], None] = print,
    persist_token: Optional[TokenPersister] = None,
    persist_pending: Optional[TokenPersister] = None,
    heartbeat_s: float = HEARTBEAT_S,
) -> int:
    """Poll -> execute -> callback -> ack until interrupted.

    Up to ``min(settings.max_workers, server max_concurrent_leases)`` jobs run
    at once. While jobs run a heartbeat thread fetches control state, so a
    kill switch or cancel stops the governed children within ``heartbeat_s``.
    ``once`` runs a single poll cycle (cron-style). Returns an exit code.
    """
    if not client.token:
        return 1
    factory = storage_factory or _default_storage_factory(settings)
    log(f"[connector] polling {settings.control_plane_url} (org {settings.org_id})")

    if once:
        try:
            result = run_once(
                client,
                settings,
                runner=runner,
                storage_factory=factory,
                require_run_token=require_run_token,
            )
        except RunsStopped:
            log("[connector] organization runs are stopped; nothing leased")
            return 0
        except KeyboardInterrupt:  # pragma: no cover
            return 0
        if result is not None:
            log(f"[connector] job done: {result}")
        log(
            "[connector] loop exiting; jobs handled this session: "
            f"{1 if result is not None else 0}"
        )
        return 0

    active = _ActiveJobs()
    stop = threading.Event()
    pool = ThreadPoolExecutor(
        max_workers=settings.max_workers, thread_name_prefix="oa-job"
    )

    def heartbeat() -> None:
        while not stop.wait(heartbeat_s):
            ids = active.ids()
            if not ids:
                continue
            try:
                apply_control(client.heartbeat(ids), ids, log)
            except Exception as exc:  # noqa: BLE001 - next beat retries
                log(f"[connector] heartbeat failed: {type(exc).__name__}")

    beat = threading.Thread(target=heartbeat, name="oa-heartbeat", daemon=True)
    beat.start()
    handled = 0
    backoff = 1.0
    try:
        while True:
            handled += active.reap(log)
            cap = min(settings.max_workers, client.control.max_concurrent_leases)
            if len(active.ids()) >= cap:
                time.sleep(0.5)
                continue
            try:
                envelope = client.poll(
                    settings.poll_wait_s, active_job_ids=active.ids()
                )
                backoff = 1.0
            except RunsStopped:
                apply_control(client.control, active.ids(), log)
                log("[connector] organization runs are stopped; holding")
                time.sleep(STOPPED_BACKOFF_S)
                continue
            except (ConnectorClientError, httpx.TransportError, ValueError) as exc:
                log(
                    f"[connector] poll failed ({type(exc).__name__}); retrying in {backoff:.0f}s"
                )
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            apply_control(client.control, active.ids(), log)
            maybe_rotate(client, persist_token, log, persist_pending=persist_pending)
            if not envelope:
                continue
            job = _job_from_envelope(client, envelope)
            if job is None or not job.lease_job_id:
                continue
            fut = pool.submit(
                handle_job,
                client,
                settings,
                job,
                runner=runner,
                storage_factory=factory,
                require_run_token=require_run_token,
            )
            with active.lock:
                active.futures[job.lease_job_id] = fut
    except KeyboardInterrupt:  # operator Ctrl-C or service stop
        log("[connector] interrupted; exiting")
    finally:
        stop.set()
        pool.shutdown(wait=True)
        handled += active.reap(log)
    log(f"[connector] loop exiting; jobs handled this session: {handled}")
    return 0
