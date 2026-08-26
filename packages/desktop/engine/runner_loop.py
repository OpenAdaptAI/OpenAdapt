"""Outbound runner lane for governed cloud dispatch to local execution.

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
* **The org's safety policy binds the run.** Immediately before executing (and
  before any GUI action), the runner resolves the org's effective policy and
  projects its Tier-3 ``safety`` block onto the deployment config Flow is given,
  via :func:`engine.policy.binding_safety` +
  :func:`engine.policy.apply_safety_policy`. A policy that cannot be resolved
  authoritatively, carries an unknown value, or cannot be bound REFUSES the
  dispatch (ack outcome ``refused``) -- the run never falls back to whatever the
  local deployment config happened to say.
* **Exit code zero is not proof.** A Flow process that returns ``0`` proves only
  that the local process returned. It does not prove the governed effect. This
  lane does not yet consume Flow's shared qualification-v2 verifier, so it
  cannot bind an exact signed ``VERIFIED`` result to the run, authorization,
  policy, identity, effect, and event sequence. Until it can, a run that
  completes without a halt is acked ``halted-needs-attention`` with
  :data:`COMPLETION_PROOF_REQUIRED_REASON`; it is NEVER acked ``confirmed``.

The lane is OFF by default (``runner_enabled=false``). This module codes to the
specified wire format, not to a particular server implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform as _platform
import random
import re
import shutil
import stat
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import yaml
from loguru import logger

from engine import policy as policy_mod
from engine.auth.store import (
    auth_header,
    clear_runner_credential,
    load_runner_credential,
    store_runner_credential,
)
from engine.config import EngineConfig
from engine.flow_bridge import FlowBridge
from engine.private_flow_config import PreparedPrivateYaml, stage_private_yaml

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
MAX_BUNDLE_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_UNPACKED_BYTES = 4 * MAX_BUNDLE_ARCHIVE_BYTES
MAX_BUNDLE_MEMBERS = 100_000

_SHA256_RE = re.compile(r"[a-f0-9]{64}")
_CONTRACT_HASH_RE = re.compile(r"sha256:[a-f0-9]{64}")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_RUNGS = frozenset({"structural", "template", "ocr", "geometry"})

# Terminal outcome for a run whose Flow process returned but which carries no
# signed qualification-v2 VERIFIED proof. Constant text: it crosses the ack
# boundary as ``reason`` and lands in the local halt mirror, so it must stay
# free of any run-derived value.
COMPLETION_PROOF_REQUIRED_REASON = (
    "run completed without the required signed qualification-v2 "
    "VERIFIED proof; operator reconciliation is required"
)
COMPLETION_PROOF_HALT_KIND = "completion_proof_missing"

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
class PhiBoundaryError(RuntimeError):
    """Raised when a payload would violate the PHI-free evidence boundary."""


class Refusal(RuntimeError):
    """A dispatch was refused before execution; ``str(exc)`` is the PHI-free reason."""


class ReauthRequired(RuntimeError):
    """The cloud rejected our token (401); the user must re-login. Never retry-loop."""


class RunnerJournalError(RuntimeError):
    """The durable runner journal cannot prove a safe prior run state."""


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
    """Extract only bounded regular ZIP members beneath ``dest``."""

    root = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise Refusal("bundle staging directory is not empty")
    seen: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_BUNDLE_MEMBERS:
            raise Refusal("bundle archive has too many members")
        if sum(member.file_size for member in members) > MAX_BUNDLE_UNPACKED_BYTES:
            raise Refusal("bundle archive expands beyond the runner limit")
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or name in seen
            ):
                raise Refusal("bundle archive contains an unsafe member path")
            seen.add(name)
            target = (root / Path(*path.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise Refusal("bundle archive contains an unsafe member path") from None
            file_type = stat.S_IFMT(member.external_attr >> 16)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise Refusal("bundle archive contains an unsupported member type")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod(0o600)
            except FileExistsError:
                raise Refusal("bundle archive contains a duplicate member path") from None


def _safe_bundle_url(value: Any) -> str:
    """Return a safe HTTPS URL, or loopback HTTP URL, for bundle download."""

    if not isinstance(value, str):
        raise Refusal("bundle staging URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise Refusal("bundle staging URL is invalid") from None
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"})
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise Refusal("bundle staging URL is invalid")
    return value


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
            if deadline.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
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


def _lease_deadline(job: dict, *, received_at: datetime) -> tuple[str, datetime]:
    """Return the exact lease id and the earliest locally enforceable deadline."""

    lease = job.get("lease")
    if not isinstance(lease, dict):
        raise Refusal("dispatch missing lease")
    job_id = lease.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", job_id):
        raise Refusal("dispatch lease id is invalid")
    visibility_timeout = lease.get("visibility_timeout_s")
    if (
        not isinstance(visibility_timeout, int)
        or isinstance(visibility_timeout, bool)
        or not 1 <= visibility_timeout <= DEFAULT_LEASE_S
    ):
        raise Refusal("dispatch lease timeout is invalid")
    local_deadline = received_at + timedelta(seconds=visibility_timeout)
    raw_deadline = lease.get("expires_at") or job.get("lease_expires_at")
    if raw_deadline is None:
        return job_id, local_deadline
    try:
        server_deadline = datetime.fromisoformat(
            str(raw_deadline).replace("Z", "+00:00")
        )
        if server_deadline.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise Refusal("dispatch lease expiry is unparseable") from None
    return job_id, min(local_deadline, server_deadline.astimezone(timezone.utc))


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
        raise Refusal(
            f"authorization revalidation refused ({type(exc).__name__})"
        ) from None


# --- evidence builders ----------------------------------------------------------------


def _safe_identifier(value: Any, *, fallback: str) -> str:
    """Return a bounded structural identifier without forwarding free text."""

    return value if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) else fallback


def _step_event(step: dict, index: int) -> dict:
    """Whitelist one report step into a spec ``step`` evidence payload."""
    hashes = step.get("effect_contract_hashes")
    if not isinstance(hashes, list):
        single = step.get("contract_hash")
        hashes = [single] if single else []
    rung = step.get("rung") or step.get("resolver_rung")
    payload: dict[str, Any] = {
        "step_id": _safe_identifier(step.get("step_id"), fallback=f"s{index}"),
        "rung": rung if rung in _RUNGS else None,
        "effect_contract_hashes": [
            value
            for value in hashes
            if isinstance(value, str) and _CONTRACT_HASH_RE.fullmatch(value)
        ],
        "effect_verified": bool(
            step.get("effect_verified", step.get("effect") == "verified")
        ),
        "effect_approved_unverified": bool(step.get("effect_approved_unverified", False)),
        "elapsed_ms": max(
            0,
            int(step.get("elapsed_ms", step.get("latency_ms")) or 0),
        ),
    }
    if "identity_verified" in step:
        payload["identity_verified"] = bool(step["identity_verified"])
    return payload


def _halt_event(
    halt: dict,
    *,
    run_id: str,
    workflow_id: str,
    step_count: int,
) -> dict:
    """Build a structural halt event without forwarding any free text."""

    rung = halt.get("rung") or halt.get("resolver_rung")
    rung = rung if rung in _RUNGS else None
    step_id = _safe_identifier(
        halt.get("step_id") or (
            f"s{halt['step_index']}" if isinstance(halt.get("step_index"), int) else None
        ),
        fallback="",
    )
    kind = halt.get("kind")
    if kind not in {
        "authorization_refused",
        "identity_halt",
        "effect_refuted",
        "effect_indeterminate",
        "compensation_failed",
        "resolver_halt",
    }:
        kind = "resolver_halt"
    payload: dict[str, Any] = {
        "task_id": f"halt-{run_id}"[:64],
        "kind": kind,
        "reason": f"halt at step {step_id}" if step_id else "halt at unidentified step",
        "drift_signature": hashlib.sha256(
            f"{workflow_id}|{rung}|{step_count}".encode("utf-8")
        ).hexdigest()[:16],
    }
    for key, allowed in {
        "substrate": {"api", "fhir", "sql", "onscreen", "web", "desktop"},
        "effect_kind": {"create", "update", "delete", "send", "submit", "write"},
        "verdict": {"confirmed", "refuted", "indeterminate"},
    }.items():
        value = halt.get(key)
        if value in allowed:
            payload[key] = value
    payload["evidence_digest"] = _counts_only(halt.get("evidence_digest"))
    contract_hash = halt.get("contract_hash")
    if isinstance(contract_hash, str) and _CONTRACT_HASH_RE.fullmatch(contract_hash):
        payload["contract_hash"] = contract_hash
    if step_id:
        payload["step_id"] = step_id
    if rung is not None:
        payload["rung"] = rung
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
        self._lock = threading.RLock()

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", run_id):
            raise RunnerJournalError("runner run id is not a safe journal key")
        return self._dir / f"{run_id}.json"

    def _read_path(self, path: Path, *, expected_run_id: str) -> dict:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            raise RunnerJournalError("runner journal is corrupt") from exc
        if (
            not isinstance(entry, dict)
            or entry.get("run_id") != expected_run_id
            or entry.get("phase") not in {"leased", "starting", "started", "finished"}
        ):
            raise RunnerJournalError("runner journal has an invalid state")
        return entry

    def record(self, run_id: str, phase: str, **extra: Any) -> None:
        """Persist a phase transition for ``run_id`` (merges over prior fields)."""
        if phase not in {"leased", "starting", "started", "finished"}:
            raise RunnerJournalError("runner journal phase is invalid")
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            try:
                self._dir.chmod(0o700)
            except OSError:
                pass
            entry = self.get(run_id) or {"run_id": run_id}
            entry.update(extra)
            entry["phase"] = phase
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            destination = self._path(run_id)
            fd, temporary = tempfile.mkstemp(
                dir=str(self._dir), prefix=f".{run_id}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(entry, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def get(self, run_id: str) -> dict | None:
        """Return the journal entry for ``run_id``, or None."""
        with self._lock:
            path = self._path(run_id)
            if not path.is_file():
                return None
            return self._read_path(path, expected_run_id=run_id)

    def entries(self) -> list[dict]:
        """All journal entries, newest first."""
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True):
            out.append(self._read_path(path, expected_run_id=path.stem))
        return out

    def unfinished_started(self) -> list[dict]:
        """Runs that began executing but never reached a terminal phase."""
        return [
            e for e in self.entries() if e.get("phase") in {"starting", "started"}
        ]

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
        policy_resolver: Resolves the org's effective policy immediately before
            a run (injected in tests). Defaults to
            :func:`engine.policy.resolve_effective_policy` against
            ``config.hosted_host``.
    """

    def __init__(
        self,
        config: EngineConfig,
        services: Any,
        *,
        emit: Callable[[str, dict], None] | None = None,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
        rng: random.Random | None = None,
        policy_resolver: Callable[[], dict] | None = None,
    ) -> None:
        self.config = config
        self.services = services
        self.emit = emit or (lambda event, data: None)
        self._http_factory = http_factory or self._default_http_factory
        self._rng = rng or random.Random()
        self._policy_resolver = policy_resolver or self._default_policy_resolver
        self.journal = RunnerJournal(config.data_dir / "runner" / "jobs")
        self._state = "disabled" if not config.runner_enabled else "offline"
        self._last_error: str | None = None
        self._last_seen_at: str | None = None
        self._attempt = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_lock = asyncio.Lock()
        self._handle_lock = asyncio.Lock()
        self._lifecycle_lock = threading.Lock()

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
        with self._lifecycle_lock:
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
            logger.error("runner loop stopped ({kind})", kind=type(exc).__name__)
            self._last_error = f"runner loop stopped ({type(exc).__name__})"
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

    def _default_policy_resolver(self) -> dict:
        """Resolve the org's effective policy from the configured control plane."""
        return policy_mod.resolve_effective_policy(self.config.hosted_host)

    # ---- safety policy binding ----

    def bind_effective_policy(self) -> tuple[dict, dict]:
        """Resolve the org's effective policy and bind it to this run's config.

        Called immediately before every dispatched run (the sync contract's
        "refresh before a run" rule), so an admin's change to a safety setting
        takes effect on the next dispatch rather than on the next restart.

        Blocking (network + disk); the caller runs it in a worker thread.

        Returns:
            ``(policy, deployment)`` -- the resolved policy (for provenance) and
            the deployment config with the ``safety`` block projected onto it.

        Raises:
            Refusal: When the policy cannot be resolved authoritatively, carries
                an unknown value, or cannot be bound to a deployment config.
                Every path here refuses; none degrades to "run it anyway".
        """
        try:
            policy = self._policy_resolver()
        except Exception as exc:
            # resolve_effective_policy is documented never to raise, so this is
            # a defensive guard: an unexpected failure must still refuse.
            raise Refusal(
                f"effective safety policy could not be resolved ({type(exc).__name__})"
            ) from None
        if not isinstance(policy, dict):
            raise Refusal("effective safety policy was not an object")

        try:
            safety = policy_mod.binding_safety(policy)
        except policy_mod.PolicyEnforcementError as exc:
            raise Refusal(f"safety policy not enforceable: {exc}") from None

        config_path = self.config.data_dir / "deployment.json"
        if not config_path.is_file():
            raise Refusal(
                "runner has no deployment config; the safety policy cannot be "
                "bound to a run"
            )
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            # Never echo the parse error: a deployment config can carry
            # PHI-capable selectors and credentials.
            raise Refusal("runner deployment config could not be read") from None
        if not isinstance(raw, dict):
            raise Refusal("runner deployment config must contain an object")

        try:
            deployment = policy_mod.apply_safety_policy(raw, safety)
        except policy_mod.PolicyEnforcementError as exc:
            raise Refusal(f"safety policy could not be bound: {exc}") from None
        return policy, deployment

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
        session = auth_header(self.config.hosted_host).get("Authorization", "")
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
        async with self._tick_lock:
            return await self._tick_once(client)

    async def _tick_once(self, client: RunnerClient) -> float | None:
        """Run one serialized poll and its complete leased job, if present."""
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
            self._last_error = f"runner transport failed ({type(exc).__name__})"
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
            self._last_error = f"runner transport failed ({type(exc).__name__})"
            logger.warning(
                "job handling hit a network error ({kind})",
                kind=type(exc).__name__,
            )
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
                logger.warning(
                    "uncertain ack for {r} deferred ({kind})",
                    r=run_id,
                    kind=type(exc).__name__,
                )
                continue
            self.journal.record(run_id, "finished", outcome="uncertain", reason=reason)

    # ---- job handling ----

    async def handle_job(self, client: RunnerClient, job: dict) -> None:
        """Validate -> execute -> stream evidence -> ack for one leased job."""
        received_at = datetime.now(timezone.utc)
        async with self._handle_lock:
            await self._handle_job(client, job, received_at=received_at)

    async def _handle_job(
        self, client: RunnerClient, job: dict, *, received_at: datetime
    ) -> None:
        """Handle one job under the process-local single-flight lock."""

        run_id = str(job.get("run_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", run_id):
            logger.warning("dispatch has an invalid run id; refusing")
            return
        try:
            job_id, lease_deadline = _lease_deadline(job, received_at=received_at)
        except Refusal as refusal:
            raw_job_id = str((job.get("lease") or {}).get("job_id") or "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", raw_job_id):
                await client.ack(
                    raw_job_id,
                    "refused",
                    run_id=run_id,
                    reason=str(refusal),
                )
            return

        try:
            existing = self.journal.get(run_id)
        except RunnerJournalError:
            reason = "local run journal is corrupt; outcome requires reconciliation"
            await client.ack(job_id, "uncertain", run_id=run_id, reason=reason)
            return
        if existing and existing.get("phase") in {"starting", "started"}:
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
            # The org's safety policy binds THIS run, resolved fresh and before
            # any GUI action. An unenforceable policy refuses here.
            policy, deployment = await asyncio.to_thread(self.bind_effective_policy)
            if datetime.now(timezone.utc) >= lease_deadline:
                raise Refusal("dispatch lease expired before start")
        except Refusal as refusal:
            reason = str(refusal)
            logger.warning("dispatch {r} refused: {why}", r=run_id, why=reason)
            self.journal.record(run_id, "finished", outcome="refused", reason=reason)
            await client.ack(job_id, "refused", run_id=run_id, reason=reason)
            return

        self.journal.record(
            run_id,
            "starting",
            policy_source=policy.get("source"),
            policy_version=policy.get("policy_version"),
        )
        start_confirmed = await self._evidence(
            client, run_id, authorization_id, seq, "state",
            {"state": "started", "at": datetime.now(timezone.utc).isoformat()},
        )
        if not start_confirmed:
            reason = "run start could not be confirmed; no action was dispatched"
            await client.ack(job_id, "uncertain", run_id=run_id, reason=reason)
            self.journal.record(
                run_id, "finished", outcome="uncertain", reason=reason
            )
            self._set_state("polling")
            return
        self.journal.record(run_id, "started")
        self._set_state("running")
        run_dir = self.config.data_dir / "runner" / "runs" / run_id
        extend_task = asyncio.ensure_future(
            self._extend_loop(client, job_id, lease_deadline=lease_deadline)
        )
        try:
            result = await asyncio.to_thread(
                self._execute,
                bundle_dir,
                run_dir,
                job.get("authorization") or {},
                deployment,
            )
            exec_error: str | None = None
            exec_ok = bool(getattr(result, "ok", False))
        except Exception as exc:
            exec_error = str(exc)
            exec_ok = False
        finally:
            if extend_task.done():
                lease_error = extend_task.result()
            else:
                extend_task.cancel()
                try:
                    await extend_task
                except asyncio.CancelledError:
                    pass
                lease_error = None

        report = FlowBridge.read_report(run_dir)
        halt = FlowBridge.read_halt(run_dir)
        raw_steps = report.get("steps")
        steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
        for index, step in enumerate(steps):
            await self._evidence(
                client, run_id, authorization_id, seq, "step", _step_event(step, index)
            )
        completion_proof_error: str | None = None
        local_halt = halt
        if lease_error:
            status = "uncertain"
        elif halt:
            status = "halted-needs-attention"
            await self._evidence(
                client,
                run_id,
                authorization_id,
                seq,
                "halt",
                _halt_event(
                    halt,
                    run_id=run_id,
                    workflow_id=str(job.get("workflow_id") or ""),
                    step_count=len(steps),
                ),
            )
        elif exec_ok:
            # Exit code zero proves only that the local process returned. It
            # does not prove the governed effect. Keep this legacy lane
            # fail-closed until it consumes Flow's shared qualification-v2
            # verifier and can bind an exact signed VERIFIED result to this
            # run, authorization, policy, identity, effect, and event sequence.
            # The operator sees the run in the local needs-attention mirror;
            # the ack carries the constant reason, never a run-derived value.
            status = "halted-needs-attention"
            completion_proof_error = COMPLETION_PROOF_REQUIRED_REASON
            local_halt = {
                "kind": COMPLETION_PROOF_HALT_KIND,
                "reason": COMPLETION_PROOF_REQUIRED_REASON,
            }
        else:
            status = "failed"
        if status != "uncertain":
            await self._evidence(
                client, run_id, authorization_id, seq, "run_summary",
                _run_summary(job, report, status),
            )
        self._record_local_run(run_id, run_dir, job, local_halt, status)
        self.journal.record(
            run_id, "finished", outcome=status,
            reason=(lease_error or exec_error or completion_proof_error or "")[:200] or None,
        )
        if status == "uncertain":
            ack_reason = lease_error
        elif status == "halted-needs-attention":
            ack_reason = completion_proof_error
        else:
            ack_reason = None
        await client.ack(job_id, status, run_id=run_id, reason=ack_reason)
        self._set_state("polling")

    async def _extend_loop(
        self, client: RunnerClient, job_id: str, *, lease_deadline: datetime
    ) -> str | None:
        """Renew a live lease and return an uncertainty reason if it expires."""

        while True:
            remaining_s = (
                lease_deadline - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining_s <= 0:
                return "lease expired while the run was in progress"
            await asyncio.sleep(min(LEASE_EXTEND_INTERVAL_S, remaining_s))
            if datetime.now(timezone.utc) >= lease_deadline:
                return "lease expired while the run was in progress"
            try:
                await client.extend(job_id)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "lease extend failed ({kind})", kind=type(exc).__name__
                )
                continue
            lease_deadline = datetime.now(timezone.utc) + timedelta(
                seconds=DEFAULT_LEASE_S
            )

    async def _stage_bundle(self, job: dict) -> Path:
        """Locate or download the sealed bundle for a dispatch.

        Order: the runner's own digest-keyed store, then the dispatch's
        short-lived signed URL (downloaded and safely extracted).
        """
        bundle_info = job.get("bundle") or {}
        digest = str(bundle_info.get("content_digest") or "")
        if not _SHA256_RE.fullmatch(digest):
            raise Refusal("dispatch bundle content digest is invalid")
        store_dir = self.config.data_dir / "runner" / "bundles" / digest
        if (store_dir / "manifest.json").is_file():
            return store_dir
        raw_url = bundle_info.get("url")
        if not raw_url:
            raise Refusal(
                f"bundle {_digest_prefix(digest)} not in local store and no staging URL"
            )
        url = _safe_bundle_url(raw_url)
        store_dir.parent.mkdir(parents=True, exist_ok=True)
        archive_fd, archive_name = tempfile.mkstemp(
            dir=str(store_dir.parent), prefix=f".{digest}.", suffix=".zip"
        )
        os.close(archive_fd)
        archive = Path(archive_name)
        staging_dir = Path(
            tempfile.mkdtemp(dir=str(store_dir.parent), prefix=f".{digest}.", suffix=".tmp")
        )
        try:
            total = 0
            async with self._http_factory() as http:
                async with http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with archive.open("wb") as output:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_BUNDLE_ARCHIVE_BYTES:
                                raise Refusal("bundle archive exceeds the runner limit")
                            output.write(chunk)
            safe_extract_zip(archive, staging_dir)
            try:
                staging_dir.replace(store_dir)
            except FileExistsError:
                if not (store_dir / "manifest.json").is_file():
                    raise Refusal("bundle staging destination is inconsistent") from None
            return store_dir
        except Refusal:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise Refusal("bundle staging failed safety validation") from None
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _execute(
        self,
        bundle_dir: Path,
        run_dir: Path,
        authorization: dict,
        deployment: dict,
    ) -> Any:
        """Execute via the existing flow bridge (blocking; runs in a thread).

        Persists the authorization JSON into the run dir (operator audit copy)
        and forwards it to ``openadapt-flow run --authorization-file`` when the
        installed flow CLI supports that flag (a PROPOSED flow follow-up).

        ``deployment`` is the POLICY-BOUND config from
        :meth:`bind_effective_policy`, not the operator's file: it is staged
        privately (0600, removed when the run ends) for the duration of the
        invocation, so Flow can only ever be handed a config the org's safety
        policy has been applied to.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.chmod(0o700)
        except OSError:
            pass
        auth_path = run_dir / "authorization.json"
        fd, temporary = tempfile.mkstemp(
            dir=str(run_dir), prefix=".authorization.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(authorization, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, auth_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        bridge = self.services.flow_bridge
        kwargs: dict[str, Any] = {}
        probe = getattr(bridge, "run_supports_authorization", None)
        if callable(probe) and probe():
            kwargs["authorization_file"] = auth_path
        prepared = PreparedPrivateYaml(
            payload=yaml.safe_dump(deployment, sort_keys=False), redactions=()
        )
        with stage_private_yaml(run_dir, prepared=prepared) as config_path:
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
                        payload: dict) -> bool:
        """Send one evidence event and report whether Cloud confirmed receipt."""
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
            return False
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(
                "evidence POST failed; local execution state is retained ({kind})",
                kind=type(exc).__name__,
            )
            return False
        return True


class _Seq:
    """Monotone per-run sequence counter for evidence events."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n
