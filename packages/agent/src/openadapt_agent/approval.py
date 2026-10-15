"""Flow approval modes as this server sees them: read-only, PHI-free.

openadapt-flow can stop before each consequential save until a person
approves it (``each_item``, Flow's default for the Standard and Regulated
profiles). The run writes ``item-approval/request-<id>.json`` in its run
directory and waits; a person answers with ``openadapt-flow item-approval
approve|decline``, which writes ``answer-<id>.json``; the run marks a used
approval with ``consumed-<id>``. This module only reads those file names and
a request's expiry, never its contents otherwise, and never answers one: a
person approves, not the calling agent.

The operator, not the calling agent, picks the mode when the server starts
(``serve --approval-mode``). Without that flag this server passes no mode,
so Flow's own default applies and a save waits for a person.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_agent.contract import APPROVAL_MODES

__all__ = [
    "APPROVAL_MODES",
    "MAX_PERSON_WAIT_S",
    "flow_supports_approval_modes",
    "report_approval",
    "waiting_for_person",
]

_LOG = logging.getLogger(__name__)

#: Item decisions Flow records in a report's ``approval`` block.
_DECISIONS = frozenset(
    {
        "no_person",
        "approval_required",
        "stop_before_saving",
        "problem",
        "not_gated",
        "approved",
        "declined",
    }
)
#: Flow waits at most 4 hours for one answer. Time a person spends answering
#: doesn't count toward the server's own run timeout, up to this much in all.
MAX_PERSON_WAIT_S = 4 * 3600.0 + 60.0
_APPROVAL_DIR = "item-approval"
_REQUEST_NAME = re.compile(r"^request-([0-9a-f]{32})\.json$")
_MAX_REQUEST_BYTES = 64 * 1024


def flow_supports_approval_modes() -> bool:
    """Whether the openadapt-flow in this interpreter has approval modes."""
    try:
        return importlib.util.find_spec("openadapt_flow.approval_mode") is not None
    except (ImportError, ValueError):
        return False


def _expired(raw: bytes) -> bool:
    """A request whose ``expires_at`` passed, or can't be read, isn't waiting."""
    try:
        expires = datetime.fromisoformat(json.loads(raw)["expires_at"])
    except (ValueError, KeyError, TypeError):
        return True
    if expires.tzinfo is None:
        return True
    return datetime.now(timezone.utc) > expires


def waiting_for_person(run_dir: Any) -> bool:
    """Whether a save in this run is waiting for a person's answer now.

    True only for a request that has no answer, wasn't used, and hasn't
    expired. Anything unreadable reads as not waiting, so the caller falls
    back to its ordinary handling.
    """
    if not run_dir:
        return False
    try:
        folder = Path(run_dir) / _APPROVAL_DIR
        if Path(run_dir).is_symlink() or folder.is_symlink() or not folder.is_dir():
            return False
        for entry in folder.iterdir():
            match = _REQUEST_NAME.match(entry.name)
            if match is None or entry.is_symlink() or not entry.is_file():
                continue
            request_id = match.group(1)
            if (folder / f"answer-{request_id}.json").exists():
                continue
            if (folder / f"consumed-{request_id}").exists():
                continue
            if entry.stat().st_size > _MAX_REQUEST_BYTES:
                continue
            if not _expired(entry.read_bytes()):
                return True
    except OSError:
        _LOG.debug("could not read the approval requests for a run", exc_info=True)
    return False


def report_approval(report: object) -> tuple[Optional[str], Optional[str]]:
    """``(mode, decision)`` from a Flow report's ``approval`` block, if any.

    Closed values only; anything else reads as absent.
    """
    if not isinstance(report, dict):
        return None, None
    block = report.get("approval")
    if not isinstance(block, dict):
        return None, None
    mode = block.get("mode")
    decision = block.get("decision")
    return (
        mode if mode in APPROVAL_MODES else None,
        decision if decision in _DECISIONS else None,
    )
