"""Check an "ensure this record exists" write through a FHIR read path only.

The task: make sure one person (or other entity) has a record. If one is
already there, report it and write nothing; otherwise create exactly one,
with the given values. The agent's report is a claim. This module reads the
server before and after and gives one of three outcomes:

- ``found_existing``: exactly one record was already the person, it is the
  reported id, and the agent wrote nothing anywhere;
- ``created_verified``: no record was the person, the agent created exactly
  one, it holds every value, the agent wrote nothing else, nobody else
  changed it, and it is now the only record that is the person;
- ``needs_review``: anything else, with the reasons.

It reads only what a FHIR server offers a client: searches, ``_history`` and
``AuditEvent``, never the database. What that read path can see, and what it
can't, is stated in ``docs/ENSURE_RECORD.md``:

- **Who wrote what in the window** comes from the server's audit trail
  (``AuditEvent`` searched by date). Each written resource's ``_history``
  gives its versions in the window and their ``meta.author``, which the
  server sets. The audit and the history must name the same actor, or the
  change is unattributed.
- **A sweep** (optional) searches the declared resource types by
  ``_lastUpdated``, so a create or update the audit trail missed still shows.
  A sweep can't see a delete.
- A change no source attributes counts as the agent's own: a change nobody
  can explain is never assumed to be someone else's.

Fail-closed: an unreadable read, a page past the limit, a next link to
another origin, a resource with no id or a server time that can't be read
gives ``needs_review`` with ``unreadable``, never an outcome that proves
something.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openadapt_flow.fhir_reference import (
    ReferenceEvidence,
    ReferenceReader,
    ReferenceSchema,
    ReferenceUnreadable,
)

Outcome = Literal["found_existing", "created_verified", "needs_review"]
Compare = Literal["exact", "casefold", "digits", "date", "instant"]

#: A search longer than this many pages reads as unreadable.
MAX_PAGES = 50
#: FHIR AuditEvent ``action`` codes that write (create, update, delete).
WRITE_ACTIONS = ("C", "U", "D")
#: AuditEvent action codes: create, read, update, delete, execute. An
#: execute (an operation, such as starting a Bot) may write, so it counts as
#: a write; a missing or unknown code makes the window unreadable.
AUDIT_ACTIONS = ("C", "R", "U", "D", "E")


class EnsureRecordError(ValueError):
    """The contract can't be loaded or bound (never a failed check)."""


class Unreadable(RuntimeError):
    """A read the judgment needs did not give a complete answer."""


# -- the contract ----------------------------------------------------------------


class FieldRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Dotted path into the resource; a segment may filter a list
    #: (``telecom[system=email].value``), as in ``effects/fhir.py``.
    path: str = Field(min_length=1)
    compare: Compare = "exact"
    #: Printed form -> stored form (``Massachusetts: MA``).
    map: dict[str, str] = Field(default_factory=dict)


class AuditRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str = "AuditEvent"
    #: The search parameter that takes the event time (``date`` in R4).
    since_param: str = "date"
    actor_path: str = "agent.who.reference"
    entity_path: str = "entity.what.reference"
    action_path: str = "action"
    #: An empty window proves nothing when the server isn't logging (Medplum
    #: answers an empty search, not an error, with ``saveAuditEvents`` off).
    #: With ``heartbeat``, a found record counts only when the verifier's own
    #: read of it shows up in the trail; set it where reads by id are logged.
    heartbeat: bool = True
    #: When the event happened; it orders the agent's writes and other
    #: actors' reads of the same record (``recorded`` in R4).
    time_path: str = "recorded"


class ChangeFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit: Optional[AuditRead] = Field(default_factory=AuditRead)
    #: Read each written resource's ``_history`` for its window versions.
    history: bool = True
    #: Whether the server returns ``meta.author`` on each version, a second
    #: witness to who wrote it. Medplum 5.2 doesn't, so its contract sets
    #: false and attribution rests on the audit trail alone.
    history_author: bool = True
    #: Resource types searched by ``_lastUpdated`` as a second source.
    sweep_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _some_source(self) -> "ChangeFeed":
        if self.audit is None and not self.sweep_types:
            raise ValueError("the change feed needs an audit read or a sweep")
        return self


Between = Literal["person", "consistent", "either"]


class CorrectionOp(BaseModel):
    """The agent's own updates of the record it just created (#1655)."""

    model_config = ConfigDict(extra="forbid")

    #: With ``accept_corrections``: both must be true for a correction to count.
    accept: bool = True
    #: What every version of the record in the window must be. ``person``:
    #: the person, by the identity rules. ``consistent``: it holds only the
    #: task's values, any of them possibly still missing (a form that saves a
    #: new record in steps), and nothing else. A version that held a value
    #: the task didn't give, such as another person's birth date, is refused
    #: either way: a search no audit event names may have shown it.
    #: ``either``: the person, or ``consistent``; never stricter than
    #: ``person`` (#1655's rule, which accepts a corrected non-identity
    #: value), and also accepting a record saved in steps.
    between: Between = "person"


class CardinalityOp(BaseModel):
    """How many records of the contract's type the agent may leave created."""

    model_config = ConfigDict(extra="forbid")

    #: A record of the contract's type that the agent created and deleted
    #: again in the window (a double submit it cleaned up) counts as not
    #: created, when every version of it held only the task's values (as
    #: ``consistent``), every write of it is the agent's and logged, and no
    #: other actor read it by id. Off by default: any second create refuses.
    own_transient: bool = False


class BookkeepingRule(BaseModel):
    """Elements the server or the app keeps on another record of one type."""

    model_config = ConfigDict(extra="forbid")

    #: Never the contract's own type: another record of that type may be
    #: the person, and its identity elements are never bookkeeping.
    resource_type: str = Field(min_length=1)
    #: Only a version counter, a time stamp or one extension named by its
    #: exact url: ``meta.versionId``, ``meta.lastUpdated``,
    #: ``extension[url=<url>]``. Any other element (``meta.security``,
    #: ``meta.account``, ``active``, ``link``, a bare ``extension``) can hide a
    #: record, move it or change a clinical fact, so it can't be named.
    paths: list[str] = Field(min_length=1)
    #: A temporal rule, as the whole-database generator's bookkeeping answer
    #: has it (``now_checks``): every time stamp these elements change to
    #: must lie within ``now_within_s`` of the version's own server-set
    #: ``meta.lastUpdated``. A planted or replayed time refuses.
    now_checks: bool = False
    now_within_s: float = Field(default=60.0, gt=0, le=3600)


class Operations(BaseModel):
    """``operations:``: what one write may also do, as data.

    Every default keeps the rule as it was, so a contract without this
    section judges exactly as before. Nothing here excuses a change on the
    record the task writes, or on any record that is the person: their
    every version is judged.
    """

    model_config = ConfigDict(extra="forbid")

    correction: CorrectionOp = Field(default_factory=CorrectionOp)
    cardinality: CardinalityOp = Field(default_factory=CardinalityOp)
    #: Updates by the agent's actor to *other* records that change only these
    #: elements (a version counter, a timestamp, an app's own extension) are
    #: the save's bookkeeping, not a write. Each rule names one resource type
    #: and exact element paths: no wildcard, no whole type.
    bookkeeping: list[BookkeepingRule] = Field(default_factory=list)


#: The only element paths a bookkeeping rule may name.
_BOOK_META = ("meta.versionId", "meta.lastUpdated")
_BOOK_EXTENSION = re.compile(r"extension\[url=([^\[\]\s]+)\]")


