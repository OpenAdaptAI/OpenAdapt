"""Domain-agnostic data types for automated pack onboarding and qualification.

Zero app-specific knowledge lives here: every table, column, parameter, and
write action is described declaratively in :class:`OnboardSpec`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

import yaml

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _SAFE_IDENT.match(name):
        raise ValueError(
            f"{what} {name!r} must be a plain SQL identifier (^[A-Za-z_][A-Za-z0-9_]*$)"
        )
    return name


@dataclass(frozen=True)
class KindOnboardingSpec:
    """Declarative specification of one write kind for a system of record."""

    kind: str
    mode: Literal["create", "update"]
    table: str
    identity_field: str = "id"
    required_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    identity_params: tuple[str, ...] = ()
    change_fields: tuple[str, ...] = ()
    column_map: Mapping[str, str] = field(default_factory=dict)
    invariant_pins: Mapping[str, str] = field(default_factory=dict)
    bookkeeping_fields: tuple[str, ...] = ()
    filter_params: tuple[str, ...] = ()
    demonstrated_field_sets: tuple[tuple[str, ...], ...] = ()
    sample_values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_ident(self.kind, "kind")
        if self.mode not in ("create", "update"):
            raise ValueError(f"kind {self.kind!r}: mode must be 'create' or 'update'")
        _validate_ident(self.table, f"kind {self.kind!r} table")
        _validate_ident(self.identity_field, f"kind {self.kind!r} identity_field")
        for f in (
            *self.required_fields,
            *self.optional_fields,
            *self.identity_params,
            *self.change_fields,
            *self.bookkeeping_fields,
            *self.filter_params,
        ):
            _validate_ident(f, f"kind {self.kind!r} field")
        for param, col in self.column_map.items():
            _validate_ident(param, f"kind {self.kind!r} column_map key")
            _validate_ident(col, f"kind {self.kind!r} column_map value")
        for col in self.invariant_pins:
            _validate_ident(col, f"kind {self.kind!r} invariant_pins key")
        if self.mode == "create" and not self.required_fields:
            raise ValueError(f"create kind {self.kind!r} requires required_fields")
        if self.mode == "update" and (
            not self.identity_params or not self.change_fields
        ):
            raise ValueError(
                f"update kind {self.kind!r} requires identity_params and change_fields"
            )
        if not self.demonstrated_field_sets:
            raise ValueError(
                f"kind {self.kind!r} requires at least one demonstrated_field_sets entry"
            )

    def column_for(self, param: str) -> str:
        return str(self.column_map.get(param, param))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KindOnboardingSpec":
        return cls(
            kind=str(data["kind"]),
            mode=data["mode"],
            table=str(data["table"]),
            identity_field=str(data.get("identity_field") or "id"),
            required_fields=tuple(str(x) for x in (data.get("required_fields") or ())),
            optional_fields=tuple(str(x) for x in (data.get("optional_fields") or ())),
            identity_params=tuple(str(x) for x in (data.get("identity_params") or ())),
            change_fields=tuple(str(x) for x in (data.get("change_fields") or ())),
            column_map={
                str(k): str(v) for k, v in (data.get("column_map") or {}).items()
            },
            invariant_pins={
                str(k): str(v) for k, v in (data.get("invariant_pins") or {}).items()
            },
            bookkeeping_fields=tuple(
                str(x) for x in (data.get("bookkeeping_fields") or ())
            ),
            filter_params=tuple(str(x) for x in (data.get("filter_params") or ())),
            demonstrated_field_sets=tuple(
                tuple(str(f) for f in fs)
                for fs in (data.get("demonstrated_field_sets") or ())
            ),
            sample_values=dict(data.get("sample_values") or {}),
        )


@dataclass(frozen=True)
class OnboardSpec:
    """Complete declarative onboarding input for one system of record."""

    name: str
    read: Mapping[str, Any]
    kinds: tuple[KindOnboardingSpec, ...]
    actor_id: str = "onboard-verifier"

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("OnboardSpec.name must be non-empty")
        if not self.read:
            raise ValueError("OnboardSpec.read must configure a read-back connection")
        if not self.kinds:
            raise ValueError("OnboardSpec.kinds must contain at least one kind")
        seen: set[str] = set()
        for k in self.kinds:
            if k.kind in seen:
                raise ValueError(f"duplicate kind {k.kind!r} in OnboardSpec")
            seen.add(k.kind)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OnboardSpec":
        return cls(
            name=str(data["name"]),
            read=dict(data["read"]),
            kinds=tuple(KindOnboardingSpec.from_dict(k) for k in data["kinds"]),
            actor_id=str(data.get("actor_id") or "onboard-verifier"),
        )

    @classmethod
    def load(cls, path: str | Path) -> "OnboardSpec":
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if p.suffix.lower() == ".json" else yaml.safe_load(raw)
        if not isinstance(data, Mapping):
            raise ValueError(f"{p} must contain a mapping")
        return cls.from_dict(data)


@dataclass(frozen=True)
class CleanWriteCase:
    """One clean write executed against the target system of record."""

    kind: str
    values: Mapping[str, Any]
    apply_write: Callable[[], None]
    name: str = ""
    expect_found: bool = False


@dataclass(frozen=True)
class PlantedFaultOutcome:
    """Result of one deterministic planted-fault check."""

    kind: str
    fault_class: str
    field: str
    outcome: str
    caught: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fault_class": self.fault_class,
            "field": self.field,
            "outcome": self.outcome,
            "caught": self.caught,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CleanWriteOutcome:
    """Result of one clean write verification check."""

    kind: str
    name: str
    fields: tuple[str, ...]
    composed: bool
    outcome: str
    confirmed: bool
    receipt_path: Optional[str] = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "fields": list(self.fields),
            "composed": self.composed,
            "outcome": self.outcome,
            "confirmed": self.confirmed,
            "receipt_path": self.receipt_path,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class QualificationResult:
    """Structured qualification verdict for an onboarded pack."""

    pack_name: str
    certified: bool
    refusal_reasons: tuple[str, ...]
    pack_dir: str
    manifest_path: str
    pack_digest: str
    planted_faults_caught: int
    planted_faults_total: int
    clean_writes_verified: int
    clean_writes_total: int
    false_stop_rate: float
    receipts_issued: int
    receipt_paths: tuple[str, ...]
    fault_outcomes: tuple[PlantedFaultOutcome, ...] = ()
    clean_outcomes: tuple[CleanWriteOutcome, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_name": self.pack_name,
            "certified": self.certified,
            "refusal_reasons": list(self.refusal_reasons),
            "pack_dir": self.pack_dir,
            "manifest_path": self.manifest_path,
            "pack_digest": self.pack_digest,
            "planted_faults_caught": self.planted_faults_caught,
            "planted_faults_total": self.planted_faults_total,
            "clean_writes_verified": self.clean_writes_verified,
            "clean_writes_total": self.clean_writes_total,
            "false_stop_rate": self.false_stop_rate,
            "receipts_issued": self.receipts_issued,
            "receipt_paths": list(self.receipt_paths),
            "fault_outcomes": [f.to_dict() for f in self.fault_outcomes],
            "clean_outcomes": [c.to_dict() for c in self.clean_outcomes],
        }
