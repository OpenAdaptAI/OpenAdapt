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

The judge reads nothing itself; callers pass the snapshots. Fail-safe: a
missing baseline, an unreadable read, a record without its identity field or
an unresolved self id makes the frame ``not_checked``, never ``held``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
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
        }


def _not_checked(reason: str, attribution: str = "none") -> FrameVerdict:
    return FrameVerdict(status="not_checked", reason=reason, attribution=attribution)


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
    """

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
        self_ids = resolved | {str(a) for a in run_actors if str(a)}

    ignore = set(spec.ignore_fields)
    if attribution is not None and attribution.kind == "record_field":
        # Server-set bookkeeping moves on every write; it says who, not what.
        ignore |= set(attribution.server_set)
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

    def key_of(record: Mapping[str, Any]) -> Any:
        if spec.identity_field:
            value = record.get(spec.identity_field)
            return None if value is None else str(value)
        return stable_id(_without(record, ignore))

    window_entries: Optional[list[dict[str, Any]]] = None
    if mode == "audit_read":
        if audit_before is not None and audit_after is not None:
            seen = {stable_id(e) for e in audit_before}
            window_entries = [e for e in audit_after if stable_id(e) not in seen]

    def ref_of(
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> Any:
        """What an audit entry names for this record (``record_key``)."""

        if attribution is None or not attribution.record_key:
            return key
        values = {
            None if v in (None, "") else str(v)
            for v in (
                r.get(attribution.record_key) for r in (before, after) if r is not None
            )
        }
        # A record moved to another ``record_key`` (a note refiled to another
        # patient) names neither: no entry can explain it.
        return values.pop() if len(values) == 1 else None

    def other_on_target(
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> bool:
        """Did another actor change this declared record in the window?"""

        if attribution is None:
            return False
        key = ref_of(key, before, after)
        if attribution.kind == "audit_read":
            return window_entries is not None and any(
                str(get_path(e, attribution.record_field or "")) == str(key)
                and get_path(e, attribution.actor_field) not in (None, "")
                and str(get_path(e, attribution.actor_field)) not in self_ids
                for e in window_entries
            )
        if after is None:
            return False
        if before is not None and not _changed_fields(before, after, ignore):
            return False  # untouched in the window
        actor = get_path(after, attribution.actor_field)
        return actor not in (None, "") and str(actor) not in self_ids

    def attribute(
        key: Any,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> Attribution:
        if attribution is None:
            return "unattributed"
        if attribution.kind == "audit_read":
            key = ref_of(key, before, after)
            if window_entries is None or key is None:
                return "unattributed"
            actors = {
                str(get_path(e, attribution.actor_field))
                for e in window_entries
                if str(get_path(e, attribution.record_field or "")) == str(key)
                and get_path(e, attribution.actor_field) not in (None, "")
            }
            if actors & self_ids:
                return "self"
            return "other" if actors else "unattributed"
        # record_field: the record says who last changed it.
        if after is None:
            return "unattributed"  # a removed record can't say who removed it
        actor = get_path(after, attribution.actor_field)
        if actor in (None, ""):
            return "unattributed"
        if str(actor) in self_ids:
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
    declared_keys: set[str] = set()
    #: ``record_key`` only: what an audit entry names for each compared record.
    refs: dict[tuple[str, str], Optional[str]] = {}
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

    # Side records: the links each new declared record gives its side rows.
    side_links: list[set[str]] = []
    side_found: list[dict[str, int]] = []
    #: Each link's parent ``ref_of`` value: a side row must name the same.
    side_parent: list[dict[str, Any]] = []
    for side in spec.side_records:
        links: set[str] = set()
        parents: dict[str, Any] = {}
        before_state, after_records = slices.get(side.of_surface, (None, None))
        if before_state is not None and after_records is not None:
            sels = selectors_for(side.of_surface)
            old = {key_of(r) for r in before_state.records}
            # A link an existing record already has is not a new record's: a
            # side row for it belongs to that record's chart.
            taken = {
                str(r.get(side.of_field))
                for r in before_state.records
                if r.get(side.of_field) not in (None, "")
            }
            for record in after_records:
                if key_of(record) in old:
                    continue
                if any(record_matches(dict(record), s) for s in sels):
                    value = record.get(side.of_field)
                    if value not in (None, "") and str(value) not in taken:
                        links.add(str(value))
                        parents[str(value)] = ref_of(key_of(record), None, record)
        side_links.append(links)
        side_parent.append(parents)
        side_found.append({link: 0 for link in links})

    for name, (before_state, after_records) in slices.items():
        if not before_state.reachable:
            return _not_checked(
                "no readable baseline, so what else changed can't be known", mode
            )
        if after_records is None:
            return _not_checked("the slice could not be read after the save", mode)
        mine = [
            effect
            for index, effect in enumerate(effects)
            if routes is None or name in routes.get(str(index), (name,))
        ]
        selectors = [s for effect in mine for s in declared_selectors(effect)]

        def declared(record: Mapping[str, Any]) -> bool:
            return any(record_matches(dict(record), s) for s in selectors)

        before_map: dict[Any, dict[str, Any]] = {}
        after_map: dict[Any, dict[str, Any]] = {}
        for label, records, into in (
            ("baseline", before_state.records, before_map),
            ("post-save", after_records, after_map),
        ):
            for record in records:
                key = key_of(record)
                if key is None:
                    return _not_checked(
                        f"a {label} record carries no {spec.identity_field!r}; "
                        "the slice can't be compared record by record",
                        mode,
                    )
                if key in into and spec.identity_field:
                    return _not_checked(
                        f"two {label} records share one {spec.identity_field!r}; "
                        "the slice can't be compared record by record",
                        mode,
                    )
                into[key] = dict(record)
        compared += len(set(before_map) | set(after_map))

        for key in set(before_map) | set(after_map):
            before = before_map.get(key)
            after = after_map.get(key)
            is_declared = (before is not None and declared(before)) or (
                after is not None and declared(after)
            )
            ref = ref_of(key, before, after)
            refs[(name, str(key))] = None if ref is None else str(ref)
            if is_declared:
                if ref is None:
                    return _not_checked(
                        "a declared record carries no record_key value (or it "
                        "changed), so its log entries can't be matched",
                        mode,
                    )
                declared_keys.add(str(ref))
                if other_on_target(key, before, after):
                    # The saved value may not stand: never excused.
                    collateral.append(
                        FrameChange(
                            name,
                            "changed",
                            "other",
                            _changed_fields(before or {}, after or {}, ignore),
                            on_target=True,
                            other_on_target=True,
                            key=key,
                        )
                    )
                    continue
                if before is None or after is None:
                    continue  # a new or removed target: the delta check's case
                stray = [
                    f
                    for f in _changed_fields(before, after, ignore)
                    if f not in may_change
                ]
                if stray:
                    note(
                        FrameChange(
                            name,
                            "changed",
                            attribute(key, before, after),
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
                        # Filed under its parent's record_key, not another's.
                        and (
                            attribution is None
                            or not attribution.record_key
                            or ref == side_parent[i][str(after.get(side.link_field))]
                        )
                    ),
                    None,
                )
                if side_index is not None:
                    # A side record of a new declared record: part of the save.
                    link = str(after.get(spec.side_records[side_index].link_field))
                    side_found[side_index][link] += 1
                    if ref is not None:
                        declared_keys.add(str(ref))
                    continue
                note(
                    FrameChange(
                        name,
                        "added",
                        attribute(key, None, after),
                        sorted(_without(after, ignore)),
                        key=key,
                    )
                )
            elif after is None and before is not None:
                note(
                    FrameChange(name, "removed", attribute(key, before, None), key=key)
                )
            elif before is not None and after is not None:
                fields = _changed_fields(before, after, ignore)
                if fields:
                    note(
                        FrameChange(
                            name,
                            "changed",
                            attribute(key, before, after),
                            fields,
                            key=key,
                        )
                    )

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
            if attribution.record_key:  # type: ignore[union-attr]
                # Compare like with like: entries name ``record_key`` values.
                moved = {
                    r
                    for c in collateral + concurrent
                    if (r := refs.get((c.surface, str(c.key)))) is not None
                }
            else:
                moved = {str(c.key) for c in collateral + concurrent}
            for entry in window_entries:
                actor = get_path(entry, attribution.actor_field)  # type: ignore[union-attr]
                ref = get_path(entry, attribution.record_field or "")  # type: ignore[union-attr]
                if actor is None or str(actor) not in self_ids:
                    continue
                if str(ref) in declared_keys or str(ref) in moved:
                    continue
                moved.add(str(ref))
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
