"""The review queue: values and halts that a person must settle.

A run puts a :class:`ReviewItem` in the queue for every field read that
needs review (channels disagree, look-alike characters, low confidence, an
invalid value) and for every halt while reading a record. The queue holds
only problems; agreeing reads never enter it.

A person settles an item with a :class:`ReviewDecision`:

- ``accept_read``: take one channel's value as shown;
- ``correct``: type the value, which must be valid for the field;
- ``reject``: the value is not exported;
- ``reread``: read the record again in a later run.

Every decision is signed with a local Ed25519 key
(:mod:`openadapt_flow.execute.keys`) over the decision and the sha256 of the
item it settles, and is written once: a second decision on the same item is
refused. Rules the queue enforces:

- the reviewer is a person (``person:`` principal). A model or agent
  principal cannot decide;
- the reviewer is not the principal that created the item (no self-approval);
- a settled value keeps the grade its reads support. A person's choice does
  not make a value ``verified``; only an independent read does.

Storage is a directory, ``items/<id>.json`` and ``decisions/<id>.json``, so
it works offline and on a shared drive. No network or model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.records.read import normalize_for
from openadapt_flow.records.schema import (
    FieldRead,
    RecordRead,
    RecordSchema,
    SourceRegion,
)

__all__ = [
    "DecisionAction",
    "ReviewDecision",
    "ReviewError",
    "ReviewItem",
    "ReviewQueue",
    "apply_decision",
]

DecisionAction = Literal["accept_read", "correct", "reject", "reread"]


class ReviewError(ValueError):
    """A decision the queue refuses."""


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewItem(BaseModel):
    """One problem for a person: a field value or a halt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    kind: Literal["field", "halt"]
    schema_name: str
    schema_version: int
    record_key: str
    run_id: str
    field: Optional[str] = None
    reason: str
    confidence: Optional[float] = None
    candidates: dict[str, Optional[str]] = Field(
        default_factory=dict, description="Raw value per channel, as read."
    )
    region: Optional[SourceRegion] = None
    created_by: str
    created_at: str

    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump(mode="json"))).hexdigest()


