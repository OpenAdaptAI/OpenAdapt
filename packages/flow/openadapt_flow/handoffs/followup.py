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


def _is_file(path: Path) -> bool:
    # The run directory is encrypted by default: its files are read, written
    # and removed through its sealed set (in memory), never in plaintext.
    from openadapt_flow import sealed_set

    return sealed_set.is_file(path)


def _read(path: Path) -> str:
    from openadapt_flow import sealed_set

    return sealed_set.read_text(path)


def _unlink(path: Path) -> None:
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed_path(path):
        sealed_set.unlink(path, missing_ok=True)
    else:
        path.unlink(missing_ok=True)


def _write_json(path: Path, payload: Any, *, private: bool) -> None:
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed_path(path):
        sealed_set.write_text(
            path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644
    )
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _sidecar(run_dir: Path) -> dict[str, Any]:
    path = run_dir / SIDECAR
    if not _is_file(path):
        return {"schema": SCHEMA, "records": []}
    data = json.loads(_read(path))
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
    from openadapt_flow import sealed_set

    for path in sealed_set.list_files(run_dir, "receipt.update-*"):
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


def _pending_path(run_dir: Path) -> Path:
    return run_dir / ".receipt-update.pending.json"


def _finish_pending(run_dir: Path, registry: Sequence[Any]) -> None:
    """Complete or roll back an update whose publication was interrupted.

    The pending note is written before any update file; if the update it
    names is now a whole signed record, its sidecar entry is added, and
    otherwise the partial files are removed. Either way the note goes."""
    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict

    pending = _pending_path(run_dir)
    if not _is_file(pending):
        return
    note = json.loads(_read(pending))
    out = run_dir / str(note["file"])
    private = run_dir / str(note["private"])
    complete = False
    try:
        envelope = loads_strict(_read(out))
        complete = r.verify_receipt(envelope, registry).ok and _is_file(private)
    except (OSError, ValueError):
        complete = False
    if complete:
        data = _sidecar(run_dir)
        if note["file"] not in [rec["file"] for rec in data["records"]]:
            data["records"].append({"file": note["file"], "lines": note["lines"]})
            _write_json(run_dir / SIDECAR, data, private=True)
    else:
        for partial in (out, private):
            _unlink(partial)
    _unlink(pending)


def sign_updates(
    run_dir: str | Path,
    ledgers: Sequence[HandoffLedger],
    *,
    signer: Any,
) -> Optional[Path]:
    """Sign one follow-up for every handoff of the run whose line changed
    since it was last signed. None when nothing changed. Needs the run's
    receipt (``receipt.json``).

    Race class: one signer at a time per run (an exclusive lock from the
    history check through publication), and an interrupted publication is
    completed or rolled back by the next call."""

    run_dir = Path(run_dir)
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed(run_dir):
        # An encrypted run's writes take its set's lock per write; one signer
        # at a time holds a lock of its own instead of a file in the set.
        with sealed_set.side_lock(run_dir, "receipt-update"):
            return _sign_updates_locked(run_dir, ledgers, signer=signer)
    lock = run_dir / ".receipt-update.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(
            f"another signer holds {lock.name}; remove it if no signer is running"
        ) from None
    os.close(fd)
    try:
        return _sign_updates_locked(run_dir, ledgers, signer=signer)
    finally:
        lock.unlink(missing_ok=True)


def _sign_updates_locked(
    run_dir: Path,
    ledgers: Sequence[HandoffLedger],
    *,
    signer: Any,
) -> Optional[Path]:
    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict

    kid = r.key_id(signer.public_raw())
    own = r.RoleKey(
        kid, base64.b64encode(signer.public_raw()).decode("ascii"), "this_computer"
    )
    _finish_pending(run_dir, [own])
    original = loads_strict(_read(run_dir / r.RECEIPT_FILE))
    # Extend only a chain that verifies as it stands, signed by this key
    # alone: the receipt and every update already written. verify_chain
    # refuses a follow-up from any other key.
    existing = verify_history(run_dir, [own])
    if existing is None or not existing.ok:
        reason = existing.reason if existing is not None else "no receipt"
        detail = (
            f" ({existing.detail})" if existing is not None and existing.detail else ""
        )
        raise ValueError(f"the run's receipt does not verify: {reason}{detail}")
    from openadapt_flow import sealed_set

    raw_report = sealed_set.read_bytes(run_dir / "report.json")
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
    if _is_file(out):
        raise ValueError(f"{out.name} was written by another signer")
    # Durable before any update file: what this update is, so an
    # interrupted publication can be completed or rolled back.
    _write_json(
        _pending_path(run_dir),
        {
            "file": out.name,
            "private": update_private_file(sequence),
            "lines": [{"handoff": h.id, "signed": _signature(h)} for h in changed],
        },
        private=True,
    )
    _write_json(
        run_dir / update_private_file(sequence),
        r.private_openings(envelope, openings),
        private=True,
    )
    _write_json(out, envelope, private=False)
    record_signed(run_dir, out.name, changed)
    _unlink(_pending_path(run_dir))
    return out


def verify_history(run_dir: str | Path, registry: Sequence[Any]) -> Any:
    """``verify_chain`` over the run's receipt and its follow-ups, with
    their private openings when present."""

    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict

    run_dir = Path(run_dir)

    def load(name: str) -> Optional[Mapping[str, Any]]:
        path = run_dir / name
        return loads_strict(_read(path)) if _is_file(path) else None

    original = load(r.RECEIPT_FILE)
    found = updates(run_dir)
    if original is None:
        if found or _is_file(run_dir / SIDECAR):
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
    chain = [loads_strict(_read(p)) for p in found]
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
