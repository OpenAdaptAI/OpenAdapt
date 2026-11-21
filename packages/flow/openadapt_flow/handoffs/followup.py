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
import hashlib
import json
import os
import re
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


_UPDATE_NAME = re.compile(r"^receipt\.update-(.*)\.json$")


def updates(run_dir: Path) -> list[Path]:
    """Every ``receipt.update-N.json`` in the run, in order.

    Fails closed (ValueError) on a gap in the numbering or a name that isn't
    ``receipt.update-<N>.json`` / ``.private.json`` with N counted from 1, so
    no update file is ever silently left out of the history.
    """
    numbers: list[int] = []
    private_numbers: set[int] = set()
    for path in Path(run_dir).iterdir():
        match = _UPDATE_NAME.match(path.name)
        if not match:
            continue
        stem = match.group(1)
        if stem.endswith(".private"):
            stem = stem[: -len(".private")]
            private = True
        else:
            private = False
        if not re.fullmatch(r"[1-9][0-9]{0,5}", stem):
            raise ValueError(f"unexpected update file {path.name}")
        (private_numbers.add if private else numbers.append)(int(stem))
    numbers.sort()
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("the run's update files skip a number")
    if not private_numbers <= set(numbers):
        raise ValueError("a private update file has no update beside it")
    return [Path(run_dir) / update_file(n) for n in numbers]


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
    # Extend only a chain that verifies as it stands, signed by this key
    # alone: the receipt and every update already written. verify_chain
    # refuses a follow-up from any other key.
    kid = r.key_id(signer.public_raw())
    own = r.RoleKey(
        kid, base64.b64encode(signer.public_raw()).decode("ascii"), "this_computer"
    )
    existing = verify_history(run_dir, [own])
    if existing is None or not existing.ok:
        reason = existing.reason if existing is not None else "no receipt"
        detail = (
            f" ({existing.detail})" if existing is not None and existing.detail else ""
        )
        raise ValueError(f"the run's receipt does not verify: {reason}{detail}")
    raw_report = (run_dir / "report.json").read_bytes()
    statement = r.loads_strict(base64.b64decode(original["payload"]))
    subjects = {
        x["name"]: x["digest"]["sha256"] for x in statement.get("subject") or []
    }
    if subjects.get("report.json") != hashlib.sha256(raw_report).hexdigest():
        # Wrong-subject class: choose the handoffs from the very report the
        # receipt signed, never a later or another run's report.
        raise ValueError("report.json is not the report the receipt signed")
    report = loads_strict(raw_report.decode("utf-8"))
    signed = last_signed(run_dir)
    changed: list[Handoff] = []
    for handoff in place(statement, handoffs_for_run(report, list(ledgers), run_dir)):
        if signed.get(handoff.id) != _signature(handoff):
            changed.append(handoff)
    if not changed:
        return None
    sequence = len(updates(run_dir)) + 1
    lines, openings = build_confirmations(changed)
    follow_up = r.follow_up_statement(original, sequence, lines)
    envelope = r.sign_statement(follow_up, [(kid, signer.sign)])
    out = run_dir / update_file(sequence)
    # Race class: a sequence is claimed exclusively, so two signers can't
    # write the same update; the loser fails and signs nothing.
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise ValueError(f"{out.name} was written by another signer") from None
    os.close(fd)
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
    found = updates(run_dir)
    if original is None:
        if found or (run_dir / SIDECAR).is_file():
            # Missing-evidence class: signed updates without their receipt are
            # a broken history, not an unsigned run.
            raise ValueError("signed updates are here but receipt.json is missing")
        return None
    # Missing-evidence class: every update the sidecar says was signed must
    # still be here, so a dropped last update can't shorten the history.
    recorded = [
        rec["file"]
        for rec in _sidecar(run_dir)["records"]
        if rec["file"] != r.RECEIPT_FILE
    ]
    if sorted(recorded) != sorted(p.name for p in found):
        raise ValueError("the signed updates don't match the record of what was signed")
    chain = [loads_strict(p.read_text("utf-8")) for p in found]
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
