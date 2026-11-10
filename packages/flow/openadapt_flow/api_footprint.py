"""Does an API step leave the same footprint as the GUI save it replaces?

Repeating a save as one request skips whatever the page would have done
around it: script that computes a field, a request that writes an audit
entry, links a record or queues a notification. The record check reads back
the record the save names; it doesn't see a side effect the API step
skipped. So an API step is qualified by its footprint: the whole database
after N saves through the GUI and N through the API step, on the same
synthetic inputs, compared table by table and field by field.

Changed rows retain both complete row states, including their unchanged
identity. Variation between GUI saves does not establish server ownership:
a business counter can vary too. Every field is compared, including
inserted IDs and times. Dumps alone cannot prove that a differing field
is generated or safe to omit, so unexplained differences refuse.

Each run's own unique marker (the tag that keeps repeated runs from
colliding) is replaced by ``<run>`` before comparing. Values are compared in
memory and never written: the verdict names tables and columns only.

Model-free and deterministic. Dumps come from the caller (a read-only query
of every table); :func:`diff_dumps` turns two into the changes a save made.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

Row = Mapping[str, Any]
Dump = Mapping[str, Sequence[Row]]
#: table -> {"added": [row], "removed": [row], "changed": [{"before", "after"}]}
Changes = dict[str, dict[str, list[Any]]]

SCHEMA = "openadapt.api-footprint/v1"


def _key_column(rows: Sequence[Row]) -> str | None:
    cols = list(rows[0]) if rows else []
    for name in cols:
        if name.lower() in ("id", "rowid"):
            return name
    return None


def _unique_key(rows: Sequence[Row], key: str) -> bool:
    """Does ``key`` identify each row on its own: present, non-null, unique?

    A column named ``id`` is not proof of a row key. It can be one part of a
    composite key, repeat across tenants, or be null. Indexing such a table by
    it would keep only the last row per value and hide writes to the others.
    """

    seen: set[str] = set()
    for r in rows:
        if key not in r or r[key] is None:
            return False
        k = _canon({"k": r[key]})
        if k in seen:
            return False
        seen.add(k)
    return True


def _canon(row: Row) -> str:
    return json.dumps(
        {k: (None if v is None else str(v)) for k, v in row.items()}, sort_keys=True
    )


def diff_dumps(before: Dump, after: Dump) -> Changes:
    """What changed between two dumps of a database, table by table.

    Rows are matched by an ``id`` column only when that column is present,
    non-null and unique in both dumps (a changed row is then one entry under
    ``changed``). Otherwise rows are compared as a multiset of whole rows, so
    a changed row is one ``removed`` and one ``added`` entry and duplicate
    rows keep their multiplicity.
    """

    out: Changes = {}
    for table in sorted(set(before) | set(after)):
        old, new = list(before.get(table) or []), list(after.get(table) or [])
        key = _key_column(old or new)
        if key is not None and not (_unique_key(old, key) and _unique_key(new, key)):
            key = None
        entry: dict[str, list[Any]] = {"added": [], "removed": [], "changed": []}
        if key is not None:
            o = {_canon({"k": r[key]}): r for r in old}
            n = {_canon({"k": r[key]}): r for r in new}
            entry["added"] = [n[k] for k in n if k not in o]
            entry["removed"] = [o[k] for k in o if k not in n]
            entry["changed"] = [
                {"before": o[k], "after": n[k]}
                for k in n
                if k in o and _canon(o[k]) != _canon(n[k])
            ]
        else:
            counts: dict[str, int] = {}
            for r in old:
                counts[_canon(r)] = counts.get(_canon(r), 0) + 1
            for r in new:
                c = _canon(r)
                if counts.get(c, 0) > 0:
                    counts[c] -= 1
                else:
                    entry["added"].append(r)
            left = dict(counts)
            for r in old:
                c = _canon(r)
                if left.get(c, 0) > 0:
                    entry["removed"].append(r)
                    left[c] -= 1
        if any(entry.values()):
            out[table] = entry
    return out


def _norm(value: Any, markers: Iterable[str]) -> Any:
    if value is None:
        return None
    text = str(value)
    for m in markers:
        if m:
            text = text.replace(m, "<run>")
    return text


def _items(
    changes: Changes, markers: Sequence[str]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """``(table, op) -> rows``, preserving complete changed-row transitions."""

    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for table, entry in changes.items():
        for op in ("added", "removed"):
            rows = [
                {k: _norm(v, markers) for k, v in r.items()}
                for r in entry.get(op) or []
            ]
            if rows:
                out[(table, op)] = rows
        changed = []
        for pair in entry.get("changed") or []:
            b, a = pair["before"], pair["after"]
            changed.append(
                {
                    state: {k: _norm(v, markers) for k, v in row.items()}
                    for state, row in (("before", b), ("after", a))
                }
            )
        if changed:
            out[(table, "changed")] = changed
    return out


def _sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


def footprint_verdict(
    gui: Sequence[Changes],
    api: Sequence[Changes],
    gui_markers: Sequence[str],
    api_markers: Sequence[str],
) -> dict[str, Any]:
    """Compare N GUI saves with N API saves made on the same inputs, pairwise.

    ``gui[i]`` and ``api[i]`` are the changes of saves with the same inputs
    apart from their run markers (``gui_markers[i]``, ``api_markers[i]``).
    Needs at least two pairs. No field is excluded based on variation.
    Existing rows retain complete before/after values. Generated insert
    fields that differ also refuse: dumps do not establish ownership.
    """

    if (
        len(gui) < 2
        or len(gui) != len(api)
        or len(gui_markers) != len(gui)
        or len(api_markers) != len(api)
    ):
        return {
            "schema": SCHEMA,
            "equivalent": False,
            "differences": [
                "needs at least two GUI saves and as many API saves, paired"
            ],
            "server_owned": [],
            "tables": {},
        }
    g = [_items(c, [m]) for c, m in zip(gui, gui_markers)]
    a = [_items(c, [m]) for c, m in zip(api, api_markers)]
    differences: list[str] = []
    keys = set().union(*[set(x) for x in g])
    for key in sorted(keys):
        runs = [x.get(key, []) for x in g]
        if len({len(r) for r in runs}) != 1:
            differences.append(
                f"{key[0]}: the GUI saves themselves {key[1]} different numbers of rows "
                f"({', '.join(str(len(r)) for r in runs)}); the footprint isn't stable"
            )
    tables: dict[str, dict[str, int]] = {}
    for i, (gi, ai) in enumerate(zip(g, a), 1):
        for key in sorted(set(gi) | set(ai)):
            table, op = key
            grow, arow = _sorted(gi.get(key, [])), _sorted(ai.get(key, []))
            tables.setdefault(table, {}).setdefault(f"gui_{op}", 0)
            tables[table][f"gui_{op}"] += len(grow)
            tables[table][f"api_{op}"] = tables[table].get(f"api_{op}", 0) + len(arow)
            if len(grow) != len(arow):
                differences.append(
                    f"pair {i}: {table} {op} {len(grow)} row(s) through the GUI and "
                    f"{len(arow)} through the API step"
                )
                continue
            for gr, ar in zip(grow, arow):
                if op == "changed":
                    differ = sorted(
                        f"{state}.{column}"
                        for state in ("before", "after")
                        for column in set(gr[state]) | set(ar[state])
                        if gr[state].get(column) != ar[state].get(column)
                    )
                else:
                    differ = sorted(
                        c for c in set(gr) | set(ar) if gr.get(c) != ar.get(c)
                    )
                if differ:
                    differences.append(
                        f"pair {i}: {table} {op} rows differ in {', '.join(differ[:8])}"
                    )
    differences = sorted(set(differences))
    return {
        "schema": SCHEMA,
        "equivalent": not differences,
        "differences": differences,
        "server_owned": [],
        "tables": tables,
        "pairs": len(gui),
    }


def verdict_digest(verdict: Mapping[str, Any]) -> str:
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