class EnsureContract(BaseModel):
    """``openadapt.ensure-record/v1``: what one ensure-record write must do."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["openadapt.ensure-record/v1"] = "openadapt.ensure-record/v1"
    resource_type: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    access_token_env: Optional[str] = None
    #: Exact schema metadata proving which elements are FHIR References.
    reference_schema: Optional[ReferenceSchema] = None
    #: The agent's own actor references (``Practitioner/<id>``).
    self_actors: list[str] = Field(alias="self", min_length=1)
    #: Task parameter -> where and how the record stores it.
    fields: dict[str, FieldRule] = Field(min_length=1)
    #: Each entry is one way to be the same person: every parameter equal.
    identity: list[list[str]] = Field(min_length=1)
    #: Task parameter -> FHIR search parameter, to find candidates.
    search: dict[str, str] = Field(min_length=1)
    change_feed: ChangeFeed = Field(default_factory=ChangeFeed)
    #: Wait this long after the write before reading, for the audit trail.
    #: The created record may hold only what ``fields`` describe, one value
    #: each: an element the task didn't give (a second given name, a fax
    #: number, a district) is a wrong field. ``allow`` names extra leaf paths
    #: a correct record may carry, alone (``address.use``) or with the one
    #: value allowed, as JSON (``active=true``, ``address.use="home"``).
    #: ``id``, ``meta`` and ``resourceType`` always are.
    closed: bool = False
    allow: list[str] = Field(default_factory=list)
    #: A record holding any of these values asserts nothing (an
    #: Immunization with ``status="entered-in-error"``): it is never the
    #: person, so it is neither found nor a duplicate, and a created record
    #: that holds one is wrong. Each is a path and its one value, as JSON, in
    #: ``allow``'s form. It names values to leave out, never values to keep:
    #: a value the list misses still matches, which stops for review instead
    #: of missing a duplicate.
    void: list[str] = Field(default_factory=list)
    #: The audit trail is written a little after the save (Medplum: up to
    #: about 0.1 s). The change feed is read until two reads ``settle_s``
    #: apart agree, at most ``stable_reads`` times; a feed still changing
    #: then reads as unreadable.
    settle_s: float = Field(default=2.0, ge=0, le=120)
    stable_reads: int = Field(default=3, ge=1, le=10)
    timeout_s: float = Field(default=15.0, gt=0, le=300)
    #: A create followed by the agent's own updates of the same record counts
    #: as one write when nothing else moved, every write is the agent's, and
    #: the audit trail shows no other actor read the record between them (see
    #: ``correction_refused``). The result says it was corrected. False sends
    #: any change after the create to review.
    accept_corrections: bool = True
    #: Correction, cardinality and bookkeeping rules (:class:`Operations`).
    operations: Operations = Field(default_factory=Operations)

    @model_validator(mode="after")
    def _bound(self) -> "EnsureContract":
        if self.reference_schema is not None:
            if self.reference_schema.definition["type"] != self.resource_type:
                raise ValueError("the Reference schema describes another resource type")
            paths = self.reference_schema.paths()
            for name, field in self.fields.items():
                plain = _bare(field.path)
                if plain in paths or plain.removesuffix(".reference") in paths:
                    if field.compare != "exact" or field.map:
                        raise ValueError(
                            "Reference identity cannot use scalar normalization"
                        )
                if (
                    plain.endswith(".display")
                    and plain.removesuffix(".display") in paths
                ):
                    parent = field.path.removesuffix(".display")
                    if not any(
                        f.path in (parent, parent + ".reference")
                        for f in self.fields.values()
                    ):
                        raise ValueError(
                            f"{name}: a Reference display needs a bound target identity"
                        )
        for rule in self.identity:
            if not rule:
                raise ValueError("an identity rule names no parameter")
            if any(
                p in self.fields and reference_display(self, self.fields[p].path)
                for p in rule
            ):
                raise ValueError("Reference display cannot identify a record")
            missing = [p for p in rule if p not in self.fields]
            if missing:
                raise ValueError(f"identity names unknown fields: {missing}")
            if not any(p in self.search for p in rule):
                raise ValueError(f"identity rule {rule} has no search parameter")
        identity_paths = {
            _bare(self.fields[p].path) for rule in self.identity for p in rule
        }
        for entry in self.void:
            path, value = _void_entry(entry)
            if _bare(path) in identity_paths:
                raise ValueError(f"void entry {entry!r} names an identity field")
        for book in self.operations.bookkeeping:
            if book.resource_type == self.resource_type:
                raise ValueError(
                    f"a bookkeeping rule can't name the contract's own type "
                    f"{book.resource_type}: another record of it may be the person"
                )
            for path in book.paths:
                if path not in _BOOK_META and not _BOOK_EXTENSION.fullmatch(path):
                    raise ValueError(
                        f"bookkeeping path {path!r} can't be excused: only "
                        "meta.versionId, meta.lastUpdated and extension[url=...]"
                    )
        return self

    @model_serializer(mode="wrap")
    def _serialize_reference_schema(self, handler: Any) -> Any:
        data = handler(self)
        if self.reference_schema is None:
            data.pop("reference_schema", None)
        return data

    @property
    def corrections(self) -> bool:
        """Whether a correction may count: both switches must say so."""

        return self.accept_corrections and self.operations.correction.accept


class AppSemantics(BaseModel):
    """``openadapt.app-semantics/v1``: one application's operation rules.

    Written once per application and version, and used by every task's
    contract on it: what one save may also do, per resource type, and the
    bookkeeping the app keeps on other records. A contract pins it by path
    and version (``semantics: {path, app, version}``); a spec for a real
    customer system is private (``source-policy.yaml``), the format public.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.app-semantics/v1"]
    app: str = Field(min_length=1)
    version: int = Field(ge=1)
    #: Resource type -> its correction and cardinality rules.
    resources: dict[str, Operations] = Field(default_factory=dict)
    bookkeeping: list[BookkeepingRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _flat(self) -> "AppSemantics":
        for rtype, ops in self.resources.items():
            if ops.bookkeeping:
                raise ValueError(f"{rtype}: bookkeeping goes in the spec's own list")
        return self

    def operations_for(self, resource_type: str) -> Operations:
        """A contract's operations: its type's rules, and the app's
        bookkeeping on every *other* type."""

        ops = self.resources.get(resource_type, Operations())
        book = [b for b in self.bookkeeping if b.resource_type != resource_type]
        return ops.model_copy(update={"bookkeeping": book})


def load_semantics(path: str | Path) -> AppSemantics:
    import yaml

    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        return AppSemantics.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - any bad spec is a load error
        raise EnsureRecordError(f"invalid semantics {source}: {exc}") from exc


def _apply_semantics(data: dict[str, Any], source: Path) -> dict[str, Any]:
    """Resolve ``semantics: {path, app, version, sha256}`` into ``operations``.

    The pin names the spec's exact bytes: a spec edited in place, without a
    new version and a new pin, doesn't load. The path is relative to the
    contract; an absolute one is refused.
    """

    import hashlib

    pin = data.pop("semantics", None)
    if pin is None:
        return data
    if "operations" in data:
        raise EnsureRecordError(f"{source}: give operations or semantics, not both")
    if not isinstance(pin, dict) or set(pin) != {"path", "app", "version", "sha256"}:
        raise EnsureRecordError(
            f"{source}: semantics needs path, app, version and sha256"
        )
    rel = Path(str(pin["path"]))
    if rel.is_absolute():
        raise EnsureRecordError(f"{source}: the semantics path must be relative")
    where = (source.parent / rel).resolve()
    try:
        digest = hashlib.sha256(where.read_bytes()).hexdigest()
    except OSError as exc:
        raise EnsureRecordError(f"cannot read semantics {where}: {exc}") from exc
    if digest != str(pin["sha256"]):
        raise EnsureRecordError(
            f"{source}: semantics {rel} is not the pinned file (sha256 {digest})"
        )
    spec = load_semantics(where)
    if (spec.app, spec.version) != (pin["app"], pin["version"]):
        raise EnsureRecordError(
            f"{source}: semantics pins {pin['app']} v{pin['version']}, "
            f"the file is {spec.app} v{spec.version}"
        )
    rtype = str(data.get("resource_type", ""))
    data["operations"] = spec.operations_for(rtype).model_dump()
    return data


def load_contract(path: str | Path) -> EnsureContract:
    import yaml

    from openadapt_flow.qualify.config import _expand

    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise EnsureRecordError(f"cannot read {source}: {exc}") from exc
    missing: list[str] = []
    data = _expand(dict(data), missing)
    if missing:
        raise EnsureRecordError(
            "environment variable(s) not set: " + ", ".join(sorted(set(missing)))
        )
    data = _apply_semantics(data, source)
    try:
        return EnsureContract.model_validate(data)
    except Exception as exc:
        raise EnsureRecordError(f"invalid {source}: {exc}") from exc


# -- values ----------------------------------------------------------------------


def extract(resource: Mapping[str, Any], path: str) -> Any:
    from openadapt_flow.runtime.effects.fhir import extract_path

    return extract_path(dict(resource), path)


def normalize(value: Any, rule: FieldRule, *, task: bool = False) -> Optional[str]:
    """``value`` as the rule compares it. ``map`` turns a task's value into
    the stored form (Massachusetts to MA), so it applies to the task's side
    only: a record that holds "Massachusetts" holds the wrong value."""

    if value is None:
        return None
    text = str(value).strip()
    if task:
        text = rule.map.get(text, text)
    if rule.compare == "casefold":
        return text.casefold()
    if rule.compare == "digits":
        return re.sub(r"\D", "", text)
    if rule.compare == "date":
        # A calendar date, a partial date as written, or the date a date-time
        # was written on (in its own offset, as a person entered it). Anything
        # else, such as
        # "1984-03-07xyz", is not a date and never matches.
        if re.fullmatch(r"\d{4}(-\d{2})?", text, re.ASCII):
            return text  # a FHIR partial date (1984, 1984-03), as written
        try:
            if len(text) == 10:
                return date.fromisoformat(text).isoformat()
            return (
                datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
            )
        except ValueError:
            return None
    if rule.compare == "instant":
        # One moment however it is written (an offset, Z, milliseconds), to
        # the second. A time with no zone names no moment, so never matches.
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            return None
        return _instant(when)
    return text


def reference_field(
    contract: EnsureContract, path: str
) -> Optional[tuple[str, tuple[str, ...]]]:
    """A field's typed Reference parent and target types, never a name guess."""
    if contract.reference_schema is None:
        return None
    paths = contract.reference_schema.paths()
    for parent in (path, path.removesuffix(".reference")):
        if _bare(parent) in paths:
            return parent, paths[_bare(parent)]
    return None


def reference_display(contract: EnsureContract, path: str) -> bool:
    return (
        path.endswith(".display")
        and reference_field(contract, path.removesuffix(".display")) is not None
    )


def _display_has_identity(
    contract: EnsureContract, path: str, params: Mapping[str, Any]
) -> bool:
    parent = path.removesuffix(".display")
    return any(
        f.path in (parent, parent + ".reference") and params.get(n) not in (None, "")
        for n, f in contract.fields.items()
    )


def _field_values(
    contract: EnsureContract, resource: Mapping[str, Any], path: str
) -> list[Any]:
    ref = reference_field(contract, path)
    return extract_all(resource, ref[0] if ref else path)


def same(
    contract: EnsureContract,
    name: str,
    stored: Any,
    wanted: Any,
    references: Optional[ReferenceEvidence] = None,
) -> bool:
    rule = contract.fields[name]
    ref = reference_field(contract, rule.path)
    if ref is not None:
        if stored is None or wanted is None:
            return False
        if references is None:
            raise ReferenceUnreadable(
                "reference comparison needs resolved target evidence"
            )
        return references.identity(stored, ref[1]) == references.identity(
            wanted, ref[1]
        )
    a, b = normalize(stored, rule), normalize(wanted, rule, task=True)
    return a is not None and b is not None and a == b


def extract_all(node: Any, path: str) -> list[Any]:
    """Every value at ``path``, across every list element (``name.given``
    reads each given name of each name; a filter keeps the matching ones)."""

    if not path:
        return (
            [node]
            if not isinstance(node, list)
            else [x for x in node if not isinstance(x, (dict, list))]
        )
    if isinstance(node, list):
        return [v for item in node for v in extract_all(item, path)]
    if not isinstance(node, dict):
        return []
    from openadapt_flow.runtime.effects.fhir import (
        filter_matches,
        parse_filter,
        split_path,
    )

    segs = split_path(path)
    seg, rest = segs[0], ".".join(segs[1:])
    name, _, cond = seg.partition("[")
    child = node.get(name)
    if cond:
        want = parse_filter(cond[:-1])
        if want is None:
            return []
        items = child if isinstance(child, list) else [child]
        child = [i for i in items if filter_matches(i, want)]
    return extract_all(child, rest) if child is not None else []


#: A void path: dotted FHIR element names, each with an optional
#: ``[name=value]`` filter, as ``fields`` paths take.
_VOID_PATH = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(\[[^\[\]\s=]+=[^\[\]\s]+\])?"
    r"(\.[A-Za-z][A-Za-z0-9_]*(\[[^\[\]\s=]+=[^\[\]\s]+\])?)*"
)


