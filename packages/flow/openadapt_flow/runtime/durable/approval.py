"""Authenticated approval for a durable RESUME (RFC §5, Tier 3, P0-5).

A durably-paused run halted for a HUMAN. Continuing it is a consequential act --
it re-drives a workflow that already performed writes -- so it must not be
possible for any caller to just call :func:`~.resume.resume` and proceed. Resume
requires an :class:`ApprovalRecord`: WHO approved (identity), WHEN, the chosen
RESOLUTION, and the bundle/version hash the approval was granted against. The CLI
``approve`` command records one; :func:`~.resume.resume` ENFORCES it.

Enforcement is layered (each raises a :class:`ResumeRefused` subclass):

- no approval record, or a record with no approver identity -> ``ApprovalRequired``
- the pause is older than its stale-pause expiry -> ``PauseExpired``
- the approval was granted against a DIFFERENT bundle/version -> ``BundleMismatch``
- the approval predates the pause it claims to resolve -> ``ApprovalRequired``

Import-light (pydantic + datetime): no vision, no backend, no model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ResumeRefused(RuntimeError):
    """Base class for every reason a durable resume is REFUSED (never a silent
    proceed). Callers can catch this to distinguish a refusal from an execution
    failure."""


class ApprovalRequired(ResumeRefused):
    """No valid, authenticated approval accompanies the resume."""


class PauseExpired(ResumeRefused):
    """The pause is older than its stale-pause expiry -- resume is refused (the
    app state a stale checkpoint expects can no longer be trusted)."""


class BundleMismatch(ResumeRefused):
    """The approval / checkpoint was captured against a different bundle version
    than the one being resumed -- the compiled program changed underneath it."""


class StateDiverged(ResumeRefused):
    """The live app is no longer in the checkpoint's expected state (or an
    already-confirmed effect no longer holds) -- resume is refused rather than
    re-drive from a screen the checkpoint was not captured against."""


class ApprovalRecord(BaseModel):
    """An authenticated authorization to RESUME a durably-paused run.

    The auditable artifact P0-5 requires: approver identity, timestamp, the
    chosen resolution, and the bundle/version hash it was granted against.
    Persisted to ``run_dir/approval.json`` (see
    :meth:`~.checkpoint.CheckpointStore.write_approval`).
    """

    schema_version: int = 1
    #: WHO approved (an operator identity -- required; a blank one is rejected).
    approver: str
    #: WHEN it was approved (ISO-8601 UTC).
    approved_at: str = Field(default_factory=_now)
    #: The chosen resolution (one of the pause's ``proposed_options``, or free
    #: text) -- what the operator decided to do.
    resolution: str = ""
    #: The bundle content hash (``program_checkpoint.bundle_version``) the
    #: approval was granted against. Resume refuses if the live bundle differs.
    bundle_version: str = ""
    #: The workflow this approval is for (audit; must match the paused run).
    workflow_name: str = ""
    #: The run directory this approval authorizes (audit).
    run_dir: str = ""


def enforce_resume_authorization(
    pending,
    approval: Optional[ApprovalRecord],
    *,
    bundle_version: str,
    now: Optional[datetime] = None,
) -> ApprovalRecord:
    """Gate a resume on an authenticated, current, matching approval.

    Args:
        pending: The :class:`~.checkpoint.PendingEscalation` being resumed (the
            durable record of WHY the run paused; carries ``created_at`` and the
            stale-pause window ``stale_after_s``).
        approval: The approval record accompanying the resume (from the caller or
            read from ``run_dir/approval.json``). ``None`` => refuse.
        bundle_version: The content hash of the bundle being resumed NOW.
        now: Injectable clock (defaults to UTC now) -- for deterministic tests.

    Returns:
        The validated :class:`ApprovalRecord`.

    Raises:
        ApprovalRequired / PauseExpired / BundleMismatch: on any failed check
        (all :class:`ResumeRefused`).
    """
    now = now or datetime.now(timezone.utc)

    # (1) Stale-pause expiry -- an approval cannot revive a pause whose expected
    # app state can no longer be trusted. Checked FIRST so an expired pause is
    # refused even with an otherwise-valid approval.
    if pause_is_expired(pending, now):
        created_dt = _parse(getattr(pending, "created_at", "")) or now
        raise PauseExpired(
            f"the pause at step '{getattr(pending, 'step_id', '?')}' expired "
            f"({created_dt.isoformat()} + {getattr(pending, 'stale_after_s', 0)}s "
            f"< {now.isoformat()}); re-run rather than resume a stale checkpoint"
        )

    # (2) An authenticated approval record is REQUIRED.
    if approval is None:
        raise ApprovalRequired(
            "resume requires an authenticated approval record (approver / "
            "timestamp / resolution / bundle version); none was supplied or "
            "found at run_dir/approval.json — refusing to resume"
        )
    if not (approval.approver or "").strip():
        raise ApprovalRequired(
            "the approval record carries no approver identity — refusing to "
            "resume an unauthenticated escalation"
        )

    # (3) The approval must be for THIS compiled program (bundle/version hash).
    if approval.bundle_version and approval.bundle_version != bundle_version:
        raise BundleMismatch(
            "the approval was granted against bundle version "
            f"{approval.bundle_version!r} but the bundle being resumed is "
            f"{bundle_version!r} — the program changed; re-approve against the "
            "current bundle"
        )

    # (4) The approval must not PREDATE the pause it claims to resolve.
    approved = _parse(approval.approved_at)
    created = _parse(getattr(pending, "created_at", ""))
    if approved is not None and created is not None and approved < created:
        raise ApprovalRequired(
            "the approval predates the pause it claims to resolve — refusing "
            "to resume on a stale approval"
        )
    return approval


def pause_is_expired(pending, now: datetime) -> bool:
    """True when the pause is older than its stale-pause window.

    ``stale_after_s <= 0`` disables expiry (never stale)."""
    ttl = float(getattr(pending, "stale_after_s", 0) or 0)
    if ttl <= 0:
        return False
    created = _parse(getattr(pending, "created_at", ""))
    if created is None:
        return False
    return (now - created).total_seconds() > ttl
