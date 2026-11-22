"""Which handoff ledgers a run used, so its receipt can carry their lines.

A deferred claim check and ``HandoffLedger.send`` note their ledger here
while the run is going. The receipt hook (``receipt_record.receipt_after_run``)
reads the noted ledgers, plus any listed in ``OPENADAPT_HANDOFF_LEDGERS``
(directories separated by the OS path separator), and attaches the lines of
the handoffs that belong to the run. Process-local: a receipt written by a
later process names its ledgers through the variable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from openadapt_flow.handoffs.ledger import HandoffLedger

ENV = "OPENADAPT_HANDOFF_LEDGERS"
_NOTED: list[Path] = []


def note_ledger(directory: str | Path) -> None:
    path = Path(directory).resolve()
    if path not in _NOTED:
        _NOTED.append(path)


def forget() -> None:
    """Clear the noted ledgers (tests)."""
    _NOTED.clear()


def ledgers_for(env: Optional[Mapping[str, str]] = None) -> list[HandoffLedger]:
    env = os.environ if env is None else env
    paths = list(_NOTED)
    for item in (env.get(ENV) or "").split(os.pathsep):
        if item.strip():
            path = Path(item.strip()).expanduser().resolve()
            if path not in paths:
                paths.append(path)
    return [HandoffLedger(path) for path in paths if path.is_dir()]


__all__ = ["ENV", "forget", "ledgers_for", "note_ledger"]
