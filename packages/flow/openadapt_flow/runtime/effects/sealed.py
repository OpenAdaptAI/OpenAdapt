"""Keep a baseline's records as keyed MACs, and rebuild them when judging.

verify-run's baseline is what lets a check tell a record the save added from
one that was there, and see what else moved. It used to hold the read set's
rows as values: a copy of the records on disk. A sealed baseline holds, per
record, a keyed MAC of its identity and one of each field (keyed BLAKE2b, 128 bits, bound
to its surface and field, with the database frame's key: the run's salt
keyed by a host secret never written into the baseline), never a value.

At judging time one fresh read rebuilds the records as they were:

- a record whose identity and fields all match is the record now, as read;
- a field whose MAC differs is set to :data:`CHANGED`, a per-process random
  marker no stored value can equal, so it shows as changed (a cell's MAC
  covers its type, so ``True`` and ``"true"`` differ);
- a field the record didn't have stays absent;
- a record that is gone comes back as a placeholder whose identity is
  :data:`REMOVED` plus its MAC, so it shows as removed.

So every check downstream (new records, an exact new set, collateral
changes, the frame) works on the rebuilt records unchanged. Values never
reach the baseline file, though whoever can read the host secret can test
guesses for short values against it.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Optional

from openadapt_flow.runtime.effects.db_snapshot import (
    canonical,
    cell_hash,
    row_key_hash,
)

#: A rebuilt field whose value moved since the baseline. Random per process,
#: so no value an app stores (or an agent writes) can equal it.
CHANGED = "\x00changed:" + secrets.token_hex(16)
#: The identity prefix of a rebuilt record that is gone (random, as above).
REMOVED = "\x00removed:" + secrets.token_hex(16) + ":"


def _typed(value: Any) -> str:
    """``canonical`` with its type, so ``True`` and ``"true"``, or an object
    and its JSON text, don't seal alike. Numbers share one class."""

    if value is None:
        tag = "n"
    elif isinstance(value, bool):
        tag = "b"
    elif isinstance(value, (int, float, Decimal)):
        tag = "d"
    elif isinstance(value, (dict, list)):
        tag = "j"
    elif isinstance(value, str):
        tag = "s"
    elif isinstance(value, (bytes, bytearray)):
        return "x:" + bytes(value).hex()
    else:
        # Anything else (a tuple, a date) carries its type name, so it never
        # seals like its text.
        return f"o:{type(value).__name__}:{canonical(value)}"
    return f"{tag}:{canonical(value)}"


def _cell(key: bytes, value: Any, where: str, field: str) -> str:
    return cell_hash(key, _typed(value), where, field)


def _identity_text(record: Mapping[str, Any], identity_field: Optional[str]) -> str:
    if identity_field and identity_field in record:
        return canonical(record[identity_field])
    if "id" in record:
        return canonical(record["id"])
    return json.dumps(record, sort_keys=True, default=str)


def seal(
    records: Sequence[Mapping[str, Any]],
    key: bytes,
    identity_field: Optional[str],
    surface: str = "",
) -> list[dict[str, Any]]:
    """Each record's identity MAC and field MACs (bound to the surface and
    field), in read order. ``key`` is the run's MAC key (``mac_key``)."""

    where = f"surface:{surface}"
    return [
        {
            "id": row_key_hash(key, _identity_text(r, identity_field), where),
            "cells": {f: _cell(key, v, where, f) for f, v in r.items()},
        }
        for r in records
    ]


def unseal(
    sealed: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    key: bytes,
    identity_field: Optional[str],
    surface: str = "",
) -> list[dict[str, Any]]:
    """The baseline's records, rebuilt from a read now (see the module)."""

    where = f"surface:{surface}"
    now: dict[str, Mapping[str, Any]] = {}
    for record in current:
        now.setdefault(
            row_key_hash(key, _identity_text(record, identity_field), where), record
        )
    out: list[dict[str, Any]] = []
    for entry in sealed:
        cells: Mapping[str, str] = entry["cells"]
        found = now.get(str(entry["id"]))
        if found is None:
            gone = REMOVED + str(entry["id"])
            out.append({f: CHANGED for f in cells} | {identity_field or "id": gone})
            continue
        out.append(
            {
                f: (
                    found.get(f)
                    if _cell(key, found.get(f), where, f) == mac
                    else CHANGED
                )
                for f, mac in cells.items()
            }
        )
    return out
