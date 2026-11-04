"""Does an API step leave the same footprint as the GUI save it replaces?

Repeating a save as one request skips whatever the page would have done
around it: script that computes a field, a request that writes an audit
entry, links a record or queues a notification. The record check reads back
the record the save names; it doesn't see a side effect the API step
skipped. So an API step is qualified by its footprint: the whole database
after N saves through the GUI and N through the API step, on the same
synthetic inputs, compared table by table and field by field.

The comparison is differential. Some columns differ on every save whatever
the path (an id, a creation time, a hash): a column whose value varies
between the GUI saves themselves is **server-owned** and isn't compared.
Every other difference between a GUI save and the API save on the same
inputs is one the API step caused: a table one path wrote and the other
didn't, a different number of rows, or a column with another value. Any
difference keeps the save on the GUI (:func:`admit_api_step` refuses it).

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


def _canon(row: Row) -> str:
    return json.dumps(
        {k: (None if v is None else str(v)) for k, v in row.items()}, sort_keys=True
    )


def diff_dumps(before: Dump, after: Dump) -> Changes:
    """What changed between two dumps of a database, table by table.

    Rows are matched by an ``id`` column when the table has one (a changed
    row is one entry under ``changed``), else as a multiset of whole rows.
    """

    out: Changes = {}
    for table in sorted(set(before) | set(after)):
        old, new = list(before.get(table) or []), list(after.get(table) or [])
        key = _key_column(old or new)
        entry: dict[str, list[Any]] = {"added": [], "removed": [], "changed": []}
        if key is not None:
            o = {str(r.get(key)): r for r in old}
            n = {str(r.get(key)): r for r in new}
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
    """``(table, op) -> rows``, normalized; a changed row is its changed columns' new values."""

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
                    k: _norm(a.get(k), markers)
                    for k in a
                    if str(a.get(k)) != str(b.get(k))
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
    Needs at least two GUI saves, to tell server-owned columns from values.
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
    # Server-owned: columns whose value varies between the GUI saves alone.
    owned: set[tuple[str, str]] = set()
    keys = set().union(*[set(x) for x in g])
    for key in sorted(keys):
        runs = [_sorted(x.get(key, [])) for x in g]
        if len({len(r) for r in runs}) != 1:
            differences.append(
                f"{key[0]}: the GUI saves themselves {key[1]} different numbers of rows "
                f"({', '.join(str(len(r)) for r in runs)}); the footprint isn't stable"
            )
            continue
        for rows in zip(*runs):
            cols = set().union(*[set(r) for r in rows])
            for col in cols:
                if len({str(r.get(col)) for r in rows}) > 1:
                    owned.add((key[0], col))
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
            strip = lambda r: {k: v for k, v in r.items() if (table, k) not in owned}  # noqa: E731
            for gr, ar in zip(
                _sorted([strip(r) for r in grow]), _sorted([strip(r) for r in arow])
            ):
                differ = sorted(
                    c for c in set(gr) | set(ar) if str(gr.get(c)) != str(ar.get(c))
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
        "server_owned": sorted(f"{t}.{c}" for t, c in owned),
        "tables": tables,
        "pairs": len(gui),
    }


def verdict_digest(verdict: Mapping[str, Any]) -> str:
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
