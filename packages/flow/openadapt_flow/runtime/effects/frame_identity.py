"""Value correspondence between a selected read and the frame's own rows.

Row bindings select stored records. Optional field bindings identify the
target columns checked against the selected read; other target columns are
reported as unverified coverage. Other rows, tables, audit and completeness
checks remain strict. No record values belong in a binding.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openadapt_flow.runtime.effects.db_snapshot import SqliteBlob, SqliteText

Canonicaliser = Literal["exact", "uuid"]
_UUID = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z"
)


# SQLite snapshot cells carry their storage class: text is a SqliteText and a
# blob a SqliteBlob (its upper-case hex, the text the JSON read gave before).
# Both are read as text here, as before; a SqliteReal is not.
_TEXT = (str, SqliteText, SqliteBlob)


class FrameIdentityError(ValueError):
    """A selected read cannot be mapped unambiguously to the frame."""


class FrameColumn(BaseModel):
    """A field/column correspondence learned from paired stored witnesses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str = Field(min_length=1)
    canonicaliser: Canonicaliser
    witnesses: int = Field(ge=1, strict=True)


class FrameIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_field: str = Field(min_length=1)
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)
    canonicaliser: Canonicaliser
    read_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    witnesses: int = Field(ge=1, strict=True)
    diagnostics: list[Literal["single_witness"]] = Field(default_factory=list)
    #: None is the legacy row-only contract. An empty mapping is an explicit
    #: learned coverage result: none of the observed fields was unambiguous.
    field_bindings: dict[str, FrameColumn] | None = None
    field_columns: list[str] | None = None

    @model_validator(mode="after")
    def _field_coverage(self) -> "FrameIdentity":
        if (self.field_bindings is None) != (self.field_columns is None):
            raise ValueError("field bindings and stored-column inventory go together")
        if self.field_columns is not None:
            if len(set(self.field_columns)) != len(self.field_columns) or any(
                not column for column in self.field_columns
            ):
                raise ValueError(
                    "stored-column inventory must be unique nonempty names"
                )
            if any(
                binding.column not in self.field_columns
                for binding in (self.field_bindings or {}).values()
            ):
                raise ValueError("bound field names an unavailable stored column")
        return self

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.field_bindings is None:
            data.pop("field_bindings", None)
            data.pop("field_columns", None)
        return data


