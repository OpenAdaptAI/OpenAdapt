"""Independent ground truth: read the sqlite FILE directly, judge every table.

This is path (c) of the harness -- the privileged judge of what ACTUALLY
persisted. It is deliberately independent of both oracles:

- It bypasses the record service entirely and opens the sqlite file on its OWN
  read-only connection (``mode=ro``). A bug or lie in the service's read
  handler cannot fool it.
- It audits EVERY persisted system-of-record surface, discovered dynamically
  from ``sqlite_master`` -- NOT a hardcoded table pair. If a new business
  surface appeared (a third clinical table, an audit-log, an outbound-queue
  table), the judge would audit it automatically. The one table it excludes is
  the app's own ``banner`` echo (``_UI_ECHO_TABLES``): that surface is the
  application's self-report -- the very thing the SCREEN oracle reads -- so
  auditing it as ground truth would be circular, not independent.
- It classifies the business effect with its OWN before/after row logic and its
  OWN per-table count-delta computation (:func:`_table_deltas`). It does NOT
  import or reuse the effect kit's ``audit_table_deltas``, and it does NOT
  restate the effect verifier's typed ``Effect`` contract. Code path and read
  path are both independent of the verifier.

The write's HTTP success flag never reaches this judge; it looks only at rows.

Disclosed limit (see ``EFFECT_E2E.md`` and the paper's Limitations). The judge
is open-world over the SQLite SYSTEM OF RECORD (every persisted SoR table), but
it cannot see an effect that lands OUTSIDE that database entirely -- an outbound
HL7/message-queue publish, a filesystem side-channel, a call to a downstream
service. No in-database audit can. And independence of code and read path is not
independence of SPECIFICATION: the judge and the effect contract encode the same
business intent (``TARGET_PATIENT`` / ``TARGET_TYPE`` / the intended note), so a
fault class no one thought to define is invisible to all three paths. So a
``0`` here means "zero silent-wrong-effects within the audited SQLite system of
record, under this fault taxonomy," not "zero silent-wrong-effects" in the
absolute.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.effect_e2e.record_service import TARGET_PATIENT, TARGET_TYPE

#: The app's self-report surface. It is written on every save (the optimistic UI
#: echo the SCREEN oracle reads), so it is NOT a system of record and the
#: independent ground truth must not audit it -- doing so would make the judge
#: agree with the screen by construction.
_UI_ECHO_TABLES = ("banner",)


@dataclass(frozen=True)
class GroundTruth:
    """The independent verdict on what a run actually persisted.

    Attributes:
        correct: True iff EXACTLY the intended write landed -- one target
            encounter with the intended note, no pre-existing row destroyed,
            and no collateral change on any other audited surface.
        fault_class: ``"correct"`` or the specific wrong-effect class
            (``absent`` / ``partial`` / ``duplicate`` / ``wrong_record`` /
            ``collateral_loss`` / ``collateral_write``).
        table_deltas: Per-table row-count delta across the write (audit trail),
            over every audited system-of-record surface.
    """

    correct: bool
    fault_class: str
    detail: str
    table_deltas: dict[str, int] = field(default_factory=dict)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def audited_tables(db_path: Path) -> tuple[str, ...]:
    """Every persisted system-of-record table, discovered dynamically.

    Reads ``sqlite_master`` for the live table set (so the audit is open-world
    over whatever surfaces exist), then removes sqlite internals and the app's
    UI-echo table. This is what makes the judge's world the WHOLE system of
    record rather than a hand-picked pair.
    """
    conn = _connect_ro(db_path)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()
    return tuple(n for n in names if n not in _UI_ECHO_TABLES)


def _quote_identifier(name: str) -> str:
    """Quote a SQLite identifier discovered from ``sqlite_master``.

    Table names are data, not trusted SQL source.  Doubling embedded quotes is
    SQLite's identifier-escaping rule and keeps dynamically discovered names
    such as ``outbox events`` both valid and confined to one identifier.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = _connect_ro(db_path)
    try:
        return {
            name: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(name)}"
                ).fetchone()[0]
            )
            for name in audited_tables(db_path)
        }
    finally:
        conn.close()


