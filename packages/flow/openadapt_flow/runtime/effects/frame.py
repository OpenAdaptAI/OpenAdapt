"""Frame conditions: prove that nothing outside the declared delta changed.

An effect contract says what a save must do. It says nothing about what the
save must not do, so a check built from it can't see an edit to a record it
never names (OpenAdaptAI/OpenAdapt#1365: another chart edited in place, 5 of 5
saves reported verified). A frame condition closes that gap the way a
procedure's ``modifies`` clause does in program verification: the contract
declares the records and fields the save may change, and everything else in
the slice must be unchanged.

The slice is the verifier's read set: the records it returns before and after
the save, through the same read-only channel the effect check uses. The judge
runs once for the whole contract, not once per effect, so two effects on one
read set never flag each other's targets.

Every change outside the declared records is attributed:

- ``other``: an audit entry, or the record's own modified-by field with a
  moved version, names an actor that is not the bot. A concurrent edit by
  another user. Listed, and not counted against the save.
- ``self``: the bot's own user or session id. Collateral.
- ``unattributed``: nothing names an actor. Collateral too: a change nobody
  can explain is never assumed to be someone else's.

An audit entry by the bot for a record no effect declares is collateral even
when the read shows nothing: the log shows a write the contract doesn't cover.

Only evidence the bot can't write counts (the safety review of #1443):

- attribution comes from the app's audit log, or from record fields the
  frame declares as server-set (``server_set``); a field the run itself
  writes is never evidence of who changed a record, so attribution that
  names one is refused and every change stays unattributed;
- the run's own identities (``self`` and the executor's id) are never
  "another actor";
- another actor's change to a record the save declares is never excused:
  the saved value may no longer stand, so it blocks like collateral. Only
  changes outside the declared delta can be concurrent edits.

With ``frame.database``, another actor's entry explains a row only when it
is a write of the matching kind (insert for an added row, delete for a
removed one, update for a changed one) on the same table, naming the record
as it was before the change; each entry explains at most one row, and a row
whose record key changed is never explained. What the log can't show stays a
limit: an entry the app files under the wrong record (a write logged against
the chart the bot has open), or one colleague insert whose own row never
landed, still reads as that colleague's. List every app account the bot
holds in ``self``.

The judge reads nothing itself; callers pass the snapshots. Fail-safe: a
missing baseline, an unreadable read, a record without its identity field or
an unresolved self id makes the frame ``not_checked``, never ``held``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
    record_matches,
    stable_id,
)

FrameStatus = Literal["held", "refuted", "not_checked"]
Attribution = Literal["other", "self", "unattributed"]
ChangeKind = Literal["added", "removed", "changed"]

#: How many changes one reason line names before it stops listing kinds.
_SAMPLE_LIMIT = 5


class FrameAttribution(BaseModel):
    """Where the judge learns who changed a record.

    ``audit_read``: a second read-only read (``read``, the same shape as an
    ``effects:`` verifier section) returns the app's audit log or change
    feed. Entries present after the save and not before are the window's
    entries. ``record_field`` names the entry field that refers to a record
    (compared with :attr:`FrameSpec.identity_field`), ``actor_field`` the
    user or session.

    ``record_field`` attribution: each record carries its last modifier in
    ``actor_field`` and, optionally, a version or ETag in ``version_field``.
    A changed record whose version did not move is unattributed.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["audit_read", "record_field"]
    #: Entry (or record) field naming who made the change. Dotted paths
    #: reach nested fields (``meta.lastUpdatedBy``).
    actor_field: str = Field(min_length=1)
    #: The bot's own user or session ids. Literals or run parameters.
    self_actors: list[ValueExpr] = Field(alias="self", min_length=1)
    #: ``audit_read`` only: the entry field naming the changed record.
    record_field: Optional[str] = None
    #: ``audit_read`` only: the read-only read of the log, as an ``effects:``
    #: verifier section. Built by the caller (``verify-run``).
    read: Optional[dict[str, Any]] = None
    #: ``audit_read`` only: the record field the audit entry's
    #: ``record_field`` refers to, when it isn't the frame's identity field.
    #: OpenEMR's log names a patient (``pid``) while ``lists`` or ``pnotes``
    #: rows are told apart by their own ``id``.
    record_key: Optional[str] = None
    #: ``audit_read`` only: per surface or table, the field that names the
    #: record when it isn't ``record_key`` (OpenEMR's ``contact`` row points
    #: at its chart through ``foreign_id``).
    record_keys: dict[str, str] = Field(default_factory=dict)
    #: ``audit_read`` only: the entry field naming what the entry did, and
    #: the values that are writes. Set, only write entries attribute a change:
    #: an entry for viewing a record never excuses a write to it. Required
    #: with ``frame.database``.
    event_field: Optional[str] = None
    write_events: list[str] = Field(default_factory=list)
    #: The write events that create a record (a subset of ``write_events``).
    #: Another actor's later write to a record the bot created can be told
    #: apart from the bot's own insert only through these.
    insert_events: list[str] = Field(default_factory=list)
    #: The write events that remove a record (a subset of ``write_events``).
    #: An added row is explained only by an insert, a removed one only by a
    #: delete, a changed one only by the other write events.
    delete_events: list[str] = Field(default_factory=list)
    #: ``audit_read`` only: the entry field naming the table written. Set,
    #: an entry attributes a change only to a row of that table. Required
    #: with ``frame.database``.
    table_field: Optional[str] = None
    #: ``record_field`` only: a version, revision or ETag on each record.
    version_field: Optional[str] = None
    #: ``record_field`` only: the record fields this app sets on the server
    #: and no user or API client can write. ``actor_field`` and
    #: ``version_field`` must be among them: declaring them is the per-app
    #: statement that they are evidence.
    server_set: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_self(cls, data: Any) -> Any:
        if isinstance(data, dict):
            key = "self" if "self" in data else "self_actors"
            values = data.get(key)
            if isinstance(values, list):
                data = dict(data)
                data[key] = [
                    ValueExpr(literal=v) if isinstance(v, str) else v for v in values
                ]
        return data

    @model_validator(mode="after")
    def _shape(self) -> "FrameAttribution":
        if self.kind == "audit_read":
            if not self.record_field or self.read is None:
                raise ValueError("audit_read attribution needs record_field and read")
            if self.version_field is not None:
                raise ValueError("version_field applies to record_field attribution")
            if self.server_set:
                raise ValueError("server_set applies to record_field attribution")
            if bool(self.event_field) != bool(self.write_events):
                raise ValueError("event_field and write_events go together")
            if not (set(self.insert_events) | set(self.delete_events)) <= set(
                self.write_events
            ):
                raise ValueError("insert_events and delete_events must be write_events")
        elif self.record_keys or self.write_events or self.table_field:
            raise ValueError(
                "record_keys, write_events and table_field apply to audit_read"
            )
        else:
            if self.read is not None or self.record_field is not None:
                raise ValueError(
                    "record_field attribution reads the records themselves; "
                    "read and record_field apply to audit_read"
                )
            needed = {self.actor_field} | (
                {self.version_field} if self.version_field else set()
            )
            missing = sorted(needed - set(self.server_set))
            if missing:
                raise ValueError(
                    "record_field attribution needs its fields declared server-set "
                    "(fields no user or API client can write): " + ", ".join(missing)
                )
        return self


