"""Shadow mode by default: a new contract observes before it may block.

A contract is new to an app until it has been seen to judge real writes
correctly. Until then its verdicts are computed and recorded in full, but a
caller that stops on a verdict (``WriteResult.blocks``) doesn't stop: the
contract is in ``shadow``. Each verdict is appended to the contract's gate
log. After ``N`` checks in a row with no stop of a correct write, gating is
*offered*; a person turns it on (``openadapt-flow gate enable``) and from
then on a verdict that isn't CONFIRMED blocks (``gating``).

What counts as clean: a CONFIRMED check, or a stop a person labelled a true
catch (``openadapt-flow gate label <write_id> caught``). Any other stop
breaks the run, since until someone looks it may be a false stop. A stop a
person labels ``false_stop`` breaks it too, and says so in ``status``.

``N`` is ``OPENADAPT_GATE_CLEAN_RUNS`` (20 to 50, default 30). Setting
``OPENADAPT_ENFORCEMENT=gating`` gates every contract (the behaviour before
shadow mode); ``shadow`` keeps every contract in shadow. The log lives under
``OPENADAPT_GATE_DIR`` (default ``<OPENADAPT_LEDGER_DIR>/gates``, else
``~/.openadapt/gates``), one JSON line per
check, names and outcomes only, never values. A log that can't be written
never changes a verdict; it only leaves the contract in shadow.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCHEMA = "openadapt.gate/v1"
MIN_CLEAN_RUNS = 20
MAX_CLEAN_RUNS = 50
DEFAULT_CLEAN_RUNS = 30
MODES = ("shadow", "gating")


def clean_runs_needed() -> int:
    """``N``: clean checks in a row before gating is offered (20 to 50)."""

    raw = os.environ.get("OPENADAPT_GATE_CLEAN_RUNS", "")
    try:
        n = int(raw) if raw.strip() else DEFAULT_CLEAN_RUNS
    except ValueError:
        n = DEFAULT_CLEAN_RUNS
    return min(MAX_CLEAN_RUNS, max(MIN_CLEAN_RUNS, n))


def gate_dir() -> Path:
    raw = os.environ.get("OPENADAPT_GATE_DIR")
    if raw:
        return Path(raw)
    ledger = os.environ.get("OPENADAPT_LEDGER_DIR")
    if ledger:
        return Path(ledger) / "gates"
    return Path.home() / ".openadapt" / "gates"


def _file(digest: str) -> Path:
    name = hashlib.sha256(str(digest).encode("utf-8")).hexdigest()[:32]
    return gate_dir() / f"{name}.jsonl"


def _append(digest: str, entry: dict[str, Any]) -> None:
    path = _file(digest)
    # Private, like the ledger it may sit in: names and outcomes only, but
    # still nobody else's to read.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    line = json.dumps({"schema": SCHEMA, "contract": digest, **entry}, sort_keys=True)
    # One short line per append: O_APPEND keeps concurrent writers whole.
    from openadapt_flow.private_file import open_binary

    fd = open_binary(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _entries(digest: str) -> list[dict[str, Any]]:
    path = _file(digest)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("contract") == digest:
            out.append(item)
    return out


@dataclass
class GateStatus:
    contract: str
    mode: str
    clean_in_a_row: int
    needed: int
    checks: int
    false_stops: int
    offered: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "mode": self.mode,
            "clean_in_a_row": self.clean_in_a_row,
            "needed": self.needed,
            "checks": self.checks,
            "false_stops_labelled": self.false_stops,
            "gating_offered": self.offered,
        }


def status(digest: str) -> GateStatus:
    """The contract's mode and its run of clean checks, from its log."""

    entries = _entries(digest)
    labels = {
        str(e.get("write_id")): str(e.get("label"))
        for e in entries
        if e.get("event") == "label"
    }
    checks = [e for e in entries if e.get("event") == "check"]
    run = 0
    from openadapt_flow.verifier import Outcome

    for e in checks:
        clean = e.get("outcome") == Outcome.CONFIRMED.value or (
            labels.get(str(e.get("write_id"))) == "caught"
        )
        run = run + 1 if clean else 0
    needed = clean_runs_needed()
    forced = os.environ.get("OPENADAPT_ENFORCEMENT", "").strip().lower()
    enabled = None
    for e in entries:
        if e.get("event") == "enable":
            enabled = True
        elif e.get("event") == "disable":
            enabled = False
    if forced in MODES:
        mode = forced
    else:
        mode = "gating" if enabled else "shadow"
    return GateStatus(
        contract=digest,
        mode=mode,
        clean_in_a_row=run,
        needed=needed,
        checks=len(checks),
        false_stops=sum(1 for v in labels.values() if v == "false_stop"),
        offered=mode == "shadow" and run >= needed,
    )


def observe(digest: str, result: Any) -> str:
    """Record one check and return the contract's mode for it. Never raises:
    a log that can't be read or written leaves the contract where it was
    (``shadow`` unless gating was on, or forced by the environment)."""

    try:
        _append(
            digest,
            {
                "event": "check",
                "at": time.time(),
                "write_id": str(getattr(result, "write_id", "") or ""),
                "outcome": str(getattr(getattr(result, "outcome", ""), "value", "")),
                "tier": str(getattr(result, "tier", "")),
            },
        )
    except (OSError, TypeError, ValueError):  # the log never changes a verdict
        pass
    try:
        return status(digest).mode
    except (OSError, TypeError, ValueError):
        forced = os.environ.get("OPENADAPT_ENFORCEMENT", "").strip().lower()
        return forced if forced in MODES else "shadow"


def label(digest: str, write_id: str, verdict: str) -> None:
    """A person's label for one stop: ``caught`` (a real harm) or
    ``false_stop`` (a correct write stopped)."""

    if verdict not in ("caught", "false_stop"):
        raise ValueError("label is 'caught' or 'false_stop'")
    _append(
        digest,
        {"event": "label", "at": time.time(), "write_id": write_id, "label": verdict},
    )


class GateRefused(ValueError):
    pass


def enable(digest: str, by: str) -> GateStatus:
    """Turn gating on, only once it is offered (``N`` clean checks in a row)."""

    st = status(digest)
    if not st.offered and st.mode != "gating":
        raise GateRefused(
            f"gating is not offered yet: {st.clean_in_a_row} of {st.needed} clean "
            "checks in a row"
        )
    _append(digest, {"event": "enable", "at": time.time(), "by": by})
    return status(digest)


def disable(digest: str, by: str) -> GateStatus:
    _append(digest, {"event": "disable", "at": time.time(), "by": by})
    return status(digest)


def contract_digest(path: Optional[str]) -> str:
    from openadapt_flow.verifier import Contract

    return Contract.load(str(path)).digest
