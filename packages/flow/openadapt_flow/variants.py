"""Variant sets: separately admitted variants of one workflow, with a router.

One workflow is often several programs. One clinic's EMR has custom fields
another lacks, one payer's claims take a different path, a dark-theme VDI
renders differently. ``induce`` folds variants that share structure into one
program with guarded branches. A variant set is for the cases that do not
share structure: each variant is its own bundle with its own
``QualificationAdmissionEnvelope``, and a deterministic pre-run router picks
exactly one of them from the item's inputs and a fingerprint of the first
screen. The router makes no model call. When no variant matches, it halts with
a named reason; it never guesses.

Schema: ``openadapt.variant-set/v0``. Filename: ``variant-set.json``. The
runtime lives in :mod:`openadapt_flow.runtime.variant_router`.

Conditions must be statically disjoint: for every pair of variants the
authoring check proves that no input and screen state can satisfy both. A
pair is disjoint when they constrain the same input to non-overlapping value
sets, require different themes, or one requires a text marker that the other
requires absent. Anything else is refused, so a routing decision can never
depend on declaration order.
"""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
from typing import Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.admitted_composition import (
    AdmittedChildSpec,
    ProcessContractError,
    _load_admitted_bundle,
    _pointer_for,
    live_bundle_content_digest,
)
from openadapt_flow.qualification_admission import QualificationAdmissionEnvelope

VARIANT_SET_FILENAME = "variant-set.json"
VARIANT_SET_SCHEMA: Literal["openadapt.variant-set/v0"] = "openadapt.variant-set/v0"
_NAME_RE = r"^[A-Za-z][A-Za-z0-9_-]*$"
_MARKER_MAX = 128

Theme = Literal["light", "dark"]


class VariantSetError(ValueError):
    """Authoring or load refusal for a variant-set artifact."""