class FrameSideRecord(BaseModel):
    """Rows the save adds beside each new declared record (a side record).

    OpenEMR's New Patient path writes one ``history_data`` row for the new
    chart, linked by ``pid``. Declared here, such rows are part of the save,
    not collateral, and a missing or extra one refutes the frame.
    """

    model_config = ConfigDict(extra="forbid")

    #: The surface the side rows are read from.
    surface: str
    #: The side row's field that names its record (``pid``).
    link_field: str
    #: The surface of the declared record, and the field the link names.
    of_surface: str
    of_field: str
    #: How many side rows each new declared record gets.
    count: int = Field(default=1, ge=1)


class AllowedTable(BaseModel):
    """One allow-listed table: the app writes it on its own."""

    model_config = ConfigDict(extra="forbid")

    table: str
    #: ``append_only`` (the default): rows may be added, existing rows must
    #: not change, and the table needs a generated key. ``skip``: not read at
    #: all, which needs a ``reason``.
    mode: Literal["append_only", "skip"] = "append_only"
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _skip_needs_reason(self) -> "AllowedTable":
        if self.mode == "skip" and not (self.reason or "").strip():
            raise ValueError(f"skipping table {self.table!r} needs a reason")
        return self


class FrameDatabase(BaseModel):
    """``frame.database``: diff the whole database, not only what is read.

    Every table and column is snapshotted before and after the save
    (``db_snapshot.py``), and each table is judged as one more slice: a row
    added, removed or changed is collateral unless a declared effect, a
    declared side record or an attributed concurrent writer explains it. A
    new declared or side row that sets a column the effects don't name, and
    that ``new_row_fields`` doesn't list, is collateral too.

    ``allow_tables`` replaces a read-list with a reviewed allow-list: the
    tables the app writes on its own for any signed-in user (logs, sessions,
    id registries). They are append-only unless an entry says ``skip`` with a
    reason. Everything else is compared. The receipt counts tables compared,
    append-only and skipped, and binds the list's digest.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: Literal["sqlite", "mysql"]
    #: The read-only connection: ``sqlite_database``, or ``sql_driver`` with
    #: ``sql_connect_args`` and ``sql_password_env``, as in a ``kind: sql``
    #: verifier (no query: every table is read).
    read: dict[str, Any]
    #: The app's own bookkeeping, reviewed per app: a table name
    #: (append-only) or ``{table, mode, reason}``.
    allow_tables: list[AllowedTable] = Field(default_factory=list)
    #: Table -> the surface whose records it holds (``patient_data`` ->
    #: ``patient``): an effect routed to the surface declares rows here too.
    views: dict[str, str] = Field(default_factory=dict)
    #: Table -> columns a new declared or side row may set beyond the fields
    #: the effects name (ids, creation dates, columns the app's form fills
    #: on its own). Any other column must hold its default.
    new_row_fields: dict[str, list[str]] = Field(default_factory=dict)
    #: Table -> columns that change on their own on every row (a last-seen
    #: time). ``frame.ignore_fields`` applies to surfaces only.
    ignore_columns: dict[str, list[str]] = Field(default_factory=dict)
    #: Over this many rows a table keeps, per row, its key and contents MACs
    #: and cell MACs only for the columns attribution and side records read.
    max_rows: int = Field(default=2000, ge=1)
    #: Schemas the read account must see (MySQL); the snapshot refuses when
    #: one isn't visible, so a schema the app writes can't be left out.
    schemas: list[str] = Field(default_factory=list)
    #: Seconds to wait after the save before the read, so writes the app
    #: makes a moment later (background jobs) fall inside the window.
    settle_s: float = Field(default=0.0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _names(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("allow_tables"), list):
            data = dict(data)
            data["allow_tables"] = [
                {"table": t} if isinstance(t, str) else t for t in data["allow_tables"]
            ]
        return data

    def modes(self) -> dict[str, str]:
        return {t.table: t.mode for t in self.allow_tables}

    def allow_list_sha256(self) -> str:
        payload = [t.model_dump(mode="json") for t in self.allow_tables]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def _connection(self) -> "FrameDatabase":
        if not (self.read.get("sqlite_database") or self.read.get("sql_driver")):
            raise ValueError("frame.database.read needs sqlite_database or sql_driver")
        if self.read.get("sql_query"):
            raise ValueError("frame.database.read reads every table; drop sql_query")
        return self


class FrameSpec(BaseModel):
    """``frame:`` in ``verify.yaml``: the slice that must otherwise not move."""

    model_config = ConfigDict(extra="forbid")

    #: Records are compared by this field. Unset: :func:`stable_id` (``id``
    #: when present, else the whole record).
    identity_field: Optional[str] = None
    #: Fields that change on their own on every record (a last-seen time).
    ignore_fields: list[str] = Field(default_factory=list)
    #: Fields of a pre-existing target record the save may change. Unset:
    #: the fields the effects name.
    target_fields: Optional[list[str]] = None
    attribution: Optional[FrameAttribution] = None
    #: Rows the save adds beside each new declared record (``FrameSideRecord``).
    side_records: list[FrameSideRecord] = Field(default_factory=list)
    #: Diff the whole database (``FrameDatabase``).
    database: Optional[FrameDatabase] = None

    @model_validator(mode="after")
    def _database_attribution(self) -> "FrameSpec":
        a = self.attribution
        if (
            self.database is not None
            and a is not None
            and a.kind == "audit_read"
            and not (a.write_events and a.table_field)
        ):
            raise ValueError(
                "with frame.database, audit attribution needs event_field, "
                "write_events and table_field: only a write to the same table "
                "attributes a change"
            )
        return self

    @model_validator(mode="after")
    def _identity_for_audit(self) -> "FrameSpec":
        if (
            self.attribution is not None
            and self.attribution.kind == "audit_read"
            and not self.identity_field
        ):
            raise ValueError(
                "audit_read attribution compares log entries with records by "
                "identity_field; set it"
            )
        attribution = self.attribution
        if attribution is not None and (
            attribution.record_key or attribution.record_keys
        ):
            for side in self.side_records:
                link = attribution.record_keys.get(side.surface, attribution.record_key)
                of = attribution.record_keys.get(
                    side.of_surface, attribution.record_key
                )
                if side.link_field != link or side.of_field != of:
                    # Otherwise a side row's record key and its link can name
                    # different records, and either can excuse the other's
                    # log entries.
                    raise ValueError(
                        f"with a record key, side record {side.surface!r} links "
                        f"by it: link_field {link!r}, of_field {of!r}"
                    )
        return self

    def digest(self) -> str:
        """SHA-256 of the frame as declared, for binding into evidence."""

        payload = self.model_dump(mode="json", by_alias=True)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class FrameChange:
    """One record outside the declared delta that moved."""

    surface: str
    change: ChangeKind
    attribution: Attribution
    #: Field names that changed (names only, never values).
    fields: list[str] = field(default_factory=list)
    #: True for a declared record that changed outside its target fields.
    on_target: bool = False
    #: True for a bot audit entry on an undeclared record the read shows as
    #: unchanged.
    audit_only: bool = False
    #: True when another actor changed a record this save declares.
    other_on_target: bool = False
    #: In memory only, for the operator's console; never put in a receipt.
    key: Any = None


@dataclass
class FrameVerdict:
    status: FrameStatus
    reason: str
    attribution: str = "none"
    records_compared: int = 0
    collateral: list[FrameChange] = field(default_factory=list)
    concurrent: list[FrameChange] = field(default_factory=list)
    #: ``frame.database`` only: tables compared, append-only and skipped,
    #: and the allow-list's digest.
    database: Optional[dict[str, Any]] = None
    #: In memory only: (surface, identity) of records another actor added,
    #: as their surface names them, when a table is judged in its place.
    added_by_others: set[tuple[str, str]] = field(default_factory=set)

    @property
    def held(self) -> bool:
        return self.status == "held"

    def evidence(self, spec: FrameSpec) -> dict[str, Any]:
        """Closed fields only: counts and field names, no records or actors."""

        return {
            "status": self.status,
            "frame_sha256": spec.digest(),
            "attribution": self.attribution,
            "records_compared": self.records_compared,
            "collateral_count": len(self.collateral),
            "concurrent_count": len(self.concurrent),
            "collateral_fields": sorted({f for c in self.collateral for f in c.fields}),
            "collateral_kinds": sorted({c.change for c in self.collateral}),
            "unattributed_count": sum(
                1 for c in self.collateral if c.attribution == "unattributed"
            ),
            "target_changed_by_other": sum(
                1 for c in self.collateral if c.other_on_target
            ),
            **({"database": self.database} if self.database is not None else {}),
        }


def _not_checked(reason: str, attribution: str = "none") -> FrameVerdict:
    return FrameVerdict(status="not_checked", reason=reason, attribution=attribution)


def _who(actor: Any) -> str:
    """An actor name as compared with the bot's own ids."""

    return str(actor).strip().casefold()