class ReviewDecision(BaseModel):
    """A person's signed decision on one review item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    item_sha256: str
    action: DecisionAction
    channel: Optional[str] = None
    value: Optional[str] = None
    note: str = ""
    reviewer: str
    decided_at: str
    key_fingerprint: str
    signature: str

    def payload(self) -> bytes:
        return _canonical(self.model_dump(mode="json", exclude={"signature"}))


def _item_id(run_id: str, record_key: str, field: Optional[str]) -> str:
    raw = f"{run_id}\x00{record_key}\x00{field or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _is_person(principal: str) -> bool:
    return principal.startswith("person:") and len(principal) > len("person:")


class ReviewQueue:
    """A directory-backed review queue."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._items = self.root / "items"
        self._decisions = self.root / "decisions"

    # -- filling -------------------------------------------------------------

    def add_field(
        self, record: RecordRead, read: FieldRead, *, created_by: str
    ) -> ReviewItem:
        """Queue one field read that needs review."""

        if not read.needs_review:
            raise ReviewError(f"field {read.field!r} does not need review")
        item = ReviewItem(
            item_id=_item_id(record.run_id, record.record_key, read.field),
            kind="field",
            schema_name=record.schema_name,
            schema_version=record.schema_version,
            record_key=record.record_key,
            run_id=record.run_id,
            field=read.field,
            reason=read.review_reason or "needs review",
            confidence=read.confidence,
            candidates={r.channel: r.raw for r in read.reads},
            region=read.region,
            created_by=created_by,
            created_at=_now(),
        )
        return self._put(item)

    def add_record(self, record: RecordRead, *, created_by: str) -> list[ReviewItem]:
        """Queue every field of ``record`` that needs review."""

        return [
            self.add_field(record, read, created_by=created_by)
            for read in record.fields
            if read.needs_review
        ]

    def add_halt(
        self,
        *,
        schema: RecordSchema,
        record_key: str,
        run_id: str,
        reason: str,
        created_by: str,
        region: Optional[SourceRegion] = None,
    ) -> ReviewItem:
        """Queue a halt while reading a record (the run stopped; nothing was guessed)."""

        item = ReviewItem(
            item_id=_item_id(run_id, record_key, None),
            kind="halt",
            schema_name=schema.name,
            schema_version=schema.version,
            record_key=record_key,
            run_id=run_id,
            reason=reason,
            region=region,
            created_by=created_by,
            created_at=_now(),
        )
        return self._put(item)

    def _put(self, item: ReviewItem) -> ReviewItem:
        path = self._items / f"{item.item_id}.json"
        if path.exists():
            return ReviewItem.model_validate_json(path.read_bytes())
        _write_once(path, item.model_dump_json(indent=2).encode("utf-8"))
        return item

    # -- reading -------------------------------------------------------------

    def item(self, item_id: str) -> ReviewItem:
        return ReviewItem.model_validate_json(
            (self._items / f"{item_id}.json").read_bytes()
        )

    def decision(self, item_id: str) -> Optional[ReviewDecision]:
        path = self._decisions / f"{item_id}.json"
        if not path.exists():
            return None
        return ReviewDecision.model_validate_json(path.read_bytes())

    def items(self) -> list[ReviewItem]:
        if not self._items.is_dir():
            return []
        found = [
            ReviewItem.model_validate_json(p.read_bytes())
            for p in self._items.glob("*.json")
        ]
        return sorted(found, key=lambda i: (i.created_at, i.item_id))

    def open_items(self) -> list[ReviewItem]:
        return [i for i in self.items() if self.decision(i.item_id) is None]

    # -- deciding ------------------------------------------------------------

    def decide(
        self,
        item_id: str,
        *,
        action: DecisionAction,
        reviewer: str,
        signing_key: Any,
        schema: RecordSchema,
        channel: Optional[str] = None,
        value: Optional[str] = None,
        note: str = "",
    ) -> ReviewDecision:
        """Record a person's signed decision on ``item_id``. Written once."""

        from openadapt_flow.execute.keys import fingerprint_of, sign_bytes

        item = self.item(item_id)
        if self.decision(item_id) is not None:
            raise ReviewError(f"item {item_id} is already decided")
        if not _is_person(reviewer):
            raise ReviewError("only a person (person:<id>) can decide a review item")
        if reviewer == item.created_by:
            raise ReviewError("the principal that created an item cannot decide it")
        settled: Optional[str] = None
        if action in ("accept_read", "correct"):
            if item.kind != "field" or item.field is None:
                raise ReviewError(f"{action} applies to field items only")
            spec = schema.field(item.field)
            if action == "accept_read":
                if channel not in item.candidates:
                    raise ReviewError(f"no read from channel {channel!r}")
                settled = normalize_for(spec, item.candidates[channel])
            else:
                settled = normalize_for(spec, value)
            if settled is None:
                raise ReviewError(f"not a valid {spec.kind} for {spec.name!r}")
        elif channel is not None or value is not None:
            raise ReviewError(f"{action} takes no channel or value")
        unsigned = {
            "item_id": item.item_id,
            "item_sha256": item.sha256(),
            "action": action,
            "channel": channel if action == "accept_read" else None,
            "value": settled,
            "note": note,
            "reviewer": reviewer,
            "decided_at": _now(),
            "key_fingerprint": fingerprint_of(signing_key.public_key()),
        }
        signature = sign_bytes(signing_key, _canonical(unsigned))
        decision = ReviewDecision(**unsigned, signature=signature)
        _write_once(
            self._decisions / f"{item_id}.json",
            decision.model_dump_json(indent=2).encode("utf-8"),
        )
        return decision

    def check(self, item_id: str, public_key: Any) -> bool:
        """True when the stored decision is signed by ``public_key`` and its
        item is unchanged since it was decided."""

        from openadapt_flow.execute.keys import fingerprint_of, verify_signature

        decision = self.decision(item_id)
        if decision is None:
            return False
        if decision.key_fingerprint != fingerprint_of(public_key):
            return False
        if decision.item_sha256 != self.item(item_id).sha256():
            return False
        return verify_signature(public_key, decision.payload(), decision.signature)


def apply_decision(read: FieldRead, decision: ReviewDecision) -> Optional[FieldRead]:
    """The field read as settled by ``decision``; None when it is not exported.

    The grade is unchanged: a person's choice settles the value but is not a
    read. ``reject`` and ``reread`` return None.
    """

    if decision.action in ("reject", "reread"):
        return None
    return read.model_copy(
        update={
            "value": decision.value,
            "needs_review": False,
            "review_reason": None,
            "review_decision": decision.item_id,
        }
    )