def read_config_digest(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def canonical_identity(value: Any, canonicaliser: Canonicaliser) -> str:
    if canonicaliser == "uuid":
        if isinstance(value, bytes) and len(value) == 16:
            return value.hex()
        if type(value) in _TEXT and _UUID.fullmatch(value):
            return value.replace("-", "").lower()
        raise FrameIdentityError("identity is not a canonicalisable UUID")
    if type(value) in _TEXT and value:
        return "text:" + value
    if type(value) is int:
        return "integer:" + str(value)
    raise FrameIdentityError("identity must be non-null text or an integer")


def _unique(values: Sequence[Any], canonicaliser: Canonicaliser) -> list[str]:
    result = [canonical_identity(v, canonicaliser) for v in values]
    if len(set(result)) != len(result):
        raise FrameIdentityError("identity is not unique")
    return result


def learn_identity(
    records: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    surface_field: str,
    table: str,
    read_config: Mapping[str, Any],
) -> FrameIdentity:
    """Search every stored column; no column-name or application hints."""
    if not records or not rows:
        raise FrameIdentityError(
            "identity learning needs an observed record and stored rows"
        )
    values = [record.get(surface_field) for record in records]
    try:
        for value in values:
            canonical_identity(value, "uuid")
    except FrameIdentityError:
        canonicaliser: Canonicaliser = "exact"
    else:
        canonicaliser = "uuid"
    observed = _unique(values, canonicaliser)
    columns = set(rows[0])
    if any(set(row) != columns for row in rows):
        raise FrameIdentityError("stored rows have inconsistent columns")
    by_value: dict[str, set[str]] = {}
    for row in rows:
        for column in columns:
            try:
                value = canonical_identity(row[column], canonicaliser)
            except FrameIdentityError:
                continue
            by_value.setdefault(value, set()).add(column)
    matches: set[str] = set()
    for identity in observed:
        matching = by_value.get(identity, set())
        if len(matching) != 1:
            raise FrameIdentityError(
                "reader identity must match exactly one stored column"
            )
        matches.update(matching)
    if len(matches) != 1:
        raise FrameIdentityError("reader identities do not share one stored column")
    column = next(iter(matches))
    stored = _unique([row[column] for row in rows], canonicaliser)
    if not set(observed).issubset(stored):
        raise FrameIdentityError("reader identities are absent from stored rows")
    return FrameIdentity(
        surface_field=surface_field,
        table=table,
        column=column,
        canonicaliser=canonicaliser,
        read_config_sha256=read_config_digest(read_config),
        witnesses=len(observed),
        diagnostics=["single_witness"] if len(observed) == 1 else [],
    )


def canonical_field_value(value: Any, canonicaliser: Canonicaliser) -> str:
    """Generic typed values only; no application-specific value transforms."""
    if canonicaliser == "uuid":
        return canonical_identity(value, canonicaliser)
    if isinstance(value, bytes):
        return "bytes:" + value.hex()
    try:
        return "json:" + json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise FrameIdentityError("field value cannot be canonicalised") from error


def learn_field_bindings(
    binding: FrameIdentity,
    records: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, FrameColumn]:
    """Pair the same identity witnesses, then compare every stored column.

    Missing/null, ambiguous or inconsistent fields remain unbound. Values are
    used only in memory and never included in the returned contract data.
    """
    by_identity = {
        canonical_identity(row[binding.column], binding.canonicaliser): row
        for row in rows
    }
    paired = [
        by_identity[
            canonical_identity(record[binding.surface_field], binding.canonicaliser)
        ]
        for record in records
    ]
    fields = set.intersection(*(set(record) for record in records))
    columns = set.intersection(*(set(row) for row in paired))
    result = {}
    for name in sorted(fields):
        values = [record[name] for record in records]
        if any(value is None for value in values):
            continue
        try:
            for value in values:
                canonical_identity(value, "uuid")
        except FrameIdentityError:
            canonicaliser: Canonicaliser = "exact"
        else:
            canonicaliser = "uuid"
        matches = []
        for column in sorted(columns):
            try:
                same = all(
                    row[column] is not None
                    and canonical_field_value(value, canonicaliser)
                    == canonical_field_value(row[column], canonicaliser)
                    for value, row in zip(values, paired)
                )
            except FrameIdentityError:
                same = False
            if same:
                matches.append(column)
        if len(matches) == 1:
            result[name] = FrameColumn(
                column=matches[0], canonicaliser=canonicaliser, witnesses=len(records)
            )
    return result


def identity_token(binding: FrameIdentity, value: Any, key: bytes) -> str:
    message = (
        "openadapt.frame-identity/v1\0"
        + binding.table
        + "\0"
        + binding.column
        + "\0"
        + canonical_identity(value, binding.canonicaliser)
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def snapshot_index(binding: FrameIdentity, table: Any, key: bytes) -> dict[str, str]:
    """Rebuild from this snapshot, including newly created rows; persist MACs only."""
    if table is None or table.mode != "compare" or binding.column not in table.columns:
        raise FrameIdentityError("identity table or column is unavailable")
    if set(table.values) != set(table.rows):
        raise FrameIdentityError("identity snapshot has not retained every row")
    result: dict[str, str] = {}
    for row_key, row in table.values.items():
        token = identity_token(binding, row.get(binding.column), key)
        if token in result:
            raise FrameIdentityError("stored identity is not unique")
        result[token] = row_key
    return result


def match_records(
    binding: FrameIdentity,
    records: Sequence[Mapping[str, Any]],
    index: Mapping[str, str],
    key: bytes,
) -> list[str]:
    tokens = [
        identity_token(binding, r.get(binding.surface_field), key) for r in records
    ]
    if len(set(tokens)) != len(tokens):
        raise FrameIdentityError("reader identity is not one-to-one")
    if any(token not in index for token in tokens):
        raise FrameIdentityError(
            "reader identity is absent from the current frame snapshot"
        )
    return [index[token] for token in tokens]