def get_path(record: Mapping[str, Any], path: str) -> Any:
    """``record[a][b]`` for ``path`` ``a.b``; None when any step is missing."""

    if path in record:
        return record[path]
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def declared_selectors(effect: Effect) -> list[dict[str, str]]:
    """The selectors naming the records ``effect`` may write.

    An ``exact_new_set``'s ``match`` is a scope, not a target, so its members
    are the declared records. An idempotency key narrows the selector.
    """

    def plain(selector: Mapping[str, Any]) -> dict[str, str]:
        out = {k: str(v) for k, v in selector.items()}
        if effect.idempotency_key is not None:
            out[effect.key_field] = str(effect.idempotency_key)
        return out

    if effect.kind is EffectKind.EXACT_NEW_SET:
        return [plain(s) for s in effect.new_records]
    return [plain(effect.match)]


def declared_fields(effects: Sequence[Effect]) -> set[str]:
    """Every field an effect names: selector keys and read-back fields."""

    names: set[str] = set()
    for effect in effects:
        for selector in declared_selectors(effect):
            names.update(selector)
        if effect.field:
            names.add(effect.field)
    return names


def _same(before: Any, after: Any) -> bool:
    from openadapt_flow.runtime.effects._common import _same_value

    return _same_value(before, after)


def _changed_fields(
    before: Mapping[str, Any], after: Mapping[str, Any], ignore: set[str]
) -> list[str]:
    return sorted(
        k
        for k in set(before) | set(after)
        if k not in ignore and not _same(before.get(k), after.get(k))
    )