def _bare(path: str) -> str:
    """A path with its list filters dropped (``telecom.value``)."""

    return re.sub(r"\[[^\]]*\]", "", path)


def _void_entry(entry: str) -> tuple[str, Any]:
    """``path=value`` split at the first ``=`` outside a filter, checked."""

    depth = 0
    for i, ch in enumerate(entry):
        depth += {"[": 1, "]": -1}.get(ch, 0)
        if ch == "=" and depth == 0:
            path, raw = entry[:i], entry[i + 1 :]
            break
    else:
        raise ValueError(f"void entry {entry!r} needs a path and a value")
    if not _VOID_PATH.fullmatch(path):
        raise ValueError(f"void entry {entry!r}: {path!r} is not an element path")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"void entry {entry!r}: the value is not JSON") from exc
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"void entry {entry!r}: the value must be one JSON scalar")
    return path, value


def is_void(contract: EnsureContract, resource: Mapping[str, Any]) -> bool:
    """True when the resource holds a value that makes it assert nothing.

    Values compare as JSON, so ``1`` is not ``true`` and ``0`` is not ``0.0``.
    """

    for entry in contract.void:
        try:
            path, wanted = _void_entry(entry)
        except ValueError as exc:  # a contract built without validation
            raise Unreadable(str(exc)) from exc
        want = json.dumps(wanted, ensure_ascii=False)
        if any(
            json.dumps(value, ensure_ascii=False) == want
            for value in extract_all(resource, path)
        ):
            return True
    return False


def is_person(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Mapping[str, Any],
    references: Optional[ReferenceEvidence] = None,
) -> bool:
    """True when the resource meets any identity rule for these values.

    Every element counts: a record whose second name, or second email, is
    the person's is the person (a maiden name kept first doesn't hide it).
    A void record (``contract.void``) is never the person.
    """

    if is_void(contract, resource):
        return False
    return any(
        all(
            any(
                same(contract, p, value, params.get(p), references)
                for value in _field_values(contract, resource, contract.fields[p].path)
            )
            for p in rule
        )
        for rule in contract.identity
        if all(params.get(p) not in (None, "") for p in rule)
    )


def wrong_fields(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Mapping[str, Any],
    references: Optional[ReferenceEvidence] = None,
) -> list[str]:
    for name, rule in contract.fields.items():
        if (
            name in params
            and reference_display(contract, rule.path)
            and not _display_has_identity(contract, rule.path, params)
        ):
            raise ReferenceUnreadable(
                "Reference display has no supplied target identity"
            )
    return sorted(
        name
        for name, rule in contract.fields.items()
        if name in params
        and not reference_display(contract, rule.path)
        and not same(
            contract,
            name,
            next(iter(_field_values(contract, resource, rule.path)), None),
            params[name],
            references,
        )
    )


def leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every value with its path, list positions dropped (``telecom.value`` twice)."""

    if isinstance(node, dict):
        return [x for k, v in node.items() for x in leaves(v, f"{prefix}{k}.")]
    if isinstance(node, list):
        return [x for v in node for x in leaves(v, prefix)]
    return [(prefix[:-1], node)]


def _allowed(contract: EnsureContract, path: str, value: Any) -> bool:
    for entry in contract.allow:
        name, eq, want = entry.partition("=")
        # The value is compared as JSON: ``active=true`` is the boolean, and a
        # string is quoted (``address.use="home"``).
        if name == path and (not eq or json.dumps(value, ensure_ascii=False) == want):
            return True
    return False


def _expected_leaves(
    contract: EnsureContract, params: Optional[Mapping[str, Any]] = None
) -> Counter[str]:
    want: Counter[str] = Counter()
    for name, rule in contract.fields.items():
        if params is not None and (
            params.get(name) in (None, "")
            or (
                reference_display(contract, rule.path)
                and not _display_has_identity(contract, rule.path, params)
            )
        ):
            continue  # the task didn't give it, so the record mustn't hold it
        from openadapt_flow.runtime.effects.fhir import parse_filter, split_path

        parts: list[str] = []
        filters: list[str] = []
        for seg in split_path(rule.path):
            key, _, cond = seg.partition("[")
            if cond:
                parsed = parse_filter(cond[:-1])
                if parsed is not None:
                    filters.append(".".join(parts + [key, parsed[0]]))
            parts.append(key)
        leaf = ".".join(parts)
        want[leaf] += 1
        # The filter's own element is a leaf too (``telecom.system``), unless
        # it is the value itself (``participant[actor.reference^=Patient/]
        # .actor.reference``), which is one leaf, not two.
        for path in filters:
            if path != leaf:
                want[path] += 1
    # These allowances apply only to a schema-proven Reference with an
    # independently checked identity supplied by this task.
    for name, rule in contract.fields.items():
        ref = reference_field(contract, rule.path)
        if ref is None or (params is not None and params.get(name) in (None, "")):
            continue
        parent = _bare(ref[0])
        if rule.path == ref[0]:
            want.pop(parent, None)
        for member in (
            "reference",
            "type",
            "identifier.system",
            "identifier.value",
            "display",
        ):
            want[parent + "." + member] = max(1, want[parent + "." + member])
    return want


def extra_elements(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Leaf paths the record holds beyond the fields the task gave.

    The server's own ``meta`` is exempt, except ``meta.security`` and
    ``meta.tag``: a security label can hide the record from other users.
    """

    def exempt(path: str) -> bool:
        top = path.split(".")[0]
        if top == "meta":
            return not path.startswith(("meta.security", "meta.tag"))
        return top in ("id", "resourceType")

    have = Counter(
        p
        for p, value in leaves(dict(resource))
        if not exempt(p) and not _allowed(contract, p, value)
    )
    return sorted((have - _expected_leaves(contract, params)).elements())


