"""Keep a baseline's records as keyed MACs, and rebuild them when judging.

verify-run's baseline is what lets a check tell a record the save added from
one that was there, and see what else moved. It used to hold the read set's
rows as values: a copy of the records on disk. A sealed baseline holds, per
record, a keyed MAC of its identity and one of each field (BLAKE2b keyed with
the run's salt, as the database frame's snapshot does), never a value.

At judging time one fresh read rebuilds the records as they were:

- a record whose identity and fields all match is the record now, as read;
- a field whose MAC differs is set to :data:`CHANGED`, which no stored value
  can equal, so it shows as changed;
- a field the record didn't have stays absent;
- a record that is gone comes back as a placeholder whose identity is
  :data:`REMOVED` plus its MAC, so it shows as removed.

So every check downstream (new records, an exact new set, collateral
changes, the frame) works on the rebuilt records unchanged. Values never
reach the baseline file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from openadapt_flow.runtime.effects.db_snapshot import (
    canonical,
    cell_hash,
    row_key_hash,
)

#: A rebuilt field whose value moved since the baseline.
CHANGED = "\x00changed"
#: The identity prefix of a rebuilt record that is gone.
REMOVED = "\x00removed:"


def _identity_text(record: Mapping[str, Any], identity_field: Optional[str]) -> str:
    if identity_field and identity_field in record:
        return canonical(record[identity_field])
    if "id" in record:
        return canonical(record["id"])
    return json.dumps(record, sort_keys=True, default=str)


def seal(
    records: Sequence[Mapping[str, Any]], key: bytes, identity_field: Optional[str]
) -> list[dict[str, Any]]:
    """Each record's identity MAC and field MACs, in read order."""

    return [
        {
            "id": row_key_hash(key, _identity_text(r, identity_field)),
            "cells": {f: cell_hash(key, v) for f, v in r.items()},
        }
        for r in records
    ]


def unseal(
    sealed: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    key: bytes,
    identity_field: Optional[str],
) -> list[dict[str, Any]]:
    """The baseline's records, rebuilt from a read now (see the module)."""

    now: dict[str, Mapping[str, Any]] = {}
    for record in current:
        now.setdefault(
            row_key_hash(key, _identity_text(record, identity_field)), record
        )
    out: list[dict[str, Any]] = []
    for entry in sealed:
        cells: Mapping[str, str] = entry["cells"]
        record = now.get(str(entry["id"]))
        if record is None:
            gone = REMOVED + str(entry["id"])
            out.append({f: CHANGED for f in cells} | {identity_field or "id": gone})
            continue
        out.append(
            {
                f: (record.get(f) if cell_hash(key, record.get(f)) == mac else CHANGED)
                for f, mac in cells.items()
            }
        )
    return out
