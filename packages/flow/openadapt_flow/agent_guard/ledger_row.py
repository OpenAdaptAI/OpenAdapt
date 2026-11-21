"""One accounting row per guarded save, and the private diff behind it.

After every read-back or refusal the commit guard writes, in the run folder:

``guard_ledger_row.json`` (``openadapt.guard-ledger-row/v1``)
    What happened to the save, with no record values: the outcome, the
    names of the fields checked and of those that differ, how long the save
    was held, who approved it and when, and how independent the read-back
    was. An ops collector sums these rows.

``guard_diff.private.json`` (``openadapt.guard-diff/v1``, mode 0600)
    Per field: the value the task intended, the value the record held
    before, the value the read-back found, and whether they differ. Raw
    values, so it stays on this machine. ``openadapt-flow redact-report``
    on the run folder writes ``guard_diff.scrubbed.json`` (:func:`export_scrubbed`):
    the same rows with each value replaced by a salted digest, so two copies
    can still be compared without the values.

``verdict`` is the run's outcome in the ledger's words:

* ``VERIFIED``: an independent read-back shows exactly the intended write.
* ``CAUGHT_ERROR``: the guard stopped or caught a wrong save: a wrong record
  on screen, a second save, a declined approval, or a read-back that shows
  another value or another write (``REFUTED``).
* ``ESCALATED``: the save went through and could not be confirmed
  (``RECONCILIATION_REQUIRED``), or nobody answered its approval before it
  expired; a person checks the record.
* ``FAILED``: the guard could not do its job (the store couldn't be read
  before the save, the approval couldn't be asked, or its answer was
  unusable).

One row per run. A refusal after a read-back (a second save the guard
blocked) keeps the read-back's row, since that is the save that happened;
a read-back after a refusal (blocked, then saved right) replaces it.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.guard import GuardVerdict, _write_json

__all__ = [
    "DIFF_FILENAME",
    "DIFF_SCHEMA",
    "ROW_FILENAME",
    "ROW_SCHEMA",
    "SCRUBBED_FILENAME",
    "export_scrubbed",
    "ledger_verdict",
    "row_after_read_back",
    "write_readback_row",
    "write_refusal_row",
]

ROW_SCHEMA = "openadapt.guard-ledger-row/v1"
DIFF_SCHEMA = "openadapt.guard-diff/v1"
SCRUBBED_SCHEMA = "openadapt.guard-diff-scrubbed/v1"
ROW_FILENAME = "guard_ledger_row.json"
DIFF_FILENAME = "guard_diff.private.json"
SCRUBBED_FILENAME = "guard_diff.scrubbed.json"
ROW_KEYS = (
    "schema",
    "task_id",
    "verdict",
    "fields_checked",
    "fields_differing",
    "hold_ms",
    "approver_kind",
    "approval_opened_at",
    "approval_decided_at",
    "readback_tier",
    "diff_path",
)

#: Refusals where the guard itself could not do its job.
_FAILED_CODES = frozenset(
    {
        "baseline_unreadable",
        "screenshot_required",
        "wrong_state",
        "task_invalid",
        "task_unreadable",
        "readback_provider_failed",
        # The approval itself could not be used: a broken or replayed answer.
        "already_answered",
        "answer_fields_invalid",
        "answer_malformed",
        "answer_unreadable",
        "approver_root_changed",
        "frame_digest_mismatch",
        "frame_unreadable",
        "initiator_changed",
        "request_unknown",
        "store_unsafe",
    }
)
#: Refusals where nobody decided in time: nothing was written, a person acts.
_ESCALATED_CODES = frozenset({"request_expired"})


def ledger_verdict(
    verdict: Optional[GuardVerdict],
    *,
    differing: bool = False,
    refusal: Optional[str] = None,
) -> str:
    """The ledger outcome for a read-back verdict or a refusal code."""

    if refusal is not None:
        if refusal in _ESCALATED_CODES:
            return "ESCALATED"
        if refusal in _FAILED_CODES or refusal.startswith(("approval_", "policy_")):
            return "FAILED"
        return "CAUGHT_ERROR"
    if differing or verdict is GuardVerdict.REFUTED:
        return "CAUGHT_ERROR"
    if verdict is GuardVerdict.VERIFIED:
        return "VERIFIED"
    return "ESCALATED"


def _approval_times(guard: Any) -> tuple[Optional[str], Optional[str]]:
    request = getattr(guard, "_request", None) if guard is not None else None
    if request is None:
        return None, None
    decided = None
    try:
        from openadapt_flow.approval_mode.item_approval import ItemApprovalStore

        answer = ItemApprovalStore(guard.dir, key_dir=guard.owner.key_dir).read_answer(
            request.request_id
        )
        decided = answer.answered_at if answer is not None else None
    except Exception:  # noqa: BLE001 - an unreadable answer: time unknown
        decided = None
    return request.created_at, decided


def _approver_kind(guard: Any) -> str:
    approver = getattr(guard, "_approver", None) if guard is not None else None
    if approver:
        return str(approver.get("kind") or "person")
    return "none"


def _row(
    *,
    task_id: str,
    verdict: str,
    checked: list[str],
    differing: list[str],
    hold_ms: Optional[float],
    guard: Any,
    tier: Optional[str],
    diff_path: Optional[str],
) -> dict[str, Any]:
    opened, decided = _approval_times(guard)
    row = {
        "schema": ROW_SCHEMA,
        "task_id": task_id,
        "verdict": verdict,
        "fields_checked": sorted(checked),
        "fields_differing": sorted(differing),
        "hold_ms": hold_ms,
        "approver_kind": _approver_kind(guard),
        "approval_opened_at": opened,
        "approval_decided_at": decided,
        "readback_tier": tier,
        "diff_path": diff_path,
    }
    return row


def write_readback_row(
    out_dir: Path,
    *,
    task: Any,
    guard: Any,
    saved: Mapping[str, Optional[str]],
    differing: list[str],
    verdict: GuardVerdict,
    tier: Optional[str],
    hold_ms: Optional[float],
) -> dict[str, Any]:
    """Write the diff (0600) and the row after a read-back. Returns the row."""

    before: dict[str, Optional[str]] = {}
    card = getattr(guard, "approval_card", None) if guard is not None else None
    for item in getattr(card, "fields", None) or []:
        before[item.label] = item.before
    rows = []
    for name in sorted(task.intended_fields):
        found = saved.get(name)
        rows.append(
            {
                "field": name,
                "intended": str(task.intended_fields[name]),
                "before": before.get(name),
                "saved": found,
                "status": (
                    "not_read"
                    if found is None
                    else ("differs" if name in differing else "same")
                ),
            }
        )
    out = Path(out_dir)
    _write_json(
        out / DIFF_FILENAME,
        {
            "schema": DIFF_SCHEMA,
            "task_id": task.task_id,
            "guard_id": getattr(guard, "guard_id", None),
            "readback_tier": tier,
            "fields": rows,
        },
    )
    row = _row(
        task_id=task.task_id,
        verdict=ledger_verdict(verdict, differing=bool(differing)),
        checked=[r["field"] for r in rows if r["status"] != "not_read"],
        differing=differing,
        hold_ms=hold_ms,
        guard=guard,
        tier=tier,
        diff_path=DIFF_FILENAME,
    )
    _write_json(out / ROW_FILENAME, row)
    return row


def row_after_read_back(
    out_dir: Optional[Path],
    *,
    task: Any,
    guard: Any,
    read: Any,
    verdict: GuardVerdict,
    hold_ms: Optional[float],
    receipt: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """The commit guard's one call after a read-back. ``read`` is the
    provider's :class:`~openadapt_flow.agent_guard.readback.ReadbackResult`,
    or None (observe-only: ``receipt`` is the verify-run document)."""

    if out_dir is None:
        return None
    from openadapt_flow.agent_guard.readback import differing_fields

    if read is not None:
        saved, tier = dict(read.fields), read.tier
    else:
        saved = {name: None for name in task.intended_fields}
        tier = ((receipt or {}).get("receipt") or {}).get("verification_tier")
    try:
        return write_readback_row(
            out_dir,
            task=task,
            guard=guard,
            saved=saved,
            differing=differing_fields(task, saved),
            verdict=verdict,
            tier=tier,
            hold_ms=hold_ms,
        )
    except OSError:
        # The accounting row is lost, never the guard's verdict or notice.
        return None


def write_refusal_row(
    out_dir: Path,
    *,
    task: Any,
    guard: Any,
    code: str,
    hold_ms: Optional[float],
) -> Optional[dict[str, Any]]:
    """Write the row for a save the guard refused. Nothing was written.

    A row a read-back already wrote in this run is kept (returns None).
    """

    if _has_read_back(Path(out_dir) / ROW_FILENAME):
        return None
    row = _row(
        task_id=task.task_id,
        verdict=ledger_verdict(None, refusal=code),
        checked=[],
        differing=[],
        hold_ms=hold_ms,
        guard=guard,
        tier=None,
        diff_path=None,
    )
    try:
        _write_json(Path(out_dir) / ROW_FILENAME, row)
    except OSError:
        return None
    return row


def _has_read_back(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(row, dict) and row.get("diff_path") is not None


def export_scrubbed(run_dir: Path) -> Optional[Path]:
    """Write ``guard_diff.scrubbed.json`` next to the private diff, or None.

    Each value becomes a digest salted once per export (the salt is not
    kept), so equal values still look equal within the copy and nothing can
    be looked up. Field names and statuses stay. The copy then goes through
    the report redaction (:func:`openadapt_flow.report_redaction.redact_report`)
    so a credential-like name is scrubbed as in a report.
    """

    from openadapt_flow.report_redaction import load_policy, redact_report

    run = Path(run_dir)
    source = run / DIFF_FILENAME
    if source.is_symlink() or not source.is_file():
        return None
    try:
        diff = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(diff, dict) or diff.get("schema") != DIFF_SCHEMA:
        return None
    salt = secrets.token_bytes(16)

    def digest(value: Any) -> Optional[str]:
        if value is None:
            return None
        mac = hashlib.sha256(salt + str(value).encode("utf-8")).hexdigest()[:16]
        return "salted:" + mac

    rows = [
        {
            "field": str(item.get("field")),
            "status": str(item.get("status")),
            **{k: digest(item.get(k)) for k in ("intended", "before", "saved")},
        }
        for item in diff.get("fields") or []
        if isinstance(item, dict)
    ]
    copy = {
        "schema": SCRUBBED_SCHEMA,
        "task_id": diff.get("task_id"),
        "readback_tier": diff.get("readback_tier"),
        "fields": rows,
    }
    redacted, _found = redact_report(copy, load_policy(), by="redact-report")
    out = run / SCRUBBED_FILENAME
    tmp = out.with_name(out.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(redacted, fh, indent=2, sort_keys=True)
    os.replace(tmp, out)
    return out
