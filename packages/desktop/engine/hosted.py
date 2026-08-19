"""hosted.py -- the cloud-lane egress verbs: ``push`` and ``report_break``.

``push`` delegates to Flow's sanitized-derivative upload contract. It never
constructs or uploads an archive from raw Desktop data. ``report_break`` also
delegates to Flow. Flow validates ``report.json`` and sends only its
closed-schema, PHI-minimal summary.

Credentials come exclusively from :mod:`engine.auth` (``auth_header()``); this
module never implements login. :func:`push` delegates to the pinned Flow
runtime and fails closed when that command is unavailable.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from engine.auth.store import (
    DEFAULT_HOST,
    INGEST_TOKEN_ENV,
    active_credential,
    canonical_host_origin,
    token_for_host,
)
from engine.flow_bridge import FlowBridge
from engine.qualification_lifecycle import parse_flow_push

_MAX_FLOW_ERROR_CHARS = 500


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
    if src_dir.is_symlink() or not src_dir.is_dir():
        raise ValueError("Archive source must be a real directory, not a symlink.")
    members = sorted(src_dir.rglob("*"))
    symlink = next((path for path in members if path.is_symlink()), None)
    if symlink is not None:
        raise ValueError(f"Archive source contains a symlink: {symlink.relative_to(src_dir)}")
    temporary = dest is None
    if dest is None:
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix=f"{src_dir.name}_")
        # Close the handle mkstemp opened before touching the path -- on Windows
        # an open fd holds a lock, so unlink/reopen raises WinError 32.
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        dest = Path(tmp)
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in members:
                if path.is_symlink():
                    raise ValueError(
                        f"Archive source contains a symlink: {path.relative_to(src_dir)}"
                    )
                if path.is_file():
                    zf.write(path, path.relative_to(src_dir))
    except Exception:
        if temporary:
            dest.unlink(missing_ok=True)
        raise
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
    backend: Any = None,
    prefer_flow: bool = True,
    db: Any = None,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """Push through Flow's approved sanitized-derivative contract.

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
        backend: Deprecated direct backend injection. Supplying it fails closed.
        prefer_flow: Deprecated bypass. Setting it false fails closed.
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

    if backend is not None or not prefer_flow:
        return {
            "success": False,
            "workflow_id": None,
            "dashboard_url": None,
            "error": (
                "Direct Desktop ingest is disabled. Use the pinned Flow push command so "
                "only an approved, exact-hash sanitized derivative can leave the machine."
            ),
        }
    try:
        result_dict = _push_via_flow(path, kind=kind, name=name, host=host, token=token)
    except Exception as exc:
        # A launch or transport failure must never select a raw upload
        # fallback. The exception can occur after Flow dispatched a request,
        # so Desktop must not claim that no bytes crossed the boundary.
        logger.warning(
            "Flow push did not return a confirmed outcome ({kind})",
            kind=type(exc).__name__,
        )
        return {
            "success": False,
            "delivery_uncertain": True,
            "workflow_id": None,
            "artifact_ingest_id": None,
            "next_action": "reconcile",
            "error_code": "delivery_uncertain",
            "dashboard_url": None,
            "error": (
                "Flow did not return a confirmed upload outcome. Do not retry blindly; "
                "reconcile the exact artifact in Cloud first."
            ),
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
            logger.warning(
                "Could not persist workflow_id to bundle {bid}: {e}", bid=bundle_id, e=exc
            )
    return result_dict


def _push_via_flow(
    path: Path, *, kind: str, name: str | None, host: str, token: str | None = None
) -> dict[str, Any]:
    """Delegate to Flow and preserve upload versus local-review outcomes."""
    logger.info("Delegating push to openadapt-flow")
    resolved_token = _token_for_host(host, explicit=token)
    env = {INGEST_TOKEN_ENV: resolved_token} if resolved_token else None
    result = FlowBridge().push(
        path,
        kind=kind,
        name=name,
        host=host,
        token=None,
        env_overrides=env,
        json_output=True,
    )
    parsed = parse_flow_push(
        result.stdout or "",
        result.stderr or "",
        ok=result.ok,
        expected_host=host,
    )
    success = bool(parsed.get("accepted_for_ingest"))
    return {
        **parsed,
        "success": success,
    }


def _token_for_host(host: str, *, explicit: str | None = None) -> str:
    """Resolve a Desktop credential without sending it to another origin."""

    return token_for_host(host, explicit=explicit)


def _bounded_flow_error(message: str, *, secret: str = "") -> str:
    """Return one bounded CLI diagnostic without reflecting a bearer token."""

    detail = (message or "").strip()
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
    return detail[:_MAX_FLOW_ERROR_CHARS]


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
    """Delegate break reporting to Flow's closed-schema egress boundary."""
    halt = FlowBridge.read_halt(run_dir)
    if halt is None:
        return {"ok": False, "error": "No halt found in report.json.", "run_id": None}
    if not workflow_id:
        return {"ok": False, "error": "A hosted workflow id is required.", "run_id": None}

    resolved_token = _token_for_host(host, explicit=token)
    if not resolved_token:
        return {"ok": False, "error": "Not logged in (no ingest token).", "run_id": None}

    if org_id is None:
        cred = active_credential()
        if cred and canonical_host_origin(
            str(cred.get("host", ""))
        ) == canonical_host_origin(host):
            org_id = cred.get("org_id")
    try:
        result = FlowBridge().report_break(
            Path(run_dir),
            workflow_id=workflow_id,
            host=host,
            deployment_kind=deployment_kind,
            org_id=org_id,
            timeout=timeout,
            env_overrides={INGEST_TOKEN_ENV: resolved_token},
        )
    except Exception as exc:
        logger.warning(
            "Flow report-break did not return a confirmed outcome ({kind})",
            kind=type(exc).__name__,
        )
        return {
            "ok": False,
            "delivery_uncertain": True,
            "error": (
                "Flow did not return a confirmed report outcome. Reconcile the run in "
                "Cloud before another report attempt."
            ),
            "run_id": None,
        }
    stdout = result.stdout or ""
    if not result.ok:
        return {
            "ok": False,
            "delivery_uncertain": True,
            "error": _bounded_flow_error(result.stderr or stdout, secret=resolved_token)
            or "Flow report-break failed.",
            "run_id": None,
        }
    if stdout.startswith("Break kept LOCAL-ONLY:"):
        if not allow_local_fallback:
            raise PhiBoundaryError(
                "Break report was kept local by Flow's PHI boundary; use local teach."
            )
        return {
            "ok": False,
            "local_teach": True,
            "error": stdout.partition(":")[2].strip(),
            "run_id": None,
        }
    if stdout.startswith("Nothing emitted:"):
        return {
            "ok": False,
            "error": stdout.partition(":")[2].strip() or "Flow emitted no break summary.",
            "run_id": None,
        }
    match = re.search(
        r"Break reported \(run_id=([^,]+), halt_id=([^,]+), status=([^\)]+)\)\.",
        stdout,
    )
    if match is None:
        return {
            "ok": False,
            "error": "Flow reported success without a verified break identity.",
            "run_id": None,
        }
    teach_match = re.search(r"^Teach:\s+(\S+)\s*$", stdout, re.MULTILINE)
    logger.info("Reported break: run {run_id}", run_id=match.group(1))
    return {
        "ok": True,
        "run_id": match.group(1),
        "halt_id": match.group(2),
        "status": match.group(3),
        "teach_url": teach_match.group(1) if teach_match else None,
        "error": "",
    }
