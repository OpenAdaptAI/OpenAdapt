"""Carry a save answer to the durable pause a run left when it stopped waiting.

A run that gets no answer in time stops before the save and leaves a durable
``item_approval`` pause. An answer that arrives later, from the command line,
an agent or the phone, has to reach that pause: an approval authorizes exactly
one resume of it, and a decline ends it. Every answering route calls
:func:`carry_answer_to_durable_pause` right after it writes the answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def carry_answer_to_durable_pause(
    run_dir: Path,
    request: Any,
    answer: Any,
    *,
    approver: Optional[str] = None,
) -> bool:
    """Apply ``answer`` to the run's durable pause, if it left one for ``request``.

    Returns True when a ``resume`` must follow (an approval was committed).
    Returns False when there is no matching pause (the run is still waiting, or
    never paused durably), when the answer is a decline (the pause is ended),
    or when the pause refuses the approval.
    """

    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable.approval import (
        ResumeRefused,
        issue_resume_approval,
    )
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

    try:
        store = CheckpointStore(run_dir, key=crypto.resolve_key(None))
        pending = store.read_pending()
        manifest = store.read_manifest()
    except Exception:  # noqa: BLE001 - no durable pause to carry the answer to
        return False
    if (
        pending is None
        or manifest is None
        or pending.category != "item_approval"
        or pending.status != "pending"
        or pending.step_id != request.step_id
        or pending.step_index != request.step_index
        or manifest.run_id != request.run_id
    ):
        return False
    if answer.decision == "decline":
        store.cas_pending(
            store.model_digest(pending),
            pending.model_copy(update={"status": "rejected"}),
        )
        return False
    if approver is None:
        approver = (
            f"desktop:{answer.approver_id}"
            if answer.approver_id is not None
            else "desktop:unauthenticated"
        )
    try:
        approval = issue_resume_approval(
            pending,
            approver=approver,
            resolution="approve_item",
            bundle_version=bundle_version(manifest.bundle_dir),
            workflow_name=pending.workflow_name,
            run_id=manifest.run_id,
            run_dir=run_dir,
        )
        store.commit_approval_transition(
            expected_pending=pending,
            approval=approval,
            target_status="approved",
        )
    except (ResumeRefused, OSError):
        return False
    return True


__all__ = ["carry_answer_to_durable_pause"]