class VariantWhen(BaseModel):
    """When a variant applies: item inputs plus a first-screen fingerprint.

    Every listed condition must hold. ``inputs`` maps an input name to the
    values it may take (exact string comparison). ``theme`` compares the
    first screen's luminance class. ``text_present`` / ``text_absent`` are
    OCR presence checks for markers such as an application version string or
    a tenant banner.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, list[str]] = Field(default_factory=dict)
    theme: Optional[Theme] = None
    text_present: list[str] = Field(default_factory=list)
    text_absent: list[str] = Field(default_factory=list)

    @field_validator("inputs")
    @classmethod
    def _input_values(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        import re

        for name, allowed in value.items():
            if not re.match(_NAME_RE, name):
                raise ValueError(f"input name {name!r} is not a parameter name")
            if not allowed:
                raise ValueError(f"input {name!r} lists no allowed value")
            if len(allowed) != len(set(allowed)):
                raise ValueError(f"input {name!r} repeats an allowed value")
        return {name: list(allowed) for name, allowed in sorted(value.items())}

    @field_validator("text_present", "text_absent")
    @classmethod
    def _markers(cls, value: list[str]) -> list[str]:
        for marker in value:
            if not marker.strip() or len(marker) > _MARKER_MAX:
                raise ValueError(
                    f"text markers must be non-empty and at most {_MARKER_MAX} "
                    "characters"
                )
        if len(value) != len(set(value)):
            raise ValueError("text markers must not repeat")
        return list(value)

    @model_validator(mode="after")
    def _consistent(self) -> "VariantWhen":
        if not (self.inputs or self.theme or self.text_present or self.text_absent):
            raise ValueError(
                "a variant needs at least one condition; an unconditional "
                "variant would make every routing decision a guess"
            )
        both = set(self.text_present) & set(self.text_absent)
        if both:
            raise ValueError(f"markers {sorted(both)} are both required and forbidden")
        return self

    @property
    def uses_screen(self) -> bool:
        return bool(self.theme or self.text_present or self.text_absent)

    @property
    def markers(self) -> list[str]:
        return [*self.text_present, *self.text_absent]


class VariantPromotion(BaseModel):
    """Lineage of a variant promoted from a repaired run."""

    model_config = ConfigDict(extra="forbid")

    derived_from: str = Field(pattern=_NAME_RE)
    repair_candidate_id: Optional[str] = Field(default=None, max_length=64)
    repair_candidate_state: Optional[str] = Field(default=None, max_length=32)


class VariantSpec(AdmittedChildSpec):
    """One admitted variant: the process child pointer plus its conditions."""

    when: VariantWhen
    promotion: Optional[VariantPromotion] = None


def disjointness_witness(a: VariantWhen, b: VariantWhen) -> Optional[str]:
    """Return why ``a`` and ``b`` can never both hold, or ``None``."""

    for name in sorted(set(a.inputs) & set(b.inputs)):
        if not set(a.inputs[name]) & set(b.inputs[name]):
            return f"input {name!r} takes different values"
    if a.theme and b.theme and a.theme != b.theme:
        return "themes differ"
    conflict = (set(a.text_present) & set(b.text_absent)) | (
        set(b.text_present) & set(a.text_absent)
    )
    if conflict:
        return (
            f"marker {sorted(conflict)[0]!r} is required by one, forbidden by the other"
        )
    return None


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class VariantSet(BaseModel):
    """Separately admitted variants of one workflow, routed before the run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.variant-set/v0"] = VARIANT_SET_SCHEMA
    name: str = Field(min_length=1, max_length=256)
    variants: list[VariantSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed(self) -> "VariantSet":
        names = [item.name for item in self.variants]
        if len(names) != len(set(names)):
            raise ValueError("variant names must be unique")
        for item in self.variants:
            promotion = item.promotion
            if promotion is not None:
                if promotion.derived_from == item.name:
                    raise ValueError(f"variant {item.name!r} cannot derive from itself")
                if promotion.derived_from not in names:
                    raise ValueError(
                        f"variant {item.name!r} derives from unknown variant "
                        f"{promotion.derived_from!r}"
                    )
        for first, second in combinations(self.variants, 2):
            if disjointness_witness(first.when, second.when) is None:
                raise ValueError(
                    f"variants {first.name!r} and {second.name!r} can both match "
                    "the same item; make their conditions disjoint (different "
                    "values for a shared input, different themes, or a marker one "
                    "requires and the other forbids)"
                )
        return self

    @property
    def uses_screen(self) -> bool:
        return any(item.when.uses_screen for item in self.variants)

    @property
    def markers(self) -> list[str]:
        seen: dict[str, None] = {}
        for item in self.variants:
            for marker in item.when.markers:
                seen.setdefault(marker, None)
        return list(seen)

    def variant(self, name: str) -> VariantSpec:
        for item in self.variants:
            if item.name == name:
                return item
        raise VariantSetError(f"variant set has no variant named {name!r}")

    def digest(self) -> str:
        """SHA-256 over the canonical set, binding every variant and pointer."""

        return _canonical_sha256(self.model_dump(mode="json"))

    def variant_digest(self, name: str) -> str:
        """SHA-256 over one variant's canonical spec."""

        return _canonical_sha256(self.variant(name).model_dump(mode="json"))

    def save(self, parent_dir: Path | str) -> Path:
        parent = Path(parent_dir)
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / VARIANT_SET_FILENAME
        path.write_text(
            json.dumps(self.model_dump(mode="json", exclude_none=True), indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, parent_dir: Path | str) -> "VariantSet":
        parent = Path(parent_dir)
        path = parent / VARIANT_SET_FILENAME
        if not path.is_file():
            raise VariantSetError(
                f"{parent} is not a variant-set artifact (no {VARIANT_SET_FILENAME})"
            )
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise VariantSetError(f"variant set at {path} is invalid: {exc}") from exc


def is_variant_set_artifact(path: Path | str) -> bool:
    """True when ``path`` is a variant-set directory."""

    return (Path(path) / VARIANT_SET_FILENAME).is_file()


class VariantInput(BaseModel):
    """One variant to author: an admitted bundle, its envelope, conditions."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: str = Field(pattern=_NAME_RE)
    bundle: Path
    admission: Path
    when: VariantWhen
    derived_from: Optional[str] = None


def _admitted_spec(item: VariantInput, out_dir: Path) -> VariantSpec:
    """Build a variant spec, refusing anything that is not admitted as is."""

    if not item.admission.is_file():
        raise VariantSetError(
            f"variant {item.name!r} has no QualificationAdmissionEnvelope at "
            f"{item.admission}; each variant keeps its own admission"
        )
    try:
        envelope = QualificationAdmissionEnvelope.model_validate_json(
            item.admission.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise VariantSetError(
            f"variant {item.name!r} envelope is not a valid "
            f"QualificationAdmissionEnvelope ({type(exc).__name__})"
        ) from exc
    try:
        workflow = _load_admitted_bundle(item.name, item.bundle)
    except ProcessContractError as exc:
        raise VariantSetError(str(exc).replace("child", "variant", 1)) from exc
    live = live_bundle_content_digest(workflow, item.bundle)
    payload = envelope.payload
    if payload.bundle_content_digest != live:
        raise VariantSetError(
            f"variant {item.name!r} bundle does not match its admission "
            f"(live {live}, admitted {payload.bundle_content_digest}); admit the "
            "bundle as it is before adding it"
        )
    promotion: Optional[VariantPromotion] = None
    if item.derived_from is not None:
        candidate_id, candidate_state = _repair_candidate(item.bundle)
        promotion = VariantPromotion(
            derived_from=item.derived_from,
            repair_candidate_id=candidate_id,
            repair_candidate_state=candidate_state,
        )
    return VariantSpec(
        name=item.name,
        admission_id=payload.admission_id,
        workflow_version_id=payload.workflow_version_id,
        bundle_content_digest=payload.bundle_content_digest,
        envelope=_pointer_for(item.admission, out_dir),
        bundle=_pointer_for(item.bundle, out_dir),
        surface=workflow.surface,
        when=item.when,
        promotion=promotion,
    )


def _repair_candidate(bundle: Path) -> tuple[Optional[str], Optional[str]]:
    """Read the detached repair-candidate record a healed bundle carries."""

    from openadapt_flow.repair.registration import detached_candidate_path

    path = detached_candidate_path(bundle)
    if not path.is_file():
        return None, None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VariantSetError(
            f"repair candidate record at {path} is unreadable"
        ) from exc
    return str(record.get("candidate_id") or "") or None, (
        str(record.get("state") or "") or None
    )


def _validated(name: str, specs: Sequence[VariantSpec]) -> VariantSet:
    try:
        return VariantSet(name=name, variants=list(specs))
    except ValueError as exc:
        raise VariantSetError(str(exc)) from exc


def author_variant_set(
    variants: Sequence[VariantInput], *, name: str, out: Path | str
) -> VariantSet:
    """Write ``variant-set.json`` that points at independently admitted bundles.

    The set does not copy bundles. Each variant must present an admission
    envelope whose digest matches the live bundle.
    """

    out_dir = Path(out)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise VariantSetError(
            f"variant-set output {out_dir} already exists and is not empty"
        )
    for item in variants:
        if item.derived_from is not None:
            raise VariantSetError(
                "derived_from is recorded when a repaired case is promoted with "
                "`variants add`, not when the set is first authored"
            )
    specs = [_admitted_spec(item, out_dir) for item in variants]
    variant_set = _validated(name, specs)
    variant_set.save(out_dir)
    return variant_set


def add_variant(parent_dir: Path | str, item: VariantInput) -> VariantSet:
    """Add one admitted variant to an existing set (promotion of a repair).

    A repaired run produces a healed bundle and a repair candidate. Once that
    bundle is admitted through the usual qualification flow, this records it
    as a new variant with its own conditions and admission. When
    ``derived_from`` is set and the bundle carries a repair-candidate record,
    the candidate must name the parent variant's admitted bundle as its prior
    revision.
    """

    parent = Path(parent_dir)
    current = VariantSet.load(parent)
    if item.name in {spec.name for spec in current.variants}:
        raise VariantSetError(f"variant {item.name!r} already exists in the set")
    if item.derived_from is not None:
        base = current.variant(item.derived_from)
        _check_lineage(item.bundle, base)
    spec = _admitted_spec(item, parent)
    updated = _validated(current.name, [*current.variants, spec])
    updated.save(parent)
    return updated


def _check_lineage(bundle: Path, base: VariantSpec) -> None:
    from openadapt_flow.repair.registration import detached_candidate_path

    path = detached_candidate_path(bundle)
    if not path.is_file():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VariantSetError(
            f"repair candidate record at {path} is unreadable"
        ) from exc
    prior = record.get("prior_content_digest")
    if prior != base.bundle_content_digest:
        raise VariantSetError(
            f"repair candidate in {bundle} was not derived from variant "
            f"{base.name!r} (prior revision {prior}, variant bundle "
            f"{base.bundle_content_digest})"
        )
