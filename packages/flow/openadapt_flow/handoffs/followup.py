"""Follow-up receipts: a signed record of each change after the run's receipt.

A run's receipt is signed once, when the run ends, so its handoff lines are
usually ``sent``. When a handoff later changes (acknowledged, recorded as
intended, recorded differently), :func:`sign_updates` signs a small
follow-up record: a ``record/run/v1`` statement that repeats the original's
predicate, names the original's payload digest in ``follows``, counts its
place in the chain (``sequence``), and carries only the confirmation lines
that changed. ``openadapt_types.receipt.verify_chain`` checks the chain, and
``handoff list --run`` shows it.

Files in the run folder:

- ``receipt.update-N.json``: the N-th follow-up (a DSSE envelope);
- ``receipt.update-N.private.json``: its openings (0600);
- ``receipt.handoffs.json``: which handoff each signed line belongs to, and
  the line as signed, so a later update signs only what changed (0600,
  local only: it names handoff ids, never values).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.handoffs.ledger import Handoff, HandoffLedger
from openadapt_flow.handoffs.line import (
    build_confirmations,
    handoffs_for_run,
    place,
    to_line,
)

SIDECAR = "receipt.handoffs.json"
SCHEMA = "openadapt.receipt-handoffs/v1"


def update_file(sequence: int) -> str:
    return f"receipt.update-{sequence}.json"


def update_private_file(sequence: int) -> str:
    return f"receipt.update-{sequence}.private.json"


def _signature(handoff: Handoff) -> dict[str, Any]:
    """What a signed line says, minus when it was observed: a new read of
    the same state is not a change."""

    item = to_line(handoff)
    values = {k: v for k, v in item.values.items() if k != "observed_at"}
    return {**item.line, "values": values}


def _write_json(path: Path, payload: Any, *, private: bool) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644
    )
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _sidecar(run_dir: Path) -> dict[str, Any]:
    path = run_dir / SIDECAR
    if not path.is_file():
        return {"schema": SCHEMA, "records": []}
    data = json.loads(path.read_text("utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{SIDECAR} is not {SCHEMA}")
    return data


def record_signed(run_dir: Path, file: str, handoffs: Sequence[Handoff]) -> None:
    """Remember which handoffs a signed record's lines are, in line order."""

    data = _sidecar(run_dir)
    data["records"].append(
        {
            "file": file,
            "lines": [{"handoff": h.id, "signed": _signature(h)} for h in handoffs],
        }
    )
    _write_json(run_dir / SIDECAR, data, private=True)


def last_signed(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Each handoff's latest signed line, across the receipt and its updates."""

    latest: dict[str, dict[str, Any]] = {}
    for record in _sidecar(run_dir)["records"]:
        for item in record["lines"]:
            latest[item["handoff"]] = item["signed"]
    return latest


def updates(run_dir: Path) -> list[Path]:
    found = []
    sequence = 1
    while (run_dir / update_file(sequence)).is_file():
        found.append(run_dir / update_file(sequence))
        sequence += 1
    return found


def sign_updates(
    run_dir: str | Path,
    ledgers: Sequence[HandoffLedger],
    *,
    signer: Any,
) -> Optional[Path]:
    """Sign one follow-up for every handoff of the run whose line changed
    since it was last signed. None when nothing changed. Needs the run's
    receipt (``receipt.json``)."""

    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict

    run_dir = Path(run_dir)
    original = loads_strict((run_dir / r.RECEIPT_FILE).read_text("utf-8"))
    report = loads_strict((run_dir / "report.json").read_text("utf-8"))
    signed = last_signed(run_dir)
    statement = r.loads_strict(base64.b64decode(original["payload"]))
    changed: list[Handoff] = []
    for handoff in place(statement, handoffs_for_run(report, list(ledgers), run_dir)):
        if signed.get(handoff.id) != _signature(handoff):
            changed.append(handoff)
    if not changed:
        return None
    sequence = len(updates(run_dir)) + 1
    lines, openings = build_confirmations(changed)
    follow_up = r.follow_up_statement(original, sequence, lines)
    kid = r.key_id(signer.public_raw())
    envelope = r.sign_statement(follow_up, [(kid, signer.sign)])
    out = run_dir / update_file(sequence)
    _write_json(
        run_dir / update_private_file(sequence),
        r.private_openings(envelope, openings),
        private=True,
    )
    _write_json(out, envelope, private=False)
    record_signed(run_dir, out.name, changed)
    return out


def verify_history(run_dir: str | Path, registry: Sequence[Any]) -> Any:
    """``verify_chain`` over the run's receipt and its follow-ups, with
    their private openings when present."""

    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict

    run_dir = Path(run_dir)

    def load(name: str) -> Optional[Mapping[str, Any]]:
        path = run_dir / name
        return loads_strict(path.read_text("utf-8")) if path.is_file() else None

    original = load(r.RECEIPT_FILE)
    if original is None:
        return None
    chain = [loads_strict(p.read_text("utf-8")) for p in updates(run_dir)]
    return r.verify_chain(
        original,
        chain,
        registry,
        openings=load(r.PRIVATE_FILE),
        update_openings=[load(update_private_file(i + 1)) for i in range(len(chain))],
    )


__all__ = [
    "SIDECAR",
    "last_signed",
    "record_signed",
    "sign_updates",
    "update_file",
    "updates",
    "verify_history",
]