def holds_only_task_values(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Mapping[str, Any],
    references: Optional[ReferenceEvidence] = None,
) -> bool:
    """True when every value the version holds is the task's (``consistent``).

    A value may still be missing; none may differ, none may be one the task
    didn't give, and on a ``closed`` contract nothing else may be held. Every
    element counts (a second given name that isn't the task's refuses), and
    a void version is never consistent.
    """

    if is_void(contract, resource):
        return False
    for name, rule in contract.fields.items():
        if reference_display(contract, rule.path):
            if extract_all(resource, rule.path) and not _display_has_identity(
                contract, rule.path, params
            ):
                return False
            continue
        held = [
            v
            for v in _field_values(contract, resource, rule.path)
            if v not in (None, "")
        ]
        if not held:
            continue
        if params.get(name) in (None, ""):
            return False
        if not all(same(contract, name, v, params[name], references) for v in held):
            return False
    # On any contract, open or closed: a version in between may hold nothing
    # the task didn't give (another person's identifier, an address).
    return not extra_elements(contract, resource, params)


def _between_ok(
    contract: EnsureContract,
    version: Mapping[str, Any],
    params: Mapping[str, Any],
    references: Optional[ReferenceEvidence] = None,
) -> bool:
    mode = contract.operations.correction.between
    if mode == "consistent":
        return holds_only_task_values(contract, version, params, references)
    if mode == "either":
        return is_person(
            contract, version, params, references
        ) or holds_only_task_values(contract, version, params, references)
    return is_person(contract, version, params, references)


def _strip(node: Any, path: str) -> Any:
    """``node`` without the element a bookkeeping path names."""

    out = dict(node)
    m = _BOOK_EXTENSION.fullmatch(path)
    if m:
        kept = [
            e
            for e in out.get("extension") or []
            if not (isinstance(e, dict) and e.get("url") == m.group(1))
        ]
        if kept:
            out["extension"] = kept
        else:
            out.pop("extension", None)
        return out
    head, _, leaf = path.partition(".")
    if isinstance(out.get(head), dict):
        inner = {k: v for k, v in out[head].items() if k != leaf}
        if inner:
            out[head] = inner
        else:
            del out[head]
    return out


def _same_outside(a: Mapping[str, Any], b: Mapping[str, Any], paths: list[str]) -> bool:
    left, right = dict(a), dict(b)
    for path in paths:
        left, right = _strip(left, path), _strip(right, path)
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def bookkeeping_only(
    contract: EnsureContract,
    change: "Change",
    protected: Collection[str],
    selfs: Collection[str] = (),
) -> bool:
    """True when ``change`` is the agent's update of another record that moved
    only the elements a ``bookkeeping`` rule names for its type.

    Fail-closed: the record must not be ``protected`` (the record the task
    writes, or any record that is the person); every write of it in the
    window must be an attributed, logged update with a time; ``_history``
    must give as many versions as the trail gives writes, and the version
    before the window; each version must differ from the one before only
    in the named elements.
    """

    rtype = change.ref.split("/", 1)[0]
    paths = [
        p
        for r in contract.operations.bookkeeping
        if r.resource_type == rtype
        for p in r.paths
    ]
    if not paths or change.ref in protected:
        return False
    if change.actions != {"U"} or not change.attributed or change.unlogged:
        return False
    if not change.events or any(when is None for when, _, _ in change.events):
        return False
    if any(not who or set(who) - set(selfs) for _, _, who in change.events):
        return False  # only the agent's own updates can be its bookkeeping
    if change.versions is None or change.prior is None:
        return False
    if change.versions != len(change.events) or not change.bodies:
        return False
    if any(not (b.get("meta") or {}).get("lastUpdated") for b in change.bodies):
        return False  # a version with no time can't be put in order
    ordered = _by_instant(change.bodies)
    if ordered is None:
        return False  # a version with no readable time can't be put in order
    chain = [change.prior, *ordered]
    if not all(_same_outside(a, b, paths) for a, b in zip(chain, chain[1:])):
        return False
    timed = [
        r
        for r in contract.operations.bookkeeping
        if r.resource_type == rtype and r.now_checks
    ]
    for rule in timed:
        for a, b in zip(chain, chain[1:]):
            if not _times_near_now(a, b, rule):
                return False
    return True