def _without(record: Mapping[str, Any], ignore: set[str]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ignore}


def _resolve_self(
    spec: FrameAttribution, params: Mapping[str, object]
) -> Optional[set[str]]:
    out: set[str] = set()
    for expr in spec.self_actors:
        value = expr.resolve(params)
        if value is None or value == "":
            return None
        out.add(value)
    return out


def judge_frame(
    spec: FrameSpec,
    effects: Sequence[Effect],
    slices: Mapping[str, tuple[EffectState, Optional[list[dict[str, Any]]]]],
    *,
    params: Mapping[str, object] = {},
    routes: Optional[Mapping[str, Sequence[str]]] = None,
    audit_before: Optional[list[dict[str, Any]]] = None,
    audit_after: Optional[list[dict[str, Any]]] = None,
    run_actors: Sequence[str] = (),
    identities: Optional[Mapping[str, str]] = None,
    new_row_defaults: Optional[Mapping[str, Mapping[str, Optional[str]]]] = None,
    unread_changes: Sequence[tuple[str, str]] = (),
    hashers: Optional[Mapping[str, Callable[[str, Any], str]]] = None,
    clock: tuple[str, str] = ("", ""),
    database_evidence: Optional[dict[str, Any]] = None,
) -> FrameVerdict:
    """Judge the frame of ``effects`` over ``slices``.

    Args:
        spec: The declared frame.
        effects: The contract's effects, already bound to the run's params.
        slices: Surface name -> (baseline, records read after the save, or
            ``None`` when unreadable). One verifier is one slice named ``""``.
        params: Run params, to resolve the bot's own ids.
        routes: Effect index (as ``str``) -> the surface names it declares
            records in (its own surface and any view of the same read). Unset:
            every effect belongs to every slice.
        audit_before: ``audit_read`` entries read with the baseline.
        audit_after: ``audit_read`` entries read after the save.
        run_actors: More ids of this run (the executor's), never "another
            actor".
        identities: Slice -> its identity field, where it isn't
            ``spec.identity_field`` (a database table's row key).
        new_row_defaults: Slice -> column defaults, for slices whose new
            declared and side rows must set nothing undeclared (database
            tables). ``db_snapshot.ANY_VALUE`` marks the generated key and
            ``CLOCK_DEFAULT`` a column the database fills with the time.
        unread_changes: (table, why) for tables that moved in a way no row
            comparison explains. Each is collateral.
        hashers: Slice -> (column, value) -> the keyed MAC its baseline records hold instead of
            values (database tables). A post-save record is plain; it is
            hashed to compare with the baseline.
        clock: The database clock at the baseline and after the save.
        database_evidence: Counts for the receipt (tables compared, ...).
    """

    from openadapt_flow.runtime.effects.db_snapshot import KEY, ROW, is_default

    attribution = spec.attribution
    mode: str = attribution.kind if attribution is not None else "none"
    self_ids: set[str] = set()
    if attribution is not None:
        resolved = _resolve_self(attribution, params)
        if resolved is None:
            return _not_checked(
                "the frame's own-actor ids reference a parameter the run did "
                "not supply; changes can't be attributed",
                mode,
            )
        # Compared without case or surrounding space: a log that writes the
        # bot as "Bot" or "bot " still names the bot (fail closed).
        self_ids = {_who(a) for a in resolved} | {
            _who(a) for a in run_actors if str(a).strip()
        }

    hashers = hashers or {}
    database = spec.database
    surface_ignore = set(spec.ignore_fields) | {KEY}
    if attribution is not None and attribution.kind == "record_field":
        # Server-set bookkeeping moves on every write; it says who, not what.
        surface_ignore |= set(attribution.server_set)
    written = declared_fields(effects)
    may_change = set(spec.target_fields) if spec.target_fields is not None else written
    if attribution is not None and attribution.kind == "record_field":
        # A field this run writes (or any run parameter names) is not evidence
        # of who changed a record: refuse the attribution, fail closed.
        evidence = {attribution.actor_field} | (
            {attribution.version_field} if attribution.version_field else set()
        )
        if evidence & (written | may_change | set(params)):
            attribution = None
            mode = "attribution_refused"

    def ignore_for(name: str) -> set[str]:
        if name in hashers:
            # Database tables: only the columns declared for that table.
            cols = database.ignore_columns.get(name, []) if database else []
            return set(cols) | {KEY}
        return surface_ignore

    def identity_of(name: str) -> Optional[str]:
        if identities is not None and name in identities:
            return identities[name]
        return spec.identity_field

    def key_of(record: Mapping[str, Any], name: str = "") -> Any:
        identity = identity_of(name)
        if identity:
            value = record.get(identity)
            return None if value is None else str(value)
        return stable_id(_without(record, ignore_for(name)))

    def changed(
        name: str, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> list[str]:
        """Fields that differ. A database baseline row holds cell MACs (for
        all columns, or only the reference columns of a large table) and its
        contents MAC: the post-save value is MACed to compare, and a row
        whose contents moved in no compared column says so."""

        ignore = ignore_for(name)
        hasher = hashers.get(name)
        if hasher is None:
            return _changed_fields(before, after, ignore)
        fields = sorted(
            k
            for k in before
            if k not in ignore and k != ROW and before.get(k) != hasher(k, after.get(k))
        )
        if not fields and before.get(ROW) != after.get(ROW):
            fields = ["(columns not compared)"]
        return fields

    row_fields = database.new_row_fields if database is not None else {}

    def undeclared_columns(
        name: str, record: Mapping[str, Any], also: Sequence[str] = ()
    ) -> list[str]:
        """Columns a new row sets that nothing declares (database slices)."""

        defaults = (new_row_defaults or {}).get(name)
        if defaults is None:
            return []
        allowed = (
            written
            | set(row_fields.get(name, ()))
            | ignore_for(name)
            | {ROW}
            | set(also)
        )
        return sorted(
            c
            for c, v in record.items()
            if c not in allowed and not is_default(v, defaults.get(c), clock)
        )

    # The audit window, kept to write entries when the frame says which they
    # are: an entry for viewing a record never excuses a write to it.
    window_entries: Optional[list[dict[str, Any]]] = None
    if mode == "audit_read":
        if audit_before is not None and audit_after is not None:
            seen = {stable_id(e) for e in audit_before}
            window_entries = [e for e in audit_after if stable_id(e) not in seen]
            if attribution is not None and attribution.write_events:
                writes = set(attribution.write_events)
                window_entries = [
                    e
                    for e in window_entries
                    if str(get_path(e, attribution.event_field or "")) in writes
                ]

    surface_tables = {v: k for k, v in (database.views if database else {}).items()}

    def table_of(name: str) -> Optional[str]:
        return name if name in hashers else surface_tables.get(name)

    def event_kinds(change: Optional[str]) -> Optional[set[str]]:
        """The write events that can explain this kind of change: an insert
        for an added row, a delete for a removed one, an update otherwise.
        None: any write (the frame names no event kinds)."""

        if attribution is None or not attribution.write_events or change is None:
            return None
        inserts, deletes = (
            set(attribution.insert_events),
            set(attribution.delete_events),
        )
        if change == "added":
            return inserts
        if change == "removed":
            return deletes
        return set(attribution.write_events) - inserts - deletes

    def entries_for(
        name: str, ref: Any, field: Optional[str], kinds: Optional[set[str]] = None
    ) -> list[dict[str, Any]]:
        """Window entries that name this record (and its table, when the
        attribution says which table an entry wrote). ``field`` is set when
        ``ref`` is a baseline MAC of that column."""

        if window_entries is None or ref is None or attribution is None:
            return []
        hasher = hashers.get(name) if field else None
        table = table_of(name)
        out = []
        for e in window_entries:
            entry_ref = get_path(e, attribution.record_field or "")
            if entry_ref in (None, ""):
                continue
            if (
                hasher(field, entry_ref) if hasher and field else str(entry_ref)
            ) != str(ref):
                continue
            if attribution.table_field:
                if table is None or str(get_path(e, attribution.table_field)) != table:
                    continue
            if get_path(e, attribution.actor_field) in (None, ""):
                continue
            if kinds is not None and (
                str(get_path(e, attribution.event_field or "")) not in kinds
            ):
                continue
            out.append(e)
        return out

    def ref_of(
        name: str,
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> tuple[Any, Optional[str]]:
        """What an audit entry names for this record, taken from the record
        as it was BEFORE the change when there was one (so a write can't pick
        its own excuse by moving a row), and the column when that value is a
        baseline MAC."""

        if attribution is None:
            return key, None
        field = attribution.record_keys.get(name, attribution.record_key)
        if not field:
            return key, None
        record = before if before is not None else after
        value = None if record is None else record.get(field)
        hashed = name in hashers and before is not None
        return (None if value in (None, "") else str(value)), (
            field if hashed else None
        )

    def key_moved(
        name: str, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> bool:
        """Did the column an audit entry names change? Then no entry about
        either chart can explain the row."""

        if attribution is None or attribution.kind != "audit_read":
            return False
        field = attribution.record_keys.get(name, attribution.record_key)
        if not field or field not in before:
            return False
        hasher = hashers.get(name)
        if hasher is not None:
            return before.get(field) != hasher(field, after.get(field))
        return not _same(before.get(field), after.get(field))

    def actors(
        name: str, ref: Any, field: Optional[str], kinds: Optional[set[str]] = None
    ) -> set[str]:
        return {
            _who(get_path(e, attribution.actor_field))  # type: ignore[union-attr]
            for e in entries_for(name, ref, field, kinds)
        }

    def other_on_target(
        name: str,
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> bool:
        """Did another actor change this declared record in the window?"""

        if attribution is None:
            return False
        if attribution.kind == "audit_read":
            ref, field = ref_of(name, key, before, after)
            return bool(actors(name, ref, field) - self_ids)
        if after is None:
            return False
        if before is not None and not changed(name, before, after):
            return False  # untouched in the window
        actor = get_path(after, attribution.actor_field)
        return actor not in (None, "") and _who(actor) not in self_ids

    def bot_only_inserted(name: str, ref: Any) -> bool:
        """The bot's write entries for this record are inserts only, so a
        later write to it by another actor is theirs, not the bot's."""

        if attribution is None or not attribution.insert_events:
            return False
        inserts = set(attribution.insert_events)
        mine = [
            e
            for e in entries_for(name, ref, None)
            if _who(get_path(e, attribution.actor_field)) in self_ids
        ]
        return all(
            str(get_path(e, attribution.event_field or "")) in inserts for e in mine
        )

    def inserted_by_other(name: str, key: Any, record: Mapping[str, Any]) -> bool:
        """Does another actor's insert entry name this new record? (A look
        only: it doesn't use the entry up.)"""

        if attribution is None or attribution.kind != "audit_read":
            return False
        ref, field = ref_of(name, key, None, record)
        found = actors(name, ref, field, event_kinds("added"))
        return (
            bool(found)
            and not (found & self_ids)
            and not (actors(name, ref, field) & self_ids)
        )

    # Each other actor's entry explains at most one row: a colleague's one
    # insert can't cover two stray rows.
    used: set[int] = set()

    def attribute(
        name: str,
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> Attribution:
        if attribution is None:
            return "unattributed"
        if attribution.kind == "audit_read":
            ref, field = ref_of(name, key, before, after)
            if window_entries is None or ref is None:
                return "unattributed"
            if (
                before is not None
                and after is not None
                and key_moved(name, before, after)
            ):
                return "unattributed"  # the row moved between charts
            if actors(name, ref, field) & self_ids:
                return "self"
            change = (
                "added" if before is None else "removed" if after is None else "changed"
            )
            kinds = event_kinds(change)
            for e in entries_for(name, ref, field, kinds):
                if (
                    id(e) in used
                    or _who(get_path(e, attribution.actor_field)) in self_ids
                ):
                    continue
                if attribution.write_events:
                    used.add(id(e))
                return "other"
            return "unattributed"
        # record_field: the record says who last changed it.
        if after is None:
            return "unattributed"  # a removed record can't say who removed it
        actor = get_path(after, attribution.actor_field)
        if actor in (None, ""):
            return "unattributed"
        if _who(actor) in self_ids:
            return "self"
        if attribution.version_field:
            new_version = get_path(after, attribution.version_field)
            old_version = (
                None if before is None else get_path(before, attribution.version_field)
            )
            if new_version in (None, "") or (
                before is not None and _same(old_version, new_version)
            ):
                return "unattributed"
        elif before is not None and _same(
            get_path(before, attribution.actor_field), actor
        ):
            # Same last modifier as before the save: nothing says the other
            # actor made this change rather than an earlier one.
            return "unattributed"
        return "other"

    collateral: list[FrameChange] = []
    concurrent: list[FrameChange] = []
    added_by_others: set[tuple[str, str]] = set()
    views_of = dict(database.views) if database is not None else {}
    declared_keys: set[str] = set()
    #: What an audit entry names for each compared record (``ref_of``).
    refs: dict[tuple[str, str], tuple[Any, Optional[str]]] = {}
    compared = 0

    def note(change: FrameChange) -> None:
        if change.attribution == "other":
            concurrent.append(change)
        else:
            collateral.append(change)

    def selectors_for(name: str) -> list[Any]:
        mine = [
            effect
            for index, effect in enumerate(effects)
            if routes is None or name in routes.get(str(index), (name,))
        ]
        return [s for effect in mine for s in declared_selectors(effect)]

    def new_row(
        name: str, key: Any, after: Mapping[str, Any], also: Sequence[str] = ()
    ) -> None:
        """A new declared or side row may set only declared columns. Columns
        another actor set later, by a write to this table, while the bot only
        inserted the row, are that actor's (concurrent)."""

        extra = undeclared_columns(name, after, also)
        if not extra:
            return
        ref, _hashed = ref_of(name, key, None, after)
        others = actors(name, ref, None) - self_ids
        change = FrameChange(name, "added", "self", extra, on_target=True, key=key)
        if others and bot_only_inserted(name, ref):
            change.attribution = "other"
            concurrent.append(change)
        else:
            collateral.append(change)

    # Side records: the links each new declared record gives its side rows.
    # A new record another actor created gives its side rows the same way:
    # those rows are that actor's too (OpenEMR logs a contact row with no
    # patient, so only its link to the colleague's chart attributes it).
    side_links: list[set[str]] = []
    other_links: list[set[str]] = []
    side_found: list[dict[str, int]] = []
    #: Side rows seen per link of another actor's new record: at most
    #: ``count`` each are theirs, the rest are unexplained.
    other_found: list[dict[str, int]] = []
    for side in spec.side_records:
        links: set[str] = set()
        theirs: set[str] = set()
        before_state, after_records = slices.get(side.of_surface, (None, None))
        if before_state is not None and after_records is not None:
            sels = selectors_for(side.of_surface)
            old = {key_of(r, side.of_surface) for r in before_state.records}
            # A link an existing record already has is not a new record's: a
            # side row for it belongs to that record's chart.
            taken = {
                str(r.get(side.of_field))
                for r in before_state.records
                if r.get(side.of_field) not in (None, "")
            }
            of_hasher = hashers.get(side.of_surface)
            for record in after_records:
                if key_of(record, side.of_surface) in old:
                    continue
                value = record.get(side.of_field)
                if value in (None, ""):
                    continue
                if (
                    of_hasher(side.of_field, value) if of_hasher else str(value)
                ) in taken:
                    continue
                if any(record_matches(dict(record), s) for s in sels):
                    links.add(str(value))
                elif inserted_by_other(
                    side.of_surface, key_of(record, side.of_surface), record
                ):
                    theirs.add(str(value))
        side_links.append(links)
        other_links.append(theirs - links)
        side_found.append({link: 0 for link in links})
        other_found.append({link: 0 for link in theirs - links})

    for name, (before_state, after_records) in slices.items():
        if not before_state.reachable:
            return _not_checked(
                "no readable baseline, so what else changed can't be known", mode
            )
        if after_records is None:
            return _not_checked("the slice could not be read after the save", mode)
        if surface_tables.get(name) in hashers:
            # A surface whose table the frame snapshots: the table, with its
            # columns, defaults and write attribution, judges these records.
            continue
        selectors = selectors_for(name)
        hasher = hashers.get(name)

        def declared(record: Mapping[str, Any], hashed: bool = False) -> bool:
            if hashed and hasher is not None:
                return any(
                    all(record.get(k) == hasher(k, v) for k, v in s.items())
                    for s in selectors
                )
            return any(record_matches(dict(record), s) for s in selectors)

        before_map: dict[Any, dict[str, Any]] = {}
        after_map: dict[Any, dict[str, Any]] = {}
        for label, records, into in (
            ("baseline", before_state.records, before_map),
            ("post-save", after_records, after_map),
        ):
            for record in records:
                key = key_of(record, name)
                if key is None:
                    return _not_checked(
                        f"a {label} record carries no {identity_of(name)!r}; "
                        "the slice can't be compared record by record",
                        mode,
                    )
                if key in into and identity_of(name):
                    return _not_checked(
                        f"two {label} records share one {identity_of(name)!r}; "
                        "the slice can't be compared record by record",
                        mode,
                    )
                into[key] = dict(record)
        compared += len(set(before_map) | set(after_map))

        for key in set(before_map) | set(after_map):
            before = before_map.get(key)
            after = after_map.get(key)
            is_declared = (
                before is not None and declared(before, hashed=hasher is not None)
            ) or (after is not None and declared(after))
            refs[(name, str(key))] = ref_of(name, key, before, after)
            if is_declared:
                ref = refs[(name, str(key))][0]
                if ref is None:
                    return _not_checked(
                        "a declared record carries no record_key value, so its "
                        "log entries can't be matched",
                        mode,
                    )
                declared_keys.add(str(ref))
                if before is None and after is not None:
                    # A new declared record. In a database table, with write
                    # and insert events to tell actors apart, anything it sets
                    # beyond the declared fields must be its default or
                    # another actor's later write. Elsewhere nothing says
                    # which fields another actor touched: their entry blocks.
                    separable = (
                        name in hashers
                        and attribution is not None
                        and bool(attribution.insert_events)
                    )
                    if not separable and other_on_target(name, key, None, after):
                        collateral.append(
                            FrameChange(
                                name,
                                "changed",
                                "other",
                                [],
                                on_target=True,
                                other_on_target=True,
                                key=key,
                            )
                        )
                        continue
                    new_row(name, key, after)
                    continue
                if after is None:
                    continue  # a removed target: the delta check's case
                fields = changed(name, before, after)  # type: ignore[arg-type]
                stray = [f for f in fields if f not in may_change]
                if other_on_target(name, key, before, after):
                    ref, field = ref_of(name, key, before, after)
                    if (
                        fields
                        and not (set(fields) & may_change)
                        and attribution is not None
                        and attribution.write_events
                        and attribution.table_field
                        and not (actors(name, ref, field) & self_ids)
                    ):
                        # Another actor wrote, by a write to this table, only
                        # fields this save doesn't declare, and the bot wrote
                        # nothing to this record: theirs. A declared field
                        # moved by another actor never passes (the saved
                        # value may not stand), and nor does a record both
                        # wrote, since nothing says whose change is whose.
                        concurrent.append(
                            FrameChange(name, "changed", "other", fields, key=key)
                        )
                        continue
                    collateral.append(
                        FrameChange(
                            name,
                            "changed",
                            "other",
                            fields,
                            on_target=True,
                            other_on_target=True,
                            key=key,
                        )
                    )
                    continue
                if stray:
                    note(
                        FrameChange(
                            name,
                            "changed",
                            attribute(name, key, before, after),
                            stray,
                            on_target=True,
                            key=key,
                        )
                    )
                continue
            if before is None and after is not None:
                side_index = next(
                    (
                        i
                        for i, side in enumerate(spec.side_records)
                        if side.surface == name
                        and str(after.get(side.link_field)) in side_links[i]
                    ),
                    None,
                )
                if side_index is not None:
                    # A side record of a new declared record: part of the save.
                    side = spec.side_records[side_index]
                    link = str(after.get(side.link_field))
                    side_found[side_index][link] += 1
                    side_ref = ref_of(name, key, None, after)[0]
                    if side_ref is not None:
                        declared_keys.add(str(side_ref))
                    new_row(name, key, after, (side.link_field,))
                    continue
                theirs_side = False
                extra_side = False
                for i, side in enumerate(spec.side_records):
                    other_link = str(after.get(side.link_field))
                    if side.surface == name and other_link in other_links[i]:
                        other_found[i][other_link] += 1
                        # Beyond the count, a side row of their chart is no
                        # more theirs than anyone's: unexplained.
                        theirs_side = other_found[i][other_link] <= side.count
                        extra_side = not theirs_side
                        break
                change = FrameChange(
                    name,
                    "added",
                    "other"
                    if theirs_side
                    else "unattributed"
                    if extra_side
                    else attribute(name, key, None, after),
                    sorted(_without(after, ignore_for(name))),
                    key=key,
                )
                note(change)
                view = views_of.get(name)
                if (
                    change.attribution == "other"
                    and view is not None
                    and spec.identity_field
                ):
                    # The same record as its surface names it, for checks
                    # that read the surface (an exact_new_set's additions).
                    added_by_others.add((view, str(after.get(spec.identity_field))))
            elif after is None and before is not None:
                note(
                    FrameChange(
                        name, "removed", attribute(name, key, before, None), key=key
                    )
                )
            elif before is not None and after is not None:
                fields = changed(name, before, after)
                if fields:
                    note(
                        FrameChange(
                            name,
                            "changed",
                            attribute(name, key, before, after),
                            fields,
                            key=key,
                        )
                    )

    for table, why in unread_changes:
        # Moved in a way no row comparison explains: nothing can say which
        # rows, or who moved them.
        collateral.append(FrameChange(table, "changed", "unattributed", [why]))

    for i, side in enumerate(spec.side_records):
        for link, found in side_found[i].items():
            if found != side.count:
                # Missing (or doubled) side record: the chart is not whole.
                collateral.append(
                    FrameChange(
                        side.surface,
                        "removed" if found < side.count else "added",
                        "unattributed",
                        [f"{found} of {side.count} side record(s)"],
                        key=link,
                    )
                )

    if mode == "audit_read":
        if window_entries is None:
            # The log could not be read. Every change stays unattributed, and
            # the bot's own log entries can't be checked: say so.
            mode = "audit_read_unavailable"
        else:
            # What the moved records' entries name: their ``ref_of`` value
            # (a record_key, a baseline MAC of one, or the row key), never
            # the row key when the entry names another field.
            # Only records that already refute: another user's concurrent
            # change to a patient never excuses the bot's own entry for it.
            moved_refs = [
                (c.surface, *refs[(c.surface, str(c.key))])
                for c in collateral
                if (c.surface, str(c.key)) in refs
            ]

            def names_moved(entry_ref: Any) -> bool:
                for surface, value, column in moved_refs:
                    if value is None:
                        continue
                    hasher = hashers.get(surface) if column else None
                    text = (
                        hasher(column, entry_ref)
                        if hasher and column
                        else str(entry_ref)
                    )
                    if text == str(value):
                        return True
                return False

            logged: set[str] = set()
            for entry in window_entries:
                actor = get_path(entry, attribution.actor_field)  # type: ignore[union-attr]
                ref = get_path(entry, attribution.record_field or "")  # type: ignore[union-attr]
                if actor is None or _who(actor) not in self_ids:
                    continue
                if str(ref) in declared_keys or str(ref) in logged or names_moved(ref):
                    continue
                logged.add(str(ref))
                collateral.append(
                    FrameChange("", "changed", "self", audit_only=True, key=ref)
                )

    verdict = FrameVerdict(
        status="refuted" if collateral else "held",
        reason="",
        attribution=mode,
        records_compared=compared,
        collateral=collateral,
        concurrent=concurrent,
        database=database_evidence,
        added_by_others=added_by_others,
    )
    verdict.reason = _reason(verdict)
    return verdict


def _reason(verdict: FrameVerdict) -> str:
    parts: list[str] = []
    if verdict.collateral:
        by_kind: dict[str, int] = {}
        for change in verdict.collateral:
            label = (
                "written by this run per the audit log"
                if change.audit_only
                else "saved record changed by another actor (the saved value may "
                "not stand)"
                if change.other_on_target
                else f"{change.change} outside the declared fields of a target"
                if change.on_target
                else change.change
            )
            by_kind[label] = by_kind.get(label, 0) + 1
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
        fields = sorted({f for c in verdict.collateral for f in c.fields})
        unattributed = sum(
            1 for c in verdict.collateral if c.attribution == "unattributed"
        )
        parts.append(
            f"{len(verdict.collateral)} record(s) outside the declared delta "
            f"moved ({kinds}"
            + (
                f"; field(s): {', '.join(fields[:_SAMPLE_LIMIT])}"
                f"{'' if len(fields) <= _SAMPLE_LIMIT else ', ...'}"
                if fields
                else ""
            )
            + f"); {unattributed} of them no actor explains -- collateral, a "
            "person should reconcile before anything is retried or undone"
        )
    else:
        parts.append(
            f"nothing outside the declared delta changed across "
            f"{verdict.records_compared} record(s)"
        )
    if verdict.concurrent:
        parts.append(
            f"{len(verdict.concurrent)} change(s) attributed to another actor "
            "(concurrent edits, not counted against this save)"
        )
    if verdict.attribution == "attribution_refused":
        parts.append(
            "attribution refused: it names a field this run writes, which is no "
            "evidence of who changed a record"
        )
    if verdict.attribution == "audit_read_unavailable":
        parts.append("the audit log could not be read, so no change was attributed")
    return "; ".join(parts)
