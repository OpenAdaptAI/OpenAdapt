"""hosted.py -- the cloud-lane egress verbs: ``push`` and ``report_break``.

``push`` zips a flow recording (or compiled bundle) directory and uploads it to
``POST /api/ingest`` (spec section 3b). ``report_break`` reads a local run's
``report.json`` and posts a PHI-free break descriptor to
``POST /api/runs/ingest-report`` (spec section 3c) so a BYOC halt is triageable
centrally without any PHI leaving the machine.

Credentials come exclusively from :mod:`engine.auth` (``auth_header()``); this
module never implements login. If the ``openadapt-flow`` CLI grows ``push``
(workstream W4), :func:`push` prefers delegating to it; otherwise it runs
in-tree against the identical contract.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from engine.auth.store import DEFAULT_HOST, active_credential, auth_header
from engine.backends.hosted_ingest import HostedIngestBackend
from engine.flow_bridge import FlowBridge

INGEST_REPORT_PATH = "/api/runs/ingest-report"

# Keys that MUST NOT appear in a break descriptor on any lane (fail-closed;
# server returns 422 if they leak). We strip them client-side too.
_PHI_FORBIDDEN_KEYS = frozenset({"field_values", "report_body", "dom"})


class PhiBoundaryError(RuntimeError):
    """Raised when the server rejects a break report as a PHI boundary violation."""


def zip_dir(src_dir: Path, dest: Path | None = None) -> Path:
    """Zip a directory (recording or bundle) into a ``.zip`` file.

    Args:
        src_dir: The directory to zip.
        dest: Optional output path; a temp file is used when omitted.

    Returns:
        Path to the created ``.zip``.
    """
    src_dir = Path(src_dir)
    if dest is None:
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix=f"{src_dir.name}_")
        # Close the handle mkstemp opened before touching the path -- on Windows
        # an open fd holds a lock, so unlink/reopen raises WinError 32.
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        dest = Path(tmp)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))
    return dest


def _latest_recording(recordings_dir: Path) -> Path | None:
    """Return the most-recently-modified recording subdirectory, or None."""
    if not recordings_dir.exists():
        return None
    dirs = [p for p in recordings_dir.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def push(
    path: Path | None = None,
    *,
    kind: str = "recording",
    name: str | None = None,
    host: str = DEFAULT_HOST,
    token: str | None = None,
    recordings_dir: Path | None = None,
    backend: HostedIngestBackend | None = None,
    prefer_flow: bool = True,
    db: Any = None,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """Zip a recording/bundle directory and push it to ``/api/ingest``.

    Signature mirrors ``openadapt_flow.hosted.push(path, kind, name, host, token)``
    (flow PR #119) so the two are swappable. On success the returned hosted
    ``workflow_id`` is persisted into ``bundles`` (when ``db`` + ``bundle_id`` are
    given) so a later halt can be reported against the correct hosted id -- a
    run's ``report.json`` only carries ``workflow_name``, never the hosted id.

    Args:
        path: Directory to push. Defaults to the most-recent recording under
            ``recordings_dir``.
        kind: ``"recording"`` (default) or ``"bundle"``.
        name: Optional workflow name.
        host: Hosted base URL.
        token: Explicit ingest token (else resolved from the auth store/env).
        recordings_dir: Where to look for the default recording.
        backend: Injected backend (tests); defaults to a real HostedIngestBackend.
        prefer_flow: Delegate to ``openadapt-flow push`` when that CLI supports it.
        db: Optional :class:`~engine.db.IndexDB` to persist the workflow_id into.
        bundle_id: Local bundle id to map to the returned hosted workflow_id.

    Returns:
        A result dict: ``{"success", "workflow_id", "dashboard_url", "error"}``.

    Raises:
        FileNotFoundError: If no directory can be resolved to push.
    """
    if path is None:
        if recordings_dir is None:
            raise FileNotFoundError("No path given and no recordings_dir to search.")
        path = _latest_recording(Path(recordings_dir))
        if path is None:
            raise FileNotFoundError(f"No recordings found under {recordings_dir}.")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Nothing to push at {path}.")

    if prefer_flow and _flow_supports_push():
        result_dict = _push_via_flow(path, kind=kind, name=name, host=host, token=token)
    else:
        backend = backend or HostedIngestBackend(host=host)
        zip_path = zip_dir(path)
        try:
            metadata: dict[str, Any] = {"kind": kind, "capture_id": path.name}
            if name:
                metadata["name"] = name
            result = backend.upload(zip_path, metadata)
        finally:
            zip_path.unlink(missing_ok=True)
        result_dict = {
            "success": result.success,
            "workflow_id": result.metadata.get("workflow_id", "") if result.success else "",
            "dashboard_url": result.remote_url,
            "error": result.error,
        }

    # Persist the hosted workflow_id so report_break can reference it later.
    should_persist = (
        result_dict.get("success")
        and result_dict.get("workflow_id")
        and db is not None
        and bundle_id
    )
    if should_persist:
        try:
            db.update_bundle(bundle_id, workflow_id=result_dict["workflow_id"])
        except Exception as exc:  # non-fatal -- push already succeeded
            logger.warning("Could not persist workflow_id to bundle {bid}: {e}",
                           bid=bundle_id, e=exc)
    return result_dict


def _flow_supports_push(flow_bin: str = "openadapt-flow") -> bool:
    """Best-effort check whether the flow CLI exposes a ``push`` subcommand."""
    import shutil
    import subprocess

    if shutil.which(flow_bin) is None:
        return False
    try:
        proc = subprocess.run(
            [flow_bin, "push", "--help"], capture_output=True, text=True, timeout=15
        )
    except Exception:
        return False
    return proc.returncode == 0


def _push_via_flow(
    path: Path, *, kind: str, name: str | None, host: str, token: str | None = None
) -> dict[str, Any]:
    """Delegate to ``openadapt-flow push`` (flow PR #119); parse its workflow id."""
    import subprocess

    args = ["push", str(path), "--kind", kind, "--host", host]
    if name:
        args += ["--name", name]
    if token:
        args += ["--token", token]
    logger.info("Delegating push to openadapt-flow")
    proc = subprocess.run(["openadapt-flow", *args], capture_output=True, text=True)
    workflow_id = ""
    for token in (proc.stdout or "").split():
        if token.startswith("wf_") or token.startswith("workflow_"):
            workflow_id = token
            break
    return {
        "success": proc.returncode == 0,
        "workflow_id": workflow_id,
        "dashboard_url": f"{host.rstrip('/')}/dashboard/workflows/{workflow_id}"
        if workflow_id
        else "",
        "error": proc.stderr if proc.returncode != 0 else "",
    }


def build_break_descriptor(
    report: dict,
    *,
    workflow_id: str | None = None,
    deployment_kind: str = "cloud",
    org_id: str | None = None,
    report_path: str | None = None,
) -> dict[str, Any]:
    """Build a PHI-free break descriptor from a run's ``report.json``.

    Only whitelisted, PHI-free fields are included. Screenshots, field values,
    DOM, and report bodies are never sent from here (spec section 3c). ``report``
    is expected to be the ``halt`` block or a halt-shaped report.

    Args:
        report: The halt/report dict (from :meth:`FlowBridge.read_halt`).
        workflow_id: The HOSTED workflow id (persisted at push time). A run's
            ``report.json`` only carries ``workflow_name``, so this must be
            supplied by the caller; it falls back to any id embedded in the report.
        deployment_kind: ``"cloud"`` or ``"byoc"``.
        org_id: The org the token resolves to (from the active credential).
        report_path: A pointer to the local report (path string only, no body).

    Returns:
        The JSON-serializable descriptor.
    """
    metrics = report.get("metrics", {}) or {}
    descriptor: dict[str, Any] = {
        "org_id": org_id,
        "workflow_id": workflow_id or report.get("workflow_id"),
        "deployment_kind": "byoc" if deployment_kind == "byoc" else "cloud",
        "status": report.get("status", "halt"),
        "step_intent": report.get("step_intent", ""),
        "reason": report.get("reason", ""),
        "resolver_rung": report.get("resolver_rung"),
        "drift_signature": report.get("drift_signature"),
        "metrics": {
            "steps": metrics.get("steps", report.get("steps", 0)),
            "duration_s": metrics.get("duration_s", report.get("duration_s", 0)),
        },
    }
    if report.get("error"):
        descriptor["error"] = report["error"]
    if report_path:
        descriptor["report_path"] = report_path
    # Defensive: never forward forbidden keys even if a report carries them.
    for key in _PHI_FORBIDDEN_KEYS:
        descriptor.pop(key, None)
    return descriptor


def report_break(
    run_dir: Path,
    *,
    workflow_id: str | None = None,
    host: str = DEFAULT_HOST,
    token: str | None = None,
    deployment_kind: str = "cloud",
    org_id: str | None = None,
    allow_local_fallback: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Post a PHI-free break descriptor for a halted run to ``/api/runs/ingest-report``.

    Signature mirrors ``openadapt_flow.hosted.report_break(run_dir, workflow_id,
    host, token, deployment_kind, org_id, allow_local_fallback)`` (flow PR #119)
    so the two are swappable.

    Args:
        run_dir: The local run directory containing ``report.json``.
        workflow_id: The HOSTED workflow id (persisted at push time). Required to
            attribute the halt to the right hosted workflow -- ``report.json``
            only has ``workflow_name``.
        host: Hosted base URL.
        token: Explicit ingest token (else resolved from the auth store/env).
        deployment_kind: ``"cloud"`` or ``"byoc"``.
        org_id: Org override (else read from the active credential).
        allow_local_fallback: On a 422 PHI-boundary rejection, return a
            ``local_teach`` result instead of raising.
        timeout: HTTP timeout in seconds.

    Returns:
        Result dict with ``{"ok", "run_id", "halt_id", "status", "teach_url", "error"}``.
        On a 422 with ``allow_local_fallback`` set, ``{"ok": False, "local_teach": True}``.

    Raises:
        PhiBoundaryError: On a 422 fail-closed response when ``allow_local_fallback``
            is False -- the caller must fall back to LOCAL teach.
    """
    halt = FlowBridge.read_halt(run_dir)
    if halt is None:
        return {"ok": False, "error": "No halt found in report.json.", "run_id": None}

    headers = {"Authorization": f"Bearer {token}"} if token else auth_header()
    if "Authorization" not in headers:
        return {"ok": False, "error": "Not logged in (no ingest token).", "run_id": None}

    if org_id is None:
        cred = active_credential()
        org_id = cred.get("org_id") if cred else None
    report_path = str(Path(run_dir) / "report.json")
    descriptor = build_break_descriptor(
        halt, workflow_id=workflow_id, deployment_kind=deployment_kind,
        org_id=org_id, report_path=report_path,
    )

    url = f"{host.rstrip('/')}{INGEST_REPORT_PATH}"
    try:
        resp = httpx.post(url, headers=headers, json=descriptor, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"ingest-report request failed: {exc}", "run_id": None}

    if resp.status_code == 422:
        if allow_local_fallback:
            logger.warning("Break report rejected (422); falling back to local teach")
            return {
                "ok": False,
                "local_teach": True,
                "error": "PHI boundary violation (422); use local teach.",
                "run_id": None,
            }
        raise PhiBoundaryError(
            "Break report rejected as a PHI boundary violation (422); "
            "fall back to local teach."
        )
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"ingest-report failed ({resp.status_code}): {resp.text[:200]}",
            "run_id": None,
        }

    try:
        body = resp.json()
    except ValueError:
        body = {}
    logger.info("Reported break: run {run_id}", run_id=body.get("run_id"))
    return {
        "ok": body.get("ok", True),
        "run_id": body.get("run_id"),
        "halt_id": body.get("halt_id"),
        "status": body.get("status"),
        "teach_url": body.get("teach_url"),
        "error": "",
    }