def _by_instant(bodies: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """``bodies`` oldest first by the instant of ``meta.lastUpdated``, or None
    when one has no readable time (none, unparseable, or no time zone) or
    two share an instant. Compared as instants, not text: with mixed offsets
    the text order is not the time order. Tied versions have no known order,
    and the server's return order is not a chronology, so a tie refuses."""

    try:
        read = [_when((b.get("meta") or {}).get("lastUpdated")) for b in bodies]
    except Unreadable:
        return None
    times = [when for when in read if when is not None]
    if len(times) != len(bodies):
        return None
    if len(set(times)) != len(times):
        return None
    order = sorted(range(len(bodies)), key=lambda i: times[i])
    return [bodies[i] for i in order]


def _times_near_now(
    before: Mapping[str, Any], after: Mapping[str, Any], rule: BookkeepingRule
) -> bool:
    """Every time stamp or date ``rule``'s extensions changed to is near
    ``after``'s own ``meta.lastUpdated``; a version with no readable time
    refuses. A value counts as unchanged only at the same path."""

    now = _when((after.get("meta") or {}).get("lastUpdated"))
    if now is None:
        return False
    for path in rule.paths:
        m = _BOOK_EXTENSION.fullmatch(path)
        if not m:
            continue  # meta.lastUpdated is the server's own clock

        def values(res: Mapping[str, Any], url: str = m.group(1)) -> list[str]:
            return sorted(
                str(v)
                for e in res.get("extension") or []
                if isinstance(e, dict) and e.get("url") == url
                for _, v in leaves(e)
            )

        if values(before) == values(after):
            continue
        for value in values(after):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                if value != now.date().isoformat():
                    return False
                continue
            if not re.match(r"\d{4}-\d{2}-\d{2}T", value):
                continue  # the extension's url or a non-time value
            try:
                when = _when(value)
            except Unreadable:
                return False
            if when is None or abs((when - now).total_seconds()) > rule.now_within_s:
                return False
    return True


# -- the read path ---------------------------------------------------------------

Getter = Callable[
    [str, Optional[Mapping[str, Any]]], tuple[int, Mapping[str, str], Any]
]


def _ref(value: str) -> str:
    """``Type/id``, whether the server wrote it relative, absolute or versioned
    (``Patient/x/_history/2``)."""

    parts = value.rstrip("/").split("/")
    if "_history" in parts[1:]:
        parts = parts[: parts.index("_history", 1)]
    return "/".join(parts[-2:]) if len(parts) >= 2 else value


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class FhirReader:
    """Read-only FHIR client: every page of a search, or :class:`Unreadable`.

    ``get`` is injectable for tests: ``get(url, params) -> (status, headers,
    json)``. The default uses ``httpx`` with the contract's bearer token.
    """

    def __init__(self, contract: EnsureContract, get: Optional[Getter] = None) -> None:
        self.base_url = contract.base_url.rstrip("/")
        self.timeout_s = contract.timeout_s
        self._token_env = contract.access_token_env
        self._get = get or self._httpx_get
        self._client: Any = None

    def _httpx_get(
        self, url: str, params: Optional[Mapping[str, str]]
    ) -> tuple[int, Mapping[str, str], Any]:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        headers = {"Accept": "application/fhir+json"}
        if self._token_env:
            token = os.environ.get(self._token_env)
            if not token:
                raise Unreadable(f"no access token in {self._token_env}")
            headers["Authorization"] = f"Bearer {token}"
        resp = self._client.get(url, params=dict(params or {}), headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, dict(resp.headers), body

    def fetch(
        self, url: str, params: Optional[Mapping[str, Any]] = None
    ) -> tuple[Mapping[str, str], Any]:
        try:
            status, headers, body = self._get(url, params)
        except Unreadable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure is unreadable
            raise Unreadable(f"GET {url} failed: {type(exc).__name__}") from exc
        if status // 100 != 2:
            raise Unreadable(f"GET {url} returned HTTP {status}")
        return headers, body

    def bundle(
        self, path: str, params: Optional[Mapping[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """Every entry of a search or history Bundle, across all pages."""

        url: Optional[str] = f"{self.base_url}/{path}"
        query: Optional[Mapping[str, Any]] = params
        entries: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            assert url is not None
            _, body = self.fetch(url, query)
            if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
                raise Unreadable(f"{path}: the reply is not a Bundle")
            page = body.get("entry") or []
            if not isinstance(page, list):
                raise Unreadable(f"{path}: entry is not a list")
            entries += [e for e in page if isinstance(e, dict)]
            for e in page:
                res = e.get("resource") if isinstance(e, dict) else None
                if (e.get("search") or {}).get("mode") != "outcome" or not isinstance(
                    res, dict
                ):
                    continue
                # A warning ("no results") is a message; an error means the
                # search may be partial, and a missed match is a duplicate.
                issues = res.get("issue") or []
                if not isinstance(issues, list) or any(
                    not isinstance(i, dict)
                    or i.get("severity") not in ("warning", "information")
                    for i in issues
                ):
                    raise Unreadable(f"{path}: the search returned an error outcome")
            nxt = next(
                (
                    link.get("url")
                    for link in body.get("link") or []
                    if isinstance(link, dict) and link.get("relation") == "next"
                ),
                None,
            )
            if not nxt:
                total = body.get("total")
                if isinstance(total, int) and body.get("type") == "searchset":
                    found = len(
                        {
                            json.dumps(
                                [
                                    (e.get("resource") or {}).get("resourceType"),
                                    (e.get("resource") or {}).get("id"),
                                ]
                            )
                            for e in entries
                            if (e.get("search") or {}).get("mode", "match") == "match"
                        }
                    )
                    if total > found:
                        raise Unreadable(f"{path}: total {total} but read {found}")
                return entries
            if _origin(nxt) != _origin(self.base_url):
                raise Unreadable(f"{path}: next page on another origin")
            url, query = nxt, None
        raise Unreadable(f"{path}: more than {MAX_PAGES} pages")

    def resources(
        self, path: str, params: Optional[Mapping[str, Any]] = None
    ) -> list[dict[str, Any]]:
        out = []
        for entry in self.bundle(path, params):
            if (entry.get("search") or {}).get("mode", "match") != "match":
                continue
            res = entry.get("resource")
            if not isinstance(res, dict) or not res.get("id"):
                raise Unreadable(f"{path}: an entry has no resource id")
            out.append(res)
        return out

    def reference_evidence(
        self,
        contract: EnsureContract,
        params: Mapping[str, Any],
        resources: Collection[Mapping[str, Any]],
    ) -> ReferenceEvidence:
        def fetch(path: str) -> Mapping[str, Any]:
            _, body = self.fetch(f"{self.base_url}/{path}")
            return body

        reader = ReferenceReader(self.base_url, fetch, self.resources)
        for name, rule in contract.fields.items():
            ref = reference_field(contract, rule.path)
            if ref is None:
                continue
            if params.get(name) not in (None, ""):
                reader.collect(params[name], ref[1])
            for resource in resources:
                for value in extract_all(resource, ref[0]):
                    reader.collect(value, ref[1])
        return reader.snapshot()

    def server_time(self) -> datetime:
        """The server's clock, from the ``Date`` header of a metadata read."""

        headers, _ = self.fetch(f"{self.base_url}/metadata", {"_summary": "true"})
        raw = {k.lower(): v for k, v in headers.items()}.get("date")
        if not raw:
            raise Unreadable("the server sent no Date header")
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise Unreadable("the server's Date header can't be read") from exc


def _instant(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        raise Unreadable(f"a timestamp with no time zone: {value}")
    return when


# -- snapshots -------------------------------------------------------------------


@dataclass
class Baseline:
    """What was true before the write: the window start and the matches."""

    started: datetime
    #: id -> resource, every record that was the person before the write.
    matches: dict[str, dict[str, Any]]
    #: The candidate searches' result count (for evidence only).
    candidates: int = 0


@dataclass
class Change:
    """One write in the window, by resource."""

    ref: str  # "Type/id"
    actions: set[str] = field(default_factory=set)
    #: Actors who wrote it, from history (server-set) and the audit trail.
    authors: set[str] = field(default_factory=set)
    attributed: bool = True
    #: True when only the sweep saw it (the audit trail did not log it).
    unlogged: bool = False
    #: The audit trail's writes of it: (time, action, actors), time None when
    #: the event gives none.
    events: list[tuple[Optional[datetime], str, frozenset[str]]] = field(
        default_factory=list
    )
    #: Versions ``_history`` shows in the window (None: history not read).
    versions: Optional[int] = None
    #: Those versions as the server returned them.
    bodies: list[dict[str, Any]] = field(default_factory=list)
    #: The last version before the window (None: none, or history not read).
    prior: Optional[dict[str, Any]] = None
    #: The audit trail's reads of it by id: (time, actors).
    reads: list[tuple[Optional[datetime], frozenset[str]]] = field(default_factory=list)


def _find_matches(
    contract: EnsureContract, reader: FhirReader, params: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], int]:
    """Every live record that is the person, by each identity rule's search."""

    seen: dict[str, dict[str, Any]] = {}
    queries: list[dict[str, str]] = []
    expected = (
        reader.reference_evidence(contract, params, [])
        if contract.reference_schema
        else ReferenceEvidence()
    )

    def query_value(name: str) -> str:
        ref = reference_field(contract, contract.fields[name].path)
        return (
            expected.identity(params[name], ref[1])
            if ref
            else str(params[name]).strip()
        )

    for rule in contract.identity:
        given = [p for p in rule if params.get(p) not in (None, "")]
        if len(given) < len(rule):
            continue  # the task doesn't give every value this rule needs
        # The whole rule, and each value alone: a server's search may compare
        # differently from the contract, so the candidates are wide and the
        # contract's own comparison decides. A value with no search parameter
        # (an allergy's substance code) only narrows nothing: the search uses
        # the rule's other values, and the contract compares it after.
        searchable = [p for p in rule if p in contract.search]
        queries.append({contract.search[p]: query_value(p) for p in searchable})
        if len(searchable) > 1:
            queries += [{contract.search[p]: query_value(p)} for p in searchable]
    if not queries:
        raise Unreadable("the task gives the values of no identity rule")
    for query in queries:
        for res in reader.resources(contract.resource_type, {**query, "_count": "200"}):
            seen[str(res["id"])] = res
    references = (
        reader.reference_evidence(contract, params, list(seen.values()))
        if contract.reference_schema
        else None
    )
    matches = {
        i: r for i, r in seen.items() if is_person(contract, r, params, references)
    }
    return matches, len(seen)


def find_matches(
    contract: EnsureContract, reader: FhirReader, params: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], int]:
    """Resolve Reference evidence outside judging; unsupported targets are unreadable."""
    try:
        return _find_matches(contract, reader, params)
    except ReferenceUnreadable as exc:
        raise Unreadable(str(exc)) from exc


def capture_baseline(
    contract: EnsureContract,
    params: Mapping[str, Any],
    reader: Optional[FhirReader] = None,
) -> Baseline:
    reader = reader or FhirReader(contract)
    # One second before the server's clock: an instant search is to the second.
    started = reader.server_time() - timedelta(seconds=1)
    matches, candidates = find_matches(contract, reader, params)
    return Baseline(started=started, matches=matches, candidates=candidates)


def read_changes(
    contract: EnsureContract,
    reader: FhirReader,
    since: datetime,
    until: Optional[datetime] = None,
) -> dict[str, Change]:
    """Every resource written in ``[since, until]``, with who wrote it."""

    def window(param: str) -> dict[str, Any]:
        bounds = [f"ge{_instant(since)}"]
        if until is not None:
            bounds.append(f"le{_instant(until)}")
        return {param: bounds, "_count": "200"}

    feed = contract.change_feed
    selves = {_ref(x) for x in contract.self_actors}
    changes: dict[str, Change] = {}
    reads: dict[str, list[tuple[Optional[datetime], frozenset[str]]]] = {}
    if feed.audit is not None:
        a = feed.audit
        for event in reader.resources(a.resource_type, window(a.since_param)):
            action = extract(event, a.action_path)
            if action not in AUDIT_ACTIONS:
                raise Unreadable(f"an audit event has action {action!r}")
            when = _when(extract(event, a.time_path))
            actors = [
                _ref(x)
                for x in extract_all(event, a.actor_path)
                if isinstance(x, str) and x
            ]
            # Every resource the event names, not only the first.
            refs = sorted(
                {
                    _ref(x)
                    for x in extract_all(event, a.entity_path)
                    if isinstance(x, str) and "/" in x
                }
            )
            if action == "R":
                for ref in refs:
                    reads.setdefault(ref, []).append((when, frozenset(actors)))
                continue
            if not refs:
                if action == "E" and actors and not set(actors) & selves:
                    continue  # another user's operation on no resource
                # A write the log can't tie to a resource: the window can't be
                # read completely.
                raise Unreadable("an audit write names no resource")
            for ref in refs:
                change = changes.setdefault(ref, Change(ref=ref))
                change.actions.add(str(action))
                change.events.append((when, str(action), frozenset(actors)))
                if actors:
                    change.authors.update(actors)
                else:
                    change.attributed = False
        for ref, seen_by in reads.items():
            if ref in changes:
                changes[ref].reads = seen_by
    for rtype in feed.sweep_types:
        for res in reader.resources(rtype, window("_lastUpdated")):
            ref = f"{rtype}/{res['id']}"
            if ref not in changes:
                changes[ref] = Change(ref=ref, actions={"U"}, unlogged=True)
    if feed.history:
        for ref, change in changes.items():
            change.versions = 0
            authors: set[str] = set()
            for entry in reader.bundle(f"{ref}/_history", {"_count": "200"}):
                version = entry.get("resource")
                req = entry.get("request") or {}
                if isinstance(version, dict):
                    meta = version.get("meta") or {}
                    when = _when(meta.get("lastUpdated"))
                    if when is None:
                        # A version with no time can't be placed in or out of
                        # the window; skipping it would hide it from the
                        # version count and the checks on each version.
                        raise Unreadable(f"a version of {ref} has no lastUpdated")
                    if when < since:
                        last = _when(
                            ((change.prior or {}).get("meta") or {}).get("lastUpdated")
                        )
                        if last is None or when > last:
                            change.prior = version
                        continue
                    if until and when > until:
                        continue
                    change.versions += 1
                    change.bodies.append(version)
                    if not feed.history_author:
                        continue
                    who = (meta.get("author") or {}).get("reference")
                    if isinstance(who, str) and who:
                        authors.add(_ref(who))
                    else:
                        change.attributed = False
                elif str(req.get("method", "")).upper() == "DELETE":
                    change.actions.add("D")
            if authors:
                if change.authors and change.authors != authors:
                    # The log and the record disagree on who wrote it.
                    change.attributed = False
                change.authors |= authors
            elif change.unlogged:
                # Neither the log nor the record says who wrote it.
                change.attributed = False
    return changes


# -- the judgment ----------------------------------------------------------------


@dataclass
class EnsureResult:
    outcome: Outcome
    record_id: Optional[str]
    reasons: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "id": self.record_id,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


def _review(reasons: list[str], evidence: dict[str, Any]) -> EnsureResult:
    return EnsureResult("needs_review", None, reasons, evidence)


def _judge(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    changes: Mapping[str, Change],
    after_matches: Mapping[str, Mapping[str, Any]],
    created: Optional[Mapping[str, Any]],
    existing_now: Optional[Mapping[str, Any]] = None,
    references: Optional[ReferenceEvidence] = None,
) -> EnsureResult:
    """Decide the outcome from the snapshots. Reads nothing itself.

    ``created`` is the current version of the one resource the agent
    created (or None when it created none, or more than one).
    ``existing_now`` is the current version of the one record that was
    already the person (None when it is gone): someone else may have edited
    it during the run, which doesn't change what the agent found.
    """

    selfs = {_ref(a) for a in contract.self_actors}
    own_type = contract.resource_type
    mine = {
        ref: c
        for ref, c in changes.items()
        if not c.attributed or not c.authors or c.authors & selfs
    }
    others = {ref: c for ref, c in changes.items() if ref not in mine}
    booked: list[str] = []
    if contract.operations.bookkeeping:
        protected = {f"{own_type}/{i}" for i in (*baseline.matches, *after_matches)}
        protected |= {r for r, c in mine.items() if "C" in c.actions}
        booked = sorted(
            r
            for r, c in mine.items()
            if bookkeeping_only(contract, c, protected, selfs)
        )
        mine = {r: c for r, c in mine.items() if r not in booked}
    evidence: dict[str, Any] = {
        "before_matches": len(baseline.matches),
        "after_matches": len(after_matches),
        "agent_writes": len(mine),
        "other_writes": len(others),
        "unattributed_writes": sum(
            1 for c in mine.values() if not c.attributed or not c.authors
        ),
        "unlogged_writes": sum(1 for c in changes.values() if c.unlogged),
        "agent_write_types": sorted({r.split("/")[0] for r in mine}),
    }
    if booked:
        evidence["bookkeeping_writes"] = len(booked)
    if not mine:
        if len(baseline.matches) == 1:
            (rid,) = baseline.matches
            dupes = sorted(set(after_matches) - {rid})
            if dupes:
                return _review(
                    [f"{len(dupes)} other record(s) are now also the person"], evidence
                )
            if existing_now is not None and is_void(contract, existing_now):
                return _review(
                    ["the existing record now asserts nothing (void)"], evidence
                )
            if rid in after_matches or (
                existing_now is not None and str(existing_now.get("id")) == rid
            ):
                return EnsureResult(
                    "found_existing",
                    rid,
                    ["one record was already the person"],
                    evidence,
                )
            return _review(
                ["the existing record is gone or no longer matches"], evidence
            )
        if not baseline.matches:
            return _review(
                ["no record is the person and the agent created none"], evidence
            )
        return _review(
            [f"{len(baseline.matches)} records were already the person"], evidence
        )
    reasons: list[str] = []
    if baseline.matches:
        reasons.append(
            f"{len(baseline.matches)} record(s) were already the person, "
            "and the agent wrote anyway"
        )
    creates = [
        r for r, c in mine.items() if r.startswith(own_type + "/") and "C" in c.actions
    ]
    transient: list[str] = []
    if contract.operations.cardinality.own_transient:
        transient = [
            r
            for r in creates
            if r.split("/", 1)[1] not in after_matches
            and transient_ok(contract, mine[r], selfs, params, changes, references)
        ]
        creates = [r for r in creates if r not in transient]
        if transient:
            evidence["transient_records"] = len(transient)
    if len(creates) != 1:
        reasons.append(f"the agent created {len(creates)} {own_type} records, not one")
    unexplained = [r for r, c in mine.items() if not c.attributed or not c.authors]
    if unexplained:
        reasons.append(f"{len(unexplained)} write(s) no source attributes")
    target = creates[0] if len(creates) == 1 else None
    extra = sorted(r for r in mine if r != target and r not in transient)
    if extra:
        types = sorted({r.split("/")[0] for r in extra})
        reasons.append(
            f"the agent wrote {len(extra)} other resource(s): {', '.join(types)}"
        )
    if target is not None:
        tid = target.split("/", 1)[1]
        corrected = bool(set(mine[target].actions) - {"C"})
        if corrected:
            refused = correction_refused(
                contract, mine[target], selfs, params, references
            )
            if refused:
                reasons.append(refused)
        if target in others or (changes[target].authors - selfs):
            reasons.append("someone else changed the created record")
        if changes[target].versions is not None:
            # Who wrote each version in the window, for the evidence: version
            # ids and actor references only, never a value.
            evidence["versions"] = version_trail(changes[target])
        if created is None or str(created.get("id")) != tid:
            reasons.append("the created record can't be read back")
        else:
            unbound = read_back_unbound(changes[target], created)
            if unbound:
                reasons.append(unbound)
            wrong = wrong_fields(contract, created, params, references)
            if wrong:
                reasons.append("wrong or missing: " + ", ".join(wrong))
            if is_void(contract, created):
                reasons.append("the created record asserts nothing (void)")
            elif not is_person(contract, created, params, references):
                reasons.append("the created record is not the person")
            if contract.closed:
                extra_paths = extra_elements(contract, created, params)
                if extra_paths:
                    reasons.append("not given by the task: " + ", ".join(extra_paths))
        dupes = sorted(set(after_matches) - {tid})
        if dupes:
            reasons.append(f"{len(dupes)} other record(s) are now also the person")
        if tid not in after_matches and created is not None:
            reasons.append("the created record does not come back from a search")
        if not reasons:
            removed = (
                f"; it also deleted {len(transient)} record(s) it had created by "
                "mistake, each holding only the task's values and read by no one else"
                if transient
                else ""
            )
            if corrected:
                writes = len(mine[target].events)
                evidence["corrected"] = True
                evidence["writes_to_record"] = writes
                return EnsureResult(
                    "created_verified",
                    tid,
                    [
                        f"created once and corrected by the agent ({writes} writes, "
                        "no other actor read or wrote it between them), every "
                        f"value read back{removed}"
                    ],
                    evidence,
                )
            return EnsureResult(
                "created_verified",
                tid,
                [f"created once, every value read back{removed}"],
                evidence,
            )
    return _review(reasons, evidence)


def judge(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    changes: Mapping[str, Change],
    after_matches: Mapping[str, Mapping[str, Any]],
    created: Optional[Mapping[str, Any]],
    existing_now: Optional[Mapping[str, Any]] = None,
    references: Optional[ReferenceEvidence] = None,
) -> EnsureResult:
    """Judge snapshots without I/O; missing target evidence stays uncertain."""
    try:
        return _judge(
            contract,
            params,
            baseline,
            changes,
            after_matches,
            created,
            existing_now,
            references,
        )
    except ReferenceUnreadable as exc:
        return _review([f"unreadable: {exc}"], {"unreadable": True})


def _version_meta(version: Mapping[str, Any]) -> tuple[Optional[datetime], str]:
    meta = version.get("meta") or {}
    if not isinstance(meta, Mapping):
        return None, ""
    vid = meta.get("versionId")
    return _when(meta.get("lastUpdated")), str(vid) if vid not in (None, "") else ""


def read_back_unbound(change: Change, created: Mapping[str, Any]) -> Optional[str]:
    """Why the read-back can't be shown to be the last version written, or None.

    With ``_history`` read, the record judged must be the newest version in
    the window, by ``meta.versionId``: a version written after the window
    (by anyone) or a stale read is not what the agent's writes left. Without
    history (``versions`` None) there is nothing to bind to, and the other
    checks decide as before.
    """

    if change.versions is None:
        return None
    if not change.bodies:
        return "the created record's _history shows no version in the window"
    timed = [_version_meta(body) for body in change.bodies]
    if any(when is None or not vid for when, vid in timed):
        return "a version in the window has no versionId or time to bind to"
    newest = max(when for when, _ in timed if when is not None)
    last = {vid for when, vid in timed if when == newest}
    if len(last) != 1:
        return "two versions share the newest time, so the last write can't be told"
    _, got = _version_meta(created)
    if not got:
        return "the read-back carries no versionId to bind to"
    if got not in last:
        return "the read-back is not the last version written in the window"
    return None


_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)


def version_trail(change: Change) -> list[dict[str, Optional[str]]]:
    """Each version in the window, oldest first: its id and who wrote it.

    Only ids and actor references; the version bodies stay out of evidence.
    The author is ``meta.author`` when the server returns it. When it
    doesn't (some servers omit it), and the audit trail logged exactly one
    timed create or update per version, the n-th logged write names the
    n-th version, but only when that order is unambiguous: every version
    has a time, and no two versions and no two logged writes share one (a
    tie keeps whatever order the server returned). Otherwise ``author`` is
    None: unknown, never guessed.
    """

    rows: list[tuple[Optional[datetime], str, Optional[str]]] = []
    for body in change.bodies:
        when, vid = _version_meta(body)
        meta = body.get("meta") or {}
        who = (
            (meta.get("author") or {}).get("reference")
            if isinstance(meta, Mapping)
            else None
        )
        rows.append((when, vid, _ref(who) if isinstance(who, str) and who else None))
    # By instant, not by text: with mixed offsets the text order is not the
    # time order, and the n-th logged write would name the wrong version.
    # A version with no time sorts first, as before.
    rows.sort(key=lambda r: (r[0] is not None, r[0] or _NO_TIME))
    writes = [
        (when, who)
        for when, action, who in change.events
        if action in ("C", "U") and when is not None
    ]
    logged: list[Optional[str]] = [None] * len(rows)
    version_times = [r[0] for r in rows]
    write_times = [w[0] for w in writes]
    if (
        len(writes) == len(rows)
        and len(writes) == sum(1 for _, a, _ in change.events if a in ("C", "U"))
        and None not in version_times
        and len(set(version_times)) == len(version_times)
        and len(set(write_times)) == len(write_times)
    ):
        logged = [
            ", ".join(sorted(who)) if who else None
            for _, who in sorted(writes, key=lambda w: w[0])
        ]
    return [
        {"version": vid or None, "author": author or audit}
        for (_, vid, author), audit in zip(rows, logged)
    ]


def correction_refused(
    contract: EnsureContract,
    change: Change,
    selfs: set[str],
    params: Mapping[str, Any],
    references: Optional[ReferenceEvidence] = None,
) -> Optional[str]:
    """Why a create followed by more writes can't count as one write, or None.

    The agent may fix its own record (a value it read back wrong) and still
    have created it once. That holds only when the audit trail shows exactly
    one create and then updates, each with a time and each by the agent, and
    no other actor reading the record by id from the create to the last
    update: someone may have acted on the wrong value. Every other write
    (another record, a second create, a delete) is judged as before. A read
    the trail doesn't log by id (a search) can't be seen; the contract's
    heartbeat checks that reads by id are logged at all.
    """

    changed = "the agent changed or deleted the record after creating it"
    if not contract.corrections:
        return changed
    if set(change.actions) != {"C", "U"}:
        return changed
    if contract.change_feed.audit is None:
        return f"{changed}, and without an audit trail no one can tell who read it"
    if not change.attributed or not change.events:
        return f"{changed}, and the audit trail doesn't attribute every write"
    if any(not who or who - selfs for _, _, who in change.events):
        return "someone else changed the created record"
    if contract.settle_s < 1:
        # The verifier's own read of the record must land after the window.
        return f"{changed}, and with settle_s under 1 s its reads can't be told apart"
    if change.versions is None:
        return f"{changed}, and without _history its versions can't be counted"
    if any(
        not _between_ok(contract, body, params, references) for body in change.bodies
    ):
        # A version that was someone else (or no one) may have been found by
        # a search no audit event names, and acted on.
        if contract.operations.correction.between != "person":
            return (
                f"{changed}, and a version in between held a value the task didn't give"
            )
        return f"{changed}, and a version in between was not the person"
    if change.versions != len(change.events):
        # A write the trail didn't log may be someone else's.
        return (
            f"{changed}, and its history shows {change.versions} versions "
            f"where the audit trail shows {len(change.events)} writes"
        )
    timed: list[tuple[datetime, str]] = []
    for when, action, _ in change.events:
        if when is None:
            return (
                f"unreadable: {changed}, and the audit trail gives no time for a write"
            )
        timed.append((when, action))
    creates = [when for when, action in timed if action == "C"]
    updates = [when for when, action in timed if action == "U"]
    if len(creates) != 1 or not updates or min(updates) < creates[0]:
        return changed
    start = creates[0]
    # From the create to the window's close: a read logged a moment after the
    # last update may have seen the version before it.
    for read_at, who in change.reads:
        if read_at is None:
            return f"unreadable: {changed}, and a read of it has no time"
        if start <= read_at and (not who or who - selfs):
            return "another actor read the record between the agent's writes"
    return None


def _refers_to(resource: Mapping[str, Any], ref: str) -> bool:
    """True when any string in ``resource`` is a reference to ``ref``
    (relative, absolute or versioned: ``Patient/y``, ``http://x/Patient/y``,
    ``Patient/y/_history/2``)."""

    return any(
        isinstance(value, str) and "/" in value and _ref(value) == ref
        for _, value in leaves(dict(resource))
    )


def transient_ok(
    contract: EnsureContract,
    change: Change,
    selfs: set[str],
    params: Mapping[str, Any],
    changes: Mapping[str, Change] = {},
    references: Optional[ReferenceEvidence] = None,
) -> bool:
    """True when ``change`` is a record the agent created and deleted again
    that ``cardinality.own_transient`` lets count as not created.

    Every write of it is the agent's, timed and logged, the create first and
    the delete last; ``_history`` shows as many versions as the trail shows
    creates and updates; every version holds only the task's values; and no
    other actor read it by id. Its ``_history`` must be read, and the trail
    must log reads by id (the verifier's heartbeat).
    """

    audit = contract.change_feed.audit
    if audit is None or not audit.heartbeat or contract.settle_s < 1:
        return False  # "no one else read it" needs a trail that logs reads
    if not contract.change_feed.history:
        return False
    # Nobody may have used it: no version of any other write in the window
    # may refer to it (an Encounter, an Observation filed on the duplicate).
    for ref, other in changes.items():
        if ref == change.ref:
            continue
        if other.versions is None:
            return False
        if any(_refers_to(b, change.ref) for b in other.bodies):
            return False
    if change.actions not in ({"C", "D"}, {"C", "U", "D"}):
        return False
    if not change.attributed or not change.events or change.versions is None:
        return False
    if any(when is None or not who or who - selfs for when, _, who in change.events):
        return False
    timed = sorted(
        (when, action) for when, action, _ in change.events if when is not None
    )
    if [a for _, a in timed].count("C") != 1 or timed[0][1] != "C":
        return False
    if [a for _, a in timed].count("D") != 1 or timed[-1][1] != "D":
        return False
    if change.versions != sum(1 for _, a in timed if a in ("C", "U")):
        return False
    if not all(
        holds_only_task_values(contract, b, params, references) for b in change.bodies
    ):
        return False
    return not any(when is None or not who or who - selfs for when, who in change.reads)


def _hear_own_read(
    contract: EnsureContract,
    reader: FhirReader,
    ref: str,
    until: datetime,
    sleep: Callable[[float], None],
) -> None:
    """Raise Unreadable unless the trail logged the read of ``ref`` just made."""

    audit = contract.change_feed.audit
    assert audit is not None
    query = {
        audit.since_param: f"ge{_instant(until - timedelta(seconds=2))}",
        "entity": ref,
        "action": "R",
    }
    for _ in range(contract.stable_reads + 1):
        if reader.resources(audit.resource_type, query):
            return
        sleep(contract.settle_s)
    raise Unreadable(
        "the audit trail logged no read of the found record; it may not be recording"
    )


def _agents_part(
    changes: Mapping[str, Change], selfs: set[str]
) -> dict[str, tuple[Any, ...]]:
    """The fingerprint of the writes ``judge`` counts as the agent's.

    Writes only: the verifier's own reads land after the window, and must not
    read as a late write.
    """

    return _fingerprint(
        {
            r: c
            for r, c in changes.items()
            if not c.attributed or not c.authors or c.authors & selfs
        },
        reads=False,
    )


def _fingerprint(
    changes: Mapping[str, Change], *, reads: bool = True
) -> dict[str, tuple[Any, ...]]:
    return {
        ref: (
            sorted(c.actions),
            sorted(c.authors),
            c.attributed,
            c.unlogged,
            len(c.events),
            len(c.reads) if reads else 0,
        )
        for ref, c in changes.items()
    }


def verify_ensure(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    *,
    reader: Optional[FhirReader] = None,
    sleep: Callable[[float], None] = time.sleep,
    declared: Collection[str] = (),
) -> EnsureResult:
    """Read the server after the write and judge it against ``baseline``.

    ``declared`` names resources (``Type/id``) the caller saw the agent write
    as other writes, each judged on its own (shadow mode's other episodes).
    They are left out of this judgment; every other change still counts.
    """

    reader = reader or FhirReader(contract)
    skip = {_ref(r) for r in declared}

    def changes_since(until: Optional[datetime]) -> dict[str, Change]:
        found = read_changes(contract, reader, baseline.started, until)
        return {r: c for r, c in found.items() if r not in skip}

    try:
        # The window closes now (the Date header counts whole seconds).
        until = reader.server_time() + timedelta(seconds=1)
        sleep(contract.settle_s)
        changes = changes_since(until)
        for _ in range(contract.stable_reads):
            sleep(contract.settle_s)
            again = changes_since(until)
            if _fingerprint(again) == _fingerprint(changes):
                break
            changes = again
        else:
            raise Unreadable(
                f"the change feed was still changing after {contract.stable_reads} reads"
            )
        after_matches, _ = find_matches(contract, reader, params)
        selfs = {_ref(a) for a in contract.self_actors}
        own = [
            r
            for r, c in changes.items()
            if r.startswith(contract.resource_type + "/")
            and "C" in c.actions
            and (not c.authors or c.authors & selfs or not c.attributed)
        ]
        created = None
        removed = []
        if contract.operations.cardinality.own_transient and len(own) > 1:
            # A record the agent deleted again may count as not created
            # (judged in ``judge``); the record the task wrote is the one left.
            removed = [r for r in own if "D" in changes[r].actions]
            own = [r for r in own if r not in removed]
        if len(own) == 1:
            try:
                _, created = reader.fetch(f"{reader.base_url}/{own[0]}")
            except Unreadable:
                created = None  # gone (deleted) or unreadable: judged below
            audit = contract.change_feed.audit
            if (
                created is not None
                and ("U" in changes[own[0]].actions or removed)
                and audit is not None
                and audit.heartbeat
            ):
                # A correction is accepted only on the trail's word that no
                # one else read the record; check the trail logs reads at all.
                _hear_own_read(contract, reader, own[0], until, sleep)
        existing_now = None
        if len(baseline.matches) == 1 and not own:
            (rid,) = baseline.matches
            try:
                _, existing_now = reader.fetch(
                    f"{reader.base_url}/{contract.resource_type}/{rid}"
                )
            except Unreadable:
                existing_now = None  # gone or unreadable: not found
                if rid in after_matches:
                    # A search still finds it: the read by id failed, so the
                    # heartbeat and the void check can't run on it.
                    raise
            audit = contract.change_feed.audit
            if existing_now is not None and audit is not None and audit.heartbeat:
                _hear_own_read(
                    contract, reader, f"{contract.resource_type}/{rid}", until, sleep
                )
        # Last, with no upper bound: a save of the agent's still in flight
        # when the window closed (a lost response) lands outside it.
        late = _agents_part(changes_since(None), selfs)
        if late != _agents_part(changes, selfs):
            raise Unreadable("the agent wrote after the window closed")
        resources = [r for r in (created, existing_now) if isinstance(r, Mapping)]
        resources += [body for change in changes.values() for body in change.bodies]
        resources += list(after_matches.values())
        references = (
            reader.reference_evidence(contract, params, resources)
            if contract.reference_schema
            else None
        )
        result = judge(
            contract,
            params,
            baseline,
            changes,
            after_matches,
            created,
            existing_now,
            references,
        )
        # Where the read window closed, for a write receipt's scope.
        result.evidence["window_until"] = until.isoformat()
        return result
    except (Unreadable, ReferenceUnreadable) as exc:
        return _review([f"unreadable: {exc}"], {"unreadable": True})


__all__ = [
    "AppSemantics",
    "Baseline",
    "Change",
    "EnsureContract",
    "EnsureRecordError",
    "EnsureResult",
    "FhirReader",
    "Unreadable",
    "capture_baseline",
    "find_matches",
    "is_void",
    "judge",
    "load_contract",
    "load_semantics",
    "read_changes",
    "verify_ensure",
]
