"""runner_loop -- EXPERIMENTAL outbound runner lane (cloud dispatch -> local execution).

Implements the desktop half of the hosted runner platform (P0): an outbound
HTTPS long-poll loop against ``/api/runners/*`` on the hosted control plane
(``register -> poll -> lease -> run -> callback -> ack``), per the 2026-07-17
runner-platform spec. The cloud is a coordination and evidence plane, not a
data plane: everything this module sends over the wire is PHI-free by
construction (digests, counts, step ids, states -- never screenshots, never
resolved values, never record contents).

Non-negotiables enforced here:

* **Local revalidation.** A dispatch carries a ``GovernedRunAuthorization``
  minted by the cloud. Before any GUI action the agent revalidates it locally:
  the staged bundle's sealed-manifest digest must match BOTH the dispatch's
  ``bundle.content_digest`` and the authorization's ``bundle_content_digest``;
  when the ``openadapt_flow`` library is importable its
  ``validate_execution_snapshot`` runs as the authoritative check. ANY mismatch
  refuses the run (ack outcome ``refused``) with a PHI-free reason (digest
  prefixes and step ids only) before the flow engine is ever invoked.
* **Idempotency / uncertain-on-restart.** A local journal records every leased
  run's phase. A run that reached ``started`` and did not finish (crash or
  restart mid-run) is NEVER silently re-executed: on the next loop start it is
  acked ``uncertain`` and left for operator/cloud reconciliation, mirroring the
  spec's "lease expiry after running -> uncertain, never silent re-dispatch".
* **PHI-free evidence.** Evidence events are built by whitelisting the exact
  spec fields and then re-checked by a fail-closed guard
  (:func:`assert_phi_free`) that refuses to serialize forbidden keys.

The whole lane is experimental and OFF by default (``runner_enabled=false``);
the cloud half is built in parallel -- this module codes to the spec's wire
format, not to a particular server implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform as _platform
import random
import sys
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

from engine.auth.store import (
    auth_header,
    clear_runner_credential,
    load_runner_credential,
    store_runner_credential,
)
from engine.config import EngineConfig
from engine.flow_bridge import FlowBridge

# --- wire constants (spec section 2) --------------------------------------------------

EVIDENCE_SCHEMA = "openadapt.run-evidence/v1"

REGISTER_PATH = "/api/runners/register"
POLL_PATH = "/api/runners/poll"
EXTEND_PATH = "/api/runners/extend"
ACK_PATH = "/api/runners/ack"


def evidence_path(run_id: str) -> str:
    """The per-run evidence endpoint path."""
    return f"/api/runs/{run_id}/evidence"


DEFAULT_WAIT_S = 25
DEFAULT_LEASE_S = 900
LEASE_EXTEND_INTERVAL_S = 300
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0

# --- PHI boundary (spec section 3) ----------------------------------------------------

# Keys that must NEVER appear anywhere in an outbound evidence/ack payload.
# Belt-and-braces: events are built by whitelist, then re-scanned against this.
FORBIDDEN_EVIDENCE_KEYS = frozenset({
    "field_values", "report_body", "dom", "html",
    "screenshot", "screenshots", "image", "images", "frames", "video",
    "matched_records", "observed", "expected", "values", "value",
    "params", "selector", "resolved_selector", "target", "text",
    "file_path", "capture_path", "bundle_path", "run_path",
})

_STEP_FIELDS = (
    "step_id", "rung", "effect_contract_hashes", "effect_verified",
    "effect_approved_unverified", "identity_verified", "elapsed_ms",
)
_HALT_FIELDS = (
    "task_id", "kind", "substrate", "effect_kind", "contract_hash", "verdict",
    "reason", "evidence_digest", "suggested_action", "step_id", "rung",
    "drift_signature",
)


class PhiBoundaryError(RuntimeError):
    """Raised when a payload would violate the PHI-free evidence boundary."""


class Refusal(RuntimeError):
    """A dispatch was refused before execution; ``str(exc)`` is the PHI-free reason."""


class ReauthRequired(RuntimeError):
    """The cloud rejected our token (401); the user must re-login. Never retry-loop."""


def assert_phi_free(obj: Any, path: str = "$") -> None:
    """Fail-closed recursive scan: refuse any payload carrying a forbidden key.

    Args:
        obj: The JSON-serializable payload about to cross the wire.
        path: Position breadcrumb used in the error message.

    Raises:
        PhiBoundaryError: If any (nested) dict key is in
            :data:`FORBIDDEN_EVIDENCE_KEYS`.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in FORBIDDEN_EVIDENCE_KEYS:
                raise PhiBoundaryError(
                    f"forbidden key '{key}' at {path} would cross the PHI boundary"
                )
            assert_phi_free(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            assert_phi_free(item, f"{path}[{i}]")


def _counts_only(evidence_digest: Any) -> dict:
    """Reduce a ReconciliationTask evidence dict to counts (spec: counts ONLY).

    Only integer values under keys ending in ``_count`` survive; the
    observed/expected VALUES and matched_records never cross the wire.
    """
    if not isinstance(evidence_digest, dict):
        return {}
    return {
        k: v
        for k, v in evidence_digest.items()
        if str(k).endswith("_count") and isinstance(v, int) and not isinstance(v, bool)
    }


def _digest_prefix(digest: Any) -> str:
    """A short, PHI-free digest prefix for refusal reasons."""
    s = str(digest or "")
    return s[:12] if s else "<absent>"


# --- backoff --------------------------------------------------------------------------


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Jittered exponential backoff: 1s -> 2 -> 4 ... capped at 60s (spec 2.2).

    Jitter multiplies the exponential value by a factor in [0.5, 1.0] so a
    fleet of runners never thundering-herds the poll route.
    """
    rng = rng or random
    exp = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt)))
    return exp * (0.5 + rng.random() / 2.0)


# --- bundle digest + staging ----------------------------------------------------------


def bundle_content_digest(bundle_dir: Path) -> str:
    """Compute the sealed-manifest content digest of a local bundle.

    The dispatch's ``bundle.content_digest`` is defined by the spec as the
    sealed manifest digest, i.e. sha256 over the bundle's ``manifest.json``
    bytes. If the manifest self-declares a ``content_digest`` that disagrees
    with the recomputed value the bundle is considered tampered.

    Raises:
        Refusal: If the manifest is missing or self-inconsistent (fail closed).
    """
    manifest = Path(bundle_dir) / "manifest.json"
    if not manifest.is_file():
        raise Refusal("bundle has no sealed manifest; refusing to execute")
    raw = manifest.read_bytes()
    computed = hashlib.sha256(raw).hexdigest()
    try:
        declared = json.loads(raw).get("content_digest")
    except (json.JSONDecodeError, AttributeError):
        declared = None
    if declared and declared != computed:
        raise Refusal(
            "bundle manifest self-digest mismatch "
            f"(declared {_digest_prefix(declared)}, computed {_digest_prefix(computed)})"
        )
    return computed


def safe_extract_zip(archive: Path, dest: Path) -> None:
    """Extract a bundle archive, refusing path-traversal member names."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            member_path = (dest / member).resolve()
            if not str(member_path).startswith(str(dest.resolve())):
                raise Refusal("bundle archive contains an unsafe member path")
        zf.extractall(dest)


def validate_dispatch(job: dict, bundle_dir: Path, *, now: datetime | None = None) -> None:
    """Locally revalidate a governed-run dispatch before ANY GUI action.

    Checks (all fail closed, all reasons PHI-free):
      1. shape -- ``job_kind == "governed_run"``, run id, authorization present;
      2. dispatch expiry (``expires_at``) has not passed;
      3. the staged bundle's recomputed digest matches the dispatch's
         ``bundle.content_digest`` AND the authorization's
         ``bundle_content_digest`` (which must also agree with each other);
      4. when ``openadapt_flow`` is importable, its
         ``validate_execution_snapshot`` runs as the authoritative check
         (sealed asset hashes, runtime-inputs digest, single-use).

    Raises:
        Refusal: On ANY mismatch, with a digest-prefix/step-id-only reason.
    """
    if job.get("job_kind") != "governed_run":
        raise Refusal(f"unsupported job_kind '{job.get('job_kind')}'")
    if not job.get("run_id"):
        raise Refusal("dispatch missing run_id")
    authorization = job.get("authorization")
    if not isinstance(authorization, dict) or not authorization.get("authorization_id"):
        raise Refusal("dispatch missing governed-run authorization")

    expires_at = job.get("expires_at")
    if expires_at:
        try:
            deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            raise Refusal("dispatch expires_at is unparseable") from None
        if (now or datetime.now(timezone.utc)) >= deadline:
            raise Refusal("dispatch expired before start")

    dispatch_digest = (job.get("bundle") or {}).get("content_digest")
    auth_digest = authorization.get("bundle_content_digest")
    if not dispatch_digest or not auth_digest:
        raise Refusal("dispatch missing bundle content digest")
    if dispatch_digest != auth_digest:
        raise Refusal(
            "authorization/bundle digest mismatch "
            f"(dispatch {_digest_prefix(dispatch_digest)}, "
            f"authorization {_digest_prefix(auth_digest)})"
        )
    local_digest = bundle_content_digest(bundle_dir)
    if local_digest != auth_digest:
        raise Refusal(
            "local bundle digest mismatch "
            f"(local {_digest_prefix(local_digest)}, "
            f"authorized {_digest_prefix(auth_digest)})"
        )
    _flow_validate(authorization, bundle_dir)


def _flow_validate(authorization: dict, bundle_dir: Path) -> None:
    """Run openadapt-flow's ``validate_execution_snapshot`` when importable.

    The engine wraps the flow CLI and may not share a Python environment with
    it; when the library is absent the engine-side digest gate above remains
    the enforced check and the flow runtime re-refuses at execution time.
    When the library IS present, any validation failure refuses (fail closed).
    """
    try:
        from openadapt_flow.runtime.authorization import (  # type: ignore[import-not-found]
            GovernedRunAuthorization,
            validate_execution_snapshot,
        )
    except Exception:
        logger.debug("openadapt_flow not importable; engine digest gate only")
        return
    try:
        auth = GovernedRunAuthorization.model_validate(authorization)
        validate_execution_snapshot(auth, Path(bundle_dir))
    except Exception as exc:
        raise Refusal(f"authorization revalidation refused: {exc}") from None


# --- evidence builders ----------------------------------------------------------------


def _step_event(step: dict, index: int) -> dict:
    """Whitelist one report step into a spec ``step`` evidence payload."""
    hashes = step.get("effect_contract_hashes")
    if not isinstance(hashes, list):
        single = step.get("contract_hash")
        hashes = [single] if single else []
    payload: dict[str, Any] = {
        "step_id": step.get("step_id") or f"s{index}",
        "rung": step.get("rung") or step.get("resolver_rung"),
        "effect_contract_hashes": [str(h) for h in hashes],
        "effect_verified": bool(
            step.get("effect_verified", step.get("effect") == "verified")
        ),
        "effect_approved_unverified": bool(step.get("effect_approved_unverified", False)),
        "elapsed_ms": step.get("elapsed_ms", step.get("latency_ms")),
    }
    if "identity_verified" in step:
        payload["identity_verified"] = bool(step["identity_verified"])
    return payload


def _halt_event(halt: dict) -> dict:
    """Whitelist a halt block into the spec ``halt`` payload (digests/counts only)."""
    payload: dict[str, Any] = {}
    for key in _HALT_FIELDS:
        if key in halt:
            payload[key] = halt[key]
    payload["task_id"] = payload.get("task_id") or f"recon-{uuid.uuid4().hex[:8]}"
    payload["kind"] = payload.get("kind") or "resolver_halt"
    payload["evidence_digest"] = _counts_only(halt.get("evidence_digest"))
    if "step_id" not in payload and halt.get("step_index") is not None:
        payload["step_id"] = f"s{halt['step_index']}"
    if "rung" not in payload and halt.get("resolver_rung"):
        payload["rung"] = halt["resolver_rung"]
    if "reason" not in payload:
        payload["reason"] = str(halt.get("reason", ""))[:500]
    return payload


def _run_summary(job: dict, report: dict, status: str) -> dict:
    """Build the terminal ``run_summary`` payload from a local ``report.json``."""
    raw_steps = report.get("steps")
    steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
    events = [_step_event(s, i) for i, s in enumerate(steps)]
    with_effects = [e for e in events if e["effect_contract_hashes"]]
    metrics = report.get("metrics") or {}
    duration_s = metrics.get("duration_s")
    identity = [e for e in events if "identity_verified" in e]
    return {
        "bundle_digest": (job.get("bundle") or {}).get("content_digest", ""),
        "authorization_id": (job.get("authorization") or {}).get("authorization_id", ""),
        "status": status,
        "steps_total": int(report.get("total_steps") or len(steps)),
        "consequential_steps": int(report.get("consequential_steps", len(with_effects))),
        "effect_covered_consequential_steps": int(
            report.get("effect_covered_consequential_steps", len(with_effects))
        ),
        "effects_confirmed": sum(1 for e in with_effects if e["effect_verified"]),
        "effects_approved_unverified": sum(
            1 for e in with_effects if e["effect_approved_unverified"]
        ),
        "identity_steps_required": int(
            report.get("identity_steps_required", len(identity))
        ),
        "identity_steps_verified": sum(
            1 for e in identity if e.get("identity_verified")
        ),
        "duration_ms": int(duration_s * 1000) if isinstance(duration_s, (int, float)) else None,
        "screenshots_may_leave_box": False,  # assertion, not a toggle (spec section 3)
    }


# --- runner journal (idempotency) -----------------------------------------------------


class RunnerJournal:
    """Durable per-run phase journal: ``leased -> started -> finished``.

    The journal is the local source of truth for the never-re-run rule: any
    run recorded ``started`` without a terminal record must be reported
    ``uncertain`` after a restart, never re-executed.
    """

    def __init__(self, journal_dir: Path) -> None:
        self._dir = journal_dir

    def _path(self, run_id: str) -> Path:
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_") or "run"
        return self._dir / f"{safe}.json"

    def record(self, run_id: str, phase: str, **extra: Any) -> None:
        """Persist a phase transition for ``run_id`` (merges over prior fields)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        entry = self.get(run_id) or {"run_id": run_id}
        entry.update(extra)
        entry["phase"] = phase
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._path(run_id).write_text(json.dumps(entry, indent=2))

    def get(self, run_id: str) -> dict | None:
        """Return the journal entry for ``run_id``, or None."""
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def entries(self) -> list[dict]:
        """All journal entries, newest first."""
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True):
            try:
                out.append(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def unfinished_started(self) -> list[dict]:
        """Runs that began executing but never reached a terminal phase."""
        return [e for e in self.entries() if e.get("phase") == "started"]

    def last_runs(self, limit: int = 10) -> list[dict]:
        """Recent runs for the UI (run_id / phase / outcome / timestamps only)."""
        keep = ("run_id", "phase", "outcome", "reason", "updated_at", "workflow_id")
        return [
            {k: e.get(k) for k in keep if k in e}
            for e in self.entries()[:limit]
        ]


# --- HTTP client ----------------------------------------------------------------------


class RunnerClient:
    """Thin async client for the ``/api/runners/*`` control-plane surface."""

    def __init__(self, http: httpx.AsyncClient, token: str | None = None) -> None:
        self._http = http
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def register(self, session_token: str, payload: dict) -> dict:
        """Register this machine as a runner; returns ``{runner_id, runner_token}``."""
        resp = await self._http.post(
            REGISTER_PATH, json=payload,
            headers={"Authorization": f"Bearer {session_token}"},
        )
        if resp.status_code == 401:
            raise ReauthRequired("registration rejected; re-login required")
        resp.raise_for_status()
        data = resp.json()
        self.token = data.get("runner_token") or self.token
        return data

    async def poll(self, wait: int = DEFAULT_WAIT_S,
                   lease_seconds: int = DEFAULT_LEASE_S) -> dict | None:
        """Long-poll for a dispatch; returns the leased job or None on 204."""
        resp = await self._http.post(
            POLL_PATH,
            json={"wait": wait, "lease_seconds": lease_seconds},
            headers=self._headers(),
            timeout=wait + 30,
        )
        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            raise ReauthRequired("runner token rejected; re-login required")
        resp.raise_for_status()
        body = resp.json()
        job = body.get("job") if isinstance(body, dict) else None
        return job if isinstance(job, dict) else None

    async def extend(self, job_id: str) -> None:
        """Renew the lease on a running job (heartbeat while executing)."""
        resp = await self._http.post(
            EXTEND_PATH, json={"job_id": job_id}, headers=self._headers()
        )
        resp.raise_for_status()

    async def post_evidence(self, run_id: str, event: dict) -> None:
        """POST one PHI-free evidence event (fail-closed on 422)."""
        assert_phi_free(event)
        resp = await self._http.post(
            evidence_path(run_id), json=event, headers=self._headers()
        )
        if resp.status_code == 422:
            raise PhiBoundaryError(
                "server rejected evidence as a PHI-boundary violation (422)"
            )
        resp.raise_for_status()

    async def ack(self, job_id: str, outcome: str, *, run_id: str | None = None,
                  reason: str | None = None) -> None:
        """Terminal ack for a leased job (``confirmed`` / ``halted-needs-attention``
        / ``failed`` / ``refused`` / ``uncertain``)."""
        payload: dict[str, Any] = {"job_id": job_id, "outcome": outcome}
        if run_id:
            payload["run_id"] = run_id
        if reason:
            payload["reason"] = reason[:500]
        assert_phi_free(payload)
        resp = await self._http.post(ACK_PATH, json=payload, headers=self._headers())
        resp.raise_for_status()


# --- the service ----------------------------------------------------------------------


class RunnerService:
    """Owns the runner loop lifecycle + status surface for the desktop UI.

    States surfaced to the UI: ``disabled`` / ``offline`` (enabled, not yet
    connected or backing off) / ``polling`` / ``running`` /
    ``reauth_required`` / ``error``.

    Args:
        config: Engine configuration (host, data dir, enabled flag).
        services: The shared :class:`~engine.dispatch.EngineServices` container
            (flow bridge + db reused; the runner never duplicates verbs).
        emit: Event sink (``emit(event, data)``) shared with the dispatcher.
        http_factory: Builds the ``httpx.AsyncClient`` (injected in tests).
        rng: Randomness source for backoff jitter (injected in tests).
    """

    def __init__(
        self,
        config: EngineConfig,
        services: Any,
        *,
        emit: Callable[[str, dict], None] | None = None,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.services = services
        self.emit = emit or (lambda event, data: None)
        self._http_factory = http_factory or self._default_http_factory
        self._rng = rng or random.Random()
        self.journal = RunnerJournal(config.data_dir / "runner" / "jobs")
        self._state = "disabled" if not config.runner_enabled else "offline"
        self._last_error: str | None = None
        self._last_seen_at: str | None = None
        self._attempt = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- status / lifecycle ----

    def status(self) -> dict:
        """The ``RunnerStatus``-shaped dict the UI renders."""
        cred = load_runner_credential(self.config.hosted_host)
        return {
            "enabled": bool(self.config.runner_enabled),
            "state": self._state,
            "runner_id": (cred or {}).get("runner_id"),
            "registered": bool(cred),
            "host": self.config.hosted_host,
            "last_error": self._last_error,
            "last_seen_at": self._last_seen_at,
            "last_runs": self.journal.last_runs(),
        }

    def enable(self) -> dict:
        """Enable the runner lane and start the loop thread."""
        self.config.runner_enabled = True
        self.start()
        return self.status()

    def disable(self) -> dict:
        """Disable the runner lane and stop the loop thread."""
        self.config.runner_enabled = False
        self.stop()
        self._set_state("disabled")
        return self.status()

    def deregister(self) -> None:
        """Forget this machine's runner credential (re-enroll to rejoin)."""
        clear_runner_credential(self.config.hosted_host)

    def start(self) -> None:
        """Start the background loop thread (no-op if already running)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state("offline")
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="runner-loop"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait briefly for the thread to exit."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - crash guard
            logger.exception("runner loop crashed")
            self._last_error = str(exc)
            self._set_state("error")

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            try:
                self.emit("runner_state", self.status())
            except Exception:  # pragma: no cover - emit must never kill the loop
                logger.exception("runner_state emit failed")

    def _default_http_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.hosted_host, timeout=DEFAULT_WAIT_S + 35
        )

    # ---- registration ----

    def _register_payload(self) -> dict:
        from engine import __version__

        os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
        backends = {"macos": ["web", "rdp_window"], "windows": ["web", "windows"]}.get(
            os_name, ["web"]
        )
        return {
            "name": _platform.node() or "desktop-runner",
            "platform": os_name,
            "agent_version": __version__,
            "engine_version": "",
            "capabilities": {
                "backends": backends,
                "attended": True,
                "effects_substrates": [],
            },
            "mode": "attended",
        }

    async def ensure_registered(self, client: RunnerClient) -> bool:
        """Load or mint the per-runner token; False when re-login is required."""
        cred = load_runner_credential(self.config.hosted_host)
        if cred and cred.get("runner_token"):
            client.token = cred["runner_token"]
            return True
        session = auth_header().get("Authorization", "")
        if not session.startswith("Bearer "):
            self._last_error = "not signed in; log in before enabling the runner"
            self._set_state("reauth_required")
            return False
        try:
            data = await client.register(
                session.removeprefix("Bearer "), self._register_payload()
            )
        except ReauthRequired:
            self._set_state("reauth_required")
            return False
        runner_id = data.get("runner_id", "")
        runner_token = data.get("runner_token", "")
        if not runner_token:
            self._last_error = "registration returned no runner token"
            self._set_state("error")
            return False
        store_runner_credential(self.config.hosted_host, runner_id, runner_token)
        return True

    # ---- the loop ----

    async def _main(self) -> None:
        async with self._http_factory() as http:
            client = RunnerClient(http)
            if not await self.ensure_registered(client):
                return
            await self.reconcile_restart(client)
            while not self._stop.is_set():
                delay = await self._tick(client)
                if delay is None:
                    return
                if delay > 0:
                    await asyncio.sleep(delay)

    async def _tick(self, client: RunnerClient) -> float | None:
        """One poll iteration; returns the next sleep delay, None to stop."""
        try:
            self._set_state("polling")
            job = await client.poll(
                wait=DEFAULT_WAIT_S, lease_seconds=DEFAULT_LEASE_S
            )
            self._last_seen_at = datetime.now(timezone.utc).isoformat()
        except ReauthRequired:
            self._last_error = "runner token rejected; re-login required"
            self._set_state("reauth_required")
            return None
        except (httpx.HTTPError, OSError) as exc:
            self._last_error = str(exc)
            self._set_state("offline")
            delay = backoff_delay(self._attempt, self._rng)
            self._attempt += 1
            return delay
        self._attempt = 0
        self._last_error = None
        if job is None:
            return 0.0
        try:
            await self.handle_job(client, job)
        except (httpx.HTTPError, OSError) as exc:
            # A dropped callback/ack never crashes the loop; the cloud's lease
            # expiry semantics land the run `uncertain` server-side.
            self._last_error = str(exc)
            logger.warning("job handling hit a network error: {e}", e=exc)
            delay = backoff_delay(self._attempt, self._rng)
            self._attempt += 1
            return delay
        return 0.0

    async def reconcile_restart(self, client: RunnerClient) -> None:
        """Report every started-but-unfinished journaled run as ``uncertain``.

        Never re-executes (spec 2.4/2.5: a run that reached ``running`` is
        never silently re-performed). The journal entry only turns terminal
        once the cloud accepted the ack, so an offline ack retries next start.
        """
        for entry in self.journal.unfinished_started():
            run_id = entry.get("run_id", "")
            job_id = entry.get("job_id", "")
            reason = "engine restarted mid-run; outcome unknown; not re-executed"
            try:
                await client.ack(job_id, "uncertain", run_id=run_id, reason=reason)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("uncertain ack for {r} deferred: {e}", r=run_id, e=exc)
                continue
            self.journal.record(run_id, "finished", outcome="uncertain", reason=reason)

    # ---- job handling ----

    async def handle_job(self, client: RunnerClient, job: dict) -> None:
        """Validate -> execute -> stream evidence -> ack for one leased job."""
        run_id = str(job.get("run_id") or "")
        job_id = str((job.get("lease") or {}).get("job_id") or "")
        if not run_id or not job_id:
            logger.warning("dispatch missing run_id/lease.job_id; ignoring")
            return

        existing = self.journal.get(run_id)
        if existing and existing.get("phase") == "started":
            # Idempotency: this run already began executing (e.g. re-leased
            # after a crash). NEVER silently re-execute.
            reason = "run was already started on this runner; outcome uncertain"
            await client.ack(job_id, "uncertain", run_id=run_id, reason=reason)
            self.journal.record(run_id, "finished", outcome="uncertain", reason=reason)
            return
        if existing and existing.get("phase") == "finished":
            await client.ack(
                job_id, existing.get("outcome", "uncertain"), run_id=run_id,
                reason="duplicate lease of a finished run",
            )
            return

        self.journal.record(
            run_id, "leased", job_id=job_id, workflow_id=job.get("workflow_id")
        )
        seq = _Seq()
        authorization_id = (job.get("authorization") or {}).get("authorization_id", "")
        try:
            bundle_dir = await self._stage_bundle(job)
            validate_dispatch(job, bundle_dir)
        except Refusal as refusal:
            reason = str(refusal)
            logger.warning("dispatch {r} refused: {why}", r=run_id, why=reason)
            self.journal.record(run_id, "finished", outcome="refused", reason=reason)
            await client.ack(job_id, "refused", run_id=run_id, reason=reason)
            return

        self.journal.record(run_id, "started")
        self._set_state("running")
        await self._evidence(
            client, run_id, authorization_id, seq, "state",
            {"state": "started", "at": datetime.now(timezone.utc).isoformat()},
        )
        run_dir = self.config.data_dir / "runner" / "runs" / run_id
        extend_task = asyncio.ensure_future(self._extend_loop(client, job_id))
        try:
            result = await asyncio.to_thread(
                self._execute, bundle_dir, run_dir, job.get("authorization") or {}
            )
            exec_error: str | None = None
            exec_ok = bool(getattr(result, "ok", False))
        except Exception as exc:
            exec_error = str(exc)
            exec_ok = False
        finally:
            extend_task.cancel()

        report = FlowBridge.read_report(run_dir)
        halt = FlowBridge.read_halt(run_dir)
        raw_steps = report.get("steps")
        steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
        for index, step in enumerate(steps):
            await self._evidence(
                client, run_id, authorization_id, seq, "step", _step_event(step, index)
            )
        if halt:
            status = "halted-needs-attention"
            await self._evidence(
                client, run_id, authorization_id, seq, "halt", _halt_event(halt)
            )
        elif exec_ok:
            status = "confirmed"
        else:
            status = "failed"
        await self._evidence(
            client, run_id, authorization_id, seq, "run_summary",
            _run_summary(job, report, status),
        )
        self._record_local_run(run_id, run_dir, job, halt, status)
        self.journal.record(
            run_id, "finished", outcome=status,
            reason=(exec_error or "")[:200] or None,
        )
        await client.ack(job_id, status, run_id=run_id)
        self._set_state("polling")

    async def _extend_loop(self, client: RunnerClient, job_id: str) -> None:
        """Renew the lease periodically while a run executes (spec Q6)."""
        while True:
            await asyncio.sleep(LEASE_EXTEND_INTERVAL_S)
            try:
                await client.extend(job_id)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("lease extend failed: {e}", e=exc)

    async def _stage_bundle(self, job: dict) -> Path:
        """Locate or download the sealed bundle for a dispatch.

        Order: the runner's own digest-keyed store, then the dispatch's
        short-lived signed URL (downloaded and safely extracted).
        """
        bundle_info = job.get("bundle") or {}
        digest = str(bundle_info.get("content_digest") or "")
        if not digest:
            raise Refusal("dispatch missing bundle content digest")
        store_dir = self.config.data_dir / "runner" / "bundles" / digest
        if (store_dir / "manifest.json").is_file():
            return store_dir
        url = bundle_info.get("url")
        if not url:
            raise Refusal(
                f"bundle {_digest_prefix(digest)} not in local store and no staging URL"
            )
        archive = store_dir.with_suffix(".zip")
        store_dir.parent.mkdir(parents=True, exist_ok=True)
        async with self._http_factory() as http:
            resp = await http.get(url)
            resp.raise_for_status()
            archive.write_bytes(resp.content)
        try:
            safe_extract_zip(archive, store_dir)
        finally:
            archive.unlink(missing_ok=True)
        return store_dir

    def _execute(self, bundle_dir: Path, run_dir: Path, authorization: dict) -> Any:
        """Execute via the existing flow bridge (blocking; runs in a thread).

        Persists the authorization JSON into the run dir (operator audit copy)
        and forwards it to ``openadapt-flow run --authorization-file`` when the
        installed flow CLI supports that flag (a PROPOSED flow follow-up).
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        auth_path = run_dir / "authorization.json"
        auth_path.write_text(json.dumps(authorization, indent=2))
        bridge = self.services.flow_bridge
        config_path = self.config.data_dir / "deployment.json"
        kwargs: dict[str, Any] = {}
        probe = getattr(bridge, "run_supports_authorization", None)
        if callable(probe) and probe():
            kwargs["authorization_file"] = auth_path
        return bridge.run(bundle_dir, config_path, out_dir=run_dir, **kwargs)

    def _record_local_run(self, run_id: str, run_dir: Path, job: dict,
                          halt: dict | None, status: str) -> None:
        """Mirror the run (and any halt) into the local index DB, best-effort."""
        try:
            self.services.db.insert_run(run_id, str(run_dir), bundle_id=None)
            self.services.db.update_run(run_id, status=status)
            if halt:
                self.services.db.insert_halt(
                    f"halt-{run_id}", run_id,
                    workflow_id=job.get("workflow_id"),
                    reason=str(halt.get("reason", ""))[:500],
                    resolver_rung=halt.get("resolver_rung") or halt.get("rung"),
                    drift_signature=halt.get("drift_signature"),
                )
        except Exception:
            logger.exception("local run mirror failed (non-fatal)")

    async def _evidence(self, client: RunnerClient, run_id: str,
                        authorization_id: str, seq: _Seq, kind: str,
                        payload: dict) -> None:
        """Send one evidence event; PHI violations fail closed and abort nothing else."""
        event: dict[str, Any] = {
            "schema": EVIDENCE_SCHEMA,
            "run_id": run_id,
            "authorization_id": authorization_id,
            "seq": seq.next(),
            "kind": kind,
            kind: payload,
        }
        try:
            await client.post_evidence(run_id, event)
        except PhiBoundaryError:
            # Fail closed: drop the event, never widen it. The full-fidelity
            # evidence stays in the local run dir (the operator's audit copy).
            logger.error("evidence event for {r} violated the PHI boundary; dropped",
                         r=run_id)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("evidence POST failed (run continues): {e}", e=exc)


class _Seq:
    """Monotone per-run sequence counter for evidence events."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n