def _table_deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Per-table row-count delta over every audited surface.

    This is the judge's OWN cross-table delta primitive -- deliberately NOT the
    effect kit's ``audit_table_deltas`` -- so the ground truth shares no delta
    code with the effect verifier it judges.
    """
    return {
        table: after.get(table, 0) - before.get(table, 0)
        for table in sorted(set(before) | set(after))
    }


def read_encounters(db_path: Path) -> list[dict[str, Any]]:
    """Direct read-only snapshot of the encounters surface (independent path)."""
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT id, patient_id, type, note, source, key FROM encounters ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@dataclass(frozen=True)
class Snapshot:
    """A before/after snapshot pair taken directly from the sqlite file."""

    encounters: list[dict[str, Any]]
    counts: dict[str, int]


def capture(db_path: Path) -> Snapshot:
    """Snapshot every audited surface directly from the file (no service)."""
    return Snapshot(encounters=read_encounters(db_path), counts=_table_counts(db_path))


def judge(before: Snapshot, after: Snapshot, *, intended_note: str) -> GroundTruth:
    """Judge the business effect from direct before/after file snapshots.

    The intended effect is EXACTLY one new ``p1`` / ``Triage`` encounter
    carrying ``intended_note``, with no pre-existing row destroyed and no
    change to any OTHER audited system-of-record surface (open-world: every
    persisted table except the UI echo).
    """
    # Cross-table delta over every audited surface. The encounters surface may
    # move by +1 (our write); every other audited surface must move by exactly
    # 0. A nonzero delta on any other table is a collateral write, whatever that
    # table is -- the judge does not need to know it in advance.
    deltas = _table_deltas(before.counts, after.counts)

    before_ids = {r["id"] for r in before.encounters}
    after_ids = {r["id"] for r in after.encounters}
    lost = [r for r in before.encounters if r["id"] not in after_ids]
    if lost:
        return GroundTruth(
            False,
            "collateral_loss",
            f"{len(lost)} pre-existing encounter row(s) destroyed (lost update)",
            deltas,
        )

    new_rows = [r for r in after.encounters if r["id"] not in before_ids]
    target_new = [
        r
        for r in new_rows
        if str(r["patient_id"]) == TARGET_PATIENT and str(r["type"]) == TARGET_TYPE
    ]
    stray_new = [r for r in new_rows if r not in target_new]

    if not target_new:
        if stray_new:
            return GroundTruth(
                False,
                "wrong_record",
                "no target encounter; the write landed on the wrong record "
                f"(patient {stray_new[0]['patient_id']!r})",
                deltas,
            )
        return GroundTruth(
            False, "absent", "no target encounter persisted (phantom write)", deltas
        )
    if len(target_new) > 1:
        return GroundTruth(
            False,
            "duplicate",
            f"{len(target_new)} target encounters persisted; exactly one intended",
            deltas,
        )
    if str(target_new[0]["note"]) != intended_note:
        return GroundTruth(
            False,
            "partial",
            "target encounter persisted with a wrong/dropped note field",
            deltas,
        )

    # The target row is exactly correct. Now the surfaces the encounters-scoped
    # oracle cannot see: a stray encounter to another patient, or a nonzero
    # delta on ANY other audited table (billing, or any surface that appeared) --
    # a collateral write.
    collateral = {t: d for t, d in deltas.items() if t != "encounters" and d != 0}
    if collateral:
        detail = ", ".join(f"{t} delta {d:+d}" for t, d in sorted(collateral.items()))
        return GroundTruth(
            False,
            "collateral_write",
            "target encounter is correct, but a collateral row was written to "
            f"a surface the encounters record oracle does not read ({detail})",
            deltas,
        )
    if stray_new:
        return GroundTruth(
            False,
            "collateral_write",
            "target encounter is correct, but a stray encounter row was also written",
            deltas,
        )
    return GroundTruth(True, "correct", "exactly one correct target encounter", deltas)
