"""Zero-model deterministic cross-site FormPack compiler (induce_form_pack).

Compiles observed structural snapshots of target forms and optional summary
screens into declarative ``FormPack`` contracts using canonical domain schemas,
deterministic synonym/token scoring, fail-closed ambiguity detection, and strict
PII + volatile-ID scrubbing. Zero application or vendor names.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.form_tx.codecs import collapse_whitespace
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    FormSnapshot,
    ObservedControl,
    OutputExtractionSpec,
    SummaryCheckSpec,
    WizardScreenSpec,
)

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_PAREN_HINT_RE = re.compile(r"\([^)]*\)")
_TRAILING_LABEL_PUNCT_RE = re.compile(r"[\s*:]+$")
_VOLATILE_PREFIX_DIGITS_RE = re.compile(
    r"^(?:dynamic|generated|uuid|guid|[a-z]{1,4})\d+$",
    flags=re.IGNORECASE,
)
_LEADING_VOLATILE_CAMEL_RE = re.compile(
    r"^(?:dyn|dynamic|idt|uid|gen|auto|tmp|ctl|fld|ctrl)\d+(?=[A-Z])"
)
_SEMANTIC_ORDINAL_TOKENS = frozenset(
    {"line1", "line2", "addr1", "addr2", "address1", "address2", "phone1", "phone2"}
)
_VOLATILE_EXACT_TOKENS = frozenset(
    {
        "dyn",
        "dynamic",
        "idt",
        "uid",
        "uuid",
        "guid",
        "gen",
        "generated",
        "auto",
        "tmp",
        "temp",
        "rand",
        "seq",
        "idx",
        "j",
    }
)
_GENERIC_UI_CHROME_TOKENS = frozenset(
    {
        "input",
        "field",
        "control",
        "widget",
        "element",
        "form",
        "txt",
        "sel",
        "chk",
        "rad",
        "cb",
        "rb",
        "lbl",
        "val",
        "box",
    }
)
_SECONDARY_ROLE_QUALIFIERS = frozenset(
    {
        "emergency",
        "guarantor",
        "subscriber",
        "employer",
        "referring",
        "physician",
        "provider",
        "coapplicant",
        "secondary",
        "mailing",
        "previous",
        "prior",
        "guardian",
        "witness",
    }
)


class CanonicalFieldSpec(BaseModel):
    """Canonical domain field or expected output definition for pack induction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dossier_key: str = Field(min_length=1)
    synonyms: tuple[str, ...]
    expected_codec: FieldCodecSpec = Field(default_factory=FieldCodecSpec)
    section_hints: tuple[str, ...] = ()
    required: bool = False
    triggers_re_render_hint: bool = False


class CanonicalDomainSchema(BaseModel):
    """Domain-level field dictionary shared across distinct target portals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str = Field(min_length=1)
    fields: tuple[CanonicalFieldSpec, ...]
    expected_outputs: tuple[CanonicalFieldSpec, ...] = ()


class PackInductionReport(BaseModel):
    """Static compilation report and draft pack; not live qualification evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack: FormPack
    matched_keys: tuple[str, ...]
    unmatched_required_keys: tuple[str, ...]
    ambiguous_controls: tuple[str, ...]
    unmapped_portal_required_labels: tuple[str, ...]
    certified_ready: bool


def build_patient_registration_domain_schema() -> CanonicalDomainSchema:
    """The patient registration preset (``presets/patient_registration.json``)."""
    from openadapt_flow.form_tx.presets import load_domain_schema

    return load_domain_schema("patient_registration")


def build_property_quote_domain_schema() -> CanonicalDomainSchema:
    """Build generic residential/commercial property quote domain schema."""
    return CanonicalDomainSchema(
        schema_id="property_quote.v1",
        fields=(
            CanonicalFieldSpec(
                dossier_key="applicant_first_name",
                synonyms=(
                    "applicant first name",
                    "insured first name",
                    "primary insured first name",
                    "first name",
                    "given name",
                    "applicant_first_name",
                    "insured_fname",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                section_hints=("applicant", "insured", "policyholder", "client"),
                required=True,
            ),
            CanonicalFieldSpec(
                dossier_key="applicant_last_name",
                synonyms=(
                    "applicant last name",
                    "insured last name",
                    "primary insured last name",
                    "last name",
                    "surname",
                    "applicant_last_name",
                    "insured_lname",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                section_hints=("applicant", "insured", "policyholder", "client"),
                required=True,
            ),
            CanonicalFieldSpec(
                dossier_key="effective_date",
                synonyms=(
                    "effective date",
                    "policy effective date",
                    "coverage start date",
                    "inception date",
                    "effective_date",
                ),
                expected_codec=FieldCodecSpec(
                    kind=FormCodecKind.ISO_DATE, date_order="MDY"
                ),
                section_hints=("policy", "term", "coverage", "quote"),
                required=False,
            ),
            CanonicalFieldSpec(
                dossier_key="property_street_address",
                synonyms=(
                    "property street address",
                    "property address",
                    "risk address",
                    "location address",
                    "dwelling address",
                    "street address",
                    "premises address",
                    "property_address",
                    "risk_street",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                section_hints=("property", "location", "risk", "dwelling", "premises"),
                required=True,
                triggers_re_render_hint=True,
            ),
            CanonicalFieldSpec(
                dossier_key="property_city",
                synonyms=(
                    "property city",
                    "risk city",
                    "location city",
                    "city",
                    "property_city",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                section_hints=("property", "location", "risk", "dwelling"),
                required=False,
            ),
            CanonicalFieldSpec(
                dossier_key="property_state",
                synonyms=(
                    "property state",
                    "risk state",
                    "location state",
                    "state",
                    "province",
                    "property_state",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                section_hints=("property", "location", "risk", "dwelling"),
                required=False,
                triggers_re_render_hint=True,
            ),
            CanonicalFieldSpec(
                dossier_key="property_postal_code",
                synonyms=(
                    "property postal code",
                    "property zip",
                    "risk zip",
                    "location zip",
                    "postal code",
                    "zip code",
                    "zip",
                    "property_postal_code",
                    "risk_zip",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                section_hints=("property", "location", "risk", "dwelling"),
                required=True,
                triggers_re_render_hint=True,
            ),
            CanonicalFieldSpec(
                dossier_key="year_built",
                synonyms=(
                    "year built",
                    "construction year",
                    "yr built",
                    "year of construction",
                    "year_built",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                section_hints=(
                    "construction",
                    "building",
                    "dwelling",
                    "property",
                    "risk",
                ),
                required=True,
            ),
            CanonicalFieldSpec(
                dossier_key="square_footage",
                synonyms=(
                    "square footage",
                    "total square feet",
                    "living area",
                    "heated sq ft",
                    "sq ft",
                    "sqft",
                    "square_footage",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                section_hints=("construction", "building", "dwelling", "property"),
                required=False,
            ),
            CanonicalFieldSpec(
                dossier_key="construction_type",
                synonyms=(
                    "construction type",
                    "frame type",
                    "building construction",
                    "structure type",
                    "exterior construction",
                    "construction_type",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                section_hints=("construction", "building", "dwelling"),
                required=False,
                triggers_re_render_hint=True,
            ),
            CanonicalFieldSpec(
                dossier_key="roof_material",
                synonyms=(
                    "roof material",
                    "roof type",
                    "roof covering",
                    "roofing",
                    "roof_material",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                section_hints=("construction", "roof", "building", "dwelling"),
                required=False,
            ),
            CanonicalFieldSpec(
                dossier_key="dwelling_coverage_amount",
                synonyms=(
                    "dwelling coverage amount",
                    "dwelling coverage",
                    "coverage a limit",
                    "coverage a",
                    "replacement cost",
                    "building limit",
                    "insured value",
                    "dwelling limit",
                    "dwelling_coverage",
                    "coverage_amount",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                section_hints=("coverage", "limits", "rating", "valuation"),
                required=True,
                triggers_re_render_hint=True,
            ),
            CanonicalFieldSpec(
                dossier_key="deductible_amount",
                synonyms=(
                    "deductible amount",
                    "all peril deductible",
                    "policy deductible",
                    "deductible",
                    "deductible_amount",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                section_hints=("coverage", "limits", "deductible", "rating"),
                required=False,
            ),
            CanonicalFieldSpec(
                dossier_key="monitored_alarm",
                synonyms=(
                    "monitored alarm",
                    "central station alarm",
                    "protective alarm",
                    "security alarm",
                    "burglar alarm",
                    "monitored_alarm",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
                section_hints=("discounts", "protection", "safety", "property"),
                required=False,
            ),
        ),
        expected_outputs=(
            CanonicalFieldSpec(
                dossier_key="quote_number",
                synonyms=(
                    "quote number",
                    "quote id",
                    "proposal number",
                    "quote reference",
                    "quote_number",
                    "quote_id",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE),
                required=True,
            ),
            CanonicalFieldSpec(
                dossier_key="annual_premium",
                synonyms=(
                    "annual premium",
                    "total premium",
                    "quoted premium",
                    "estimated annual premium",
                    "premium",
                    "annual_premium",
                    "quoted_premium",
                ),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                required=True,
            ),
        ),
    )


def _scrub_snapshot_values(snapshot: FormSnapshot) -> FormSnapshot:
    """Return a value-scrubbed copy of ``snapshot`` so zero PII can enter a pack."""
    scrubbed_controls = tuple(
        ctrl.model_copy(
            update={
                "current_value": "",
                "checked": None,
                "error_text": "",
                "options": (),
            }
        )
        for ctrl in snapshot.controls
    )
    return snapshot.model_copy(
        update={
            "controls": scrubbed_controls,
            "entity_rows": (),
            "active_modals": (),
            "banner_errors": (),
        }
    )


def _sanitize_url_path(raw_url: str) -> str:
    cleaned = raw_url.strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if parts.scheme and parts.hostname:
        port_suffix = f":{parts.port}" if parts.port is not None else ""
        path = parts.path or "/"
        return f"{parts.scheme}://{parts.hostname}{port_suffix}{path}"
    return parts.path


def _is_volatile_chunk(chunk: str) -> bool:
    if not chunk:
        return True
    lower = chunk.lower()
    if lower in _SEMANTIC_ORDINAL_TOKENS:
        return False
    if lower.isdigit():
        return True
    if lower in _VOLATILE_EXACT_TOKENS:
        return True
    if _VOLATILE_PREFIX_DIGITS_RE.fullmatch(lower):
        return True
    if (
        len(lower) >= 6
        and re.fullmatch(r"[0-9a-f]+", lower)
        and any(ch.isdigit() for ch in lower)
    ):
        return True
    return False


def _extract_stable_attr_tokens(raw_attr: str) -> tuple[str, ...]:
    """Extract non-volatile lowercase identifier tokens from ``name`` or ``id``."""
    if not raw_attr or not raw_attr.strip():
        return ()
    tokens: list[str] = []
    for raw_chunk in _NON_ALNUM_SPLIT_RE.split(raw_attr.strip()):
        if not raw_chunk:
            continue
        trimmed = _LEADING_VOLATILE_CAMEL_RE.sub("", raw_chunk)
        if _is_volatile_chunk(trimmed):
            continue
        if trimmed.lower() in _SEMANTIC_ORDINAL_TOKENS:
            tokens.append(trimmed.lower())
            continue
        subparts = _CAMEL_BOUNDARY_RE.split(trimmed)
        for sub in subparts:
            if not sub or _is_volatile_chunk(sub):
                continue
            low = sub.lower()
            # Strip trailing numeric counters from non-ordinal tokens (e.g. fname2 -> fname)
            if low not in _SEMANTIC_ORDINAL_TOKENS:
                low = re.sub(r"\d+$", "", low)
            if low and not _is_volatile_chunk(low):
                tokens.append(low)
    return tuple(tokens)


def _semantic_core_attr_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    filtered = [t for t in tokens if t not in _GENERIC_UI_CHROME_TOKENS]
    return tuple(filtered) if filtered else tuple(tokens)


def _tokens_to_attr_regex(tokens: Sequence[str]) -> Optional[str]:
    if not tokens:
        return None
    joined = r"[^a-z]*".join(re.escape(tok) for tok in tokens)
    last_tok = tokens[-1]
    end_boundary = (
        r"(?:$|[^a-z0-9])" if any(c.isdigit() for c in last_tok) else r"(?:$|[^a-z])"
    )
    return rf"(?:^|[^a-z]){joined}{end_boundary}"


def _clean_display_label(raw_label: str) -> str:
    collapsed = collapse_whitespace(raw_label)
    return _TRAILING_LABEL_PUNCT_RE.sub("", collapsed).strip()


def _normalize_phrase(text: str) -> str:
    words = [w.lower() for w in _NON_ALNUM_SPLIT_RE.split(text) if w]
    return " ".join(words)


def _normalize_core_label(raw_label: str) -> str:
    without_parens = _PAREN_HINT_RE.sub(" ", raw_label)
    norm = _normalize_phrase(without_parens)
    # Strip trailing "required" or "optional" badge words
    norm = re.sub(r"\b(?:required|optional)$", "", norm).strip()
    return collapse_whitespace(norm)


def _is_action_button(ctrl: ObservedControl) -> bool:
    tag_cf = ctrl.tag.casefold()
    itype_cf = ctrl.input_type.casefold()
    role_cf = ctrl.role.casefold()
    if role_cf in {"combobox", "listbox", "checkbox", "radio", "switch", "textbox"}:
        return False
    return (
        tag_cf == "button"
        or itype_cf in {"submit", "button", "reset"}
        or role_cf == "button"
    )


def infer_control_kind(ctrl: ObservedControl) -> ControlKind:
    """Deterministically infer ``ControlKind`` from structural element metadata."""
    if ctrl.readonly:
        return ControlKind.READONLY_OUTPUT
    role_cf = ctrl.role.casefold()
    tag_cf = ctrl.tag.casefold()
    itype_cf = ctrl.input_type.casefold()

    if role_cf == "combobox":
        return ControlKind.PORTAL_COMBOBOX
    if tag_cf == "select" or role_cf == "listbox":
        return ControlKind.SELECT
    if itype_cf == "date":
        return ControlKind.DATE
    if itype_cf == "checkbox" or role_cf in {"checkbox", "switch"}:
        return ControlKind.CHECKBOX
    if itype_cf == "radio" or role_cf in {"radio", "radiogroup"}:
        return ControlKind.RADIO_GROUP
    return ControlKind.TEXT


def _spec_allowed_qualifiers(spec: CanonicalFieldSpec) -> set[str]:
    corpus = " ".join((spec.dossier_key, *spec.synonyms, *spec.section_hints))
    words = set(_normalize_phrase(corpus).split())
    return words & _SECONDARY_ROLE_QUALIFIERS


def _score_control_for_spec(ctrl: ObservedControl, spec: CanonicalFieldSpec) -> int:
    """Return deterministic non-negative match score between ``ctrl`` and ``spec``."""
    label_full = _normalize_phrase(ctrl.label_text)
    label_core = _normalize_core_label(ctrl.label_text)
    section_norm = _normalize_phrase(ctrl.section_text)

    name_tokens = _semantic_core_attr_tokens(
        _extract_stable_attr_tokens(ctrl.name_attr)
    )
    id_tokens = _semantic_core_attr_tokens(_extract_stable_attr_tokens(ctrl.id_attr))
    attr_sequences = [seq for seq in (name_tokens, id_tokens) if seq]

    # Guard against hijacking secondary-entity fields (e.g., emergency contact,
    # referring physician, guarantor) onto primary-entity specs.
    ctrl_words = set(label_full.split()) | set(section_norm.split())
    for seq in attr_sequences:
        ctrl_words.update(seq)
    disallowed_qualifiers = (
        ctrl_words & _SECONDARY_ROLE_QUALIFIERS
    ) - _spec_allowed_qualifiers(spec)
    if disallowed_qualifiers:
        return 0

    candidate_synonyms = list(spec.synonyms)
    key_tail = spec.dossier_key.split(".")[-1].replace("_", " ")
    if key_tail not in candidate_synonyms:
        candidate_synonyms.append(key_tail)

    best_base = 0
    for raw_syn in candidate_synonyms:
        syn_norm = _normalize_phrase(raw_syn)
        if not syn_norm:
            continue
        syn_tokens = tuple(syn_norm.split())
        syn_compact = "".join(syn_tokens)

        # Tier 1: Exact label match
        if label_core and (label_core == syn_norm or label_full == syn_norm):
            best_base = max(best_base, 100 + min(len(syn_tokens), 4) * 2)
            continue

        # Tier 2: Exact stable attribute token match
        for seq in attr_sequences:
            seq_norm = " ".join(seq)
            seq_compact = "".join(seq)
            if seq_norm == syn_norm or (
                len(syn_compact) >= 3 and seq_compact == syn_compact
            ):
                best_base = max(best_base, 90 + min(len(syn_tokens), 4) * 2)

        # Tier 3: Whole-word phrase containment in label
        if label_full and re.search(rf"\b{re.escape(syn_norm)}\b", label_full):
            best_base = max(best_base, 74 + min(len(syn_tokens), 4) * 3)

        # Tier 4: All synonym tokens present in stable attribute tokens
        for seq in attr_sequences:
            seq_set = set(seq)
            if syn_tokens and all(tok in seq_set for tok in syn_tokens):
                best_base = max(best_base, 68 + min(len(syn_tokens), 4) * 2)

    if best_base == 0:
        return 0

    section_bonus = 0
    if section_norm and spec.section_hints:
        for hint in spec.section_hints:
            hint_norm = _normalize_phrase(hint)
            if hint_norm and re.search(rf"\b{re.escape(hint_norm)}\b", section_norm):
                section_bonus = 12
                break

    return best_base + section_bonus


def _build_structural_anchor(ctrl: ObservedControl) -> Optional[FieldAnchorSpec]:
    """Construct a value-free, volatile-ID-free ``FieldAnchorSpec`` for ``ctrl``."""
    clean_label = _clean_display_label(ctrl.label_text)
    label_patterns: tuple[str, ...] = (
        (rf"^{re.escape(clean_label)}[\s*:]*$",) if clean_label else ()
    )

    attr_patterns_list: list[str] = []
    for raw_attr in (ctrl.name_attr, ctrl.id_attr):
        tokens = _extract_stable_attr_tokens(raw_attr)
        pat = _tokens_to_attr_regex(tokens)
        if pat and pat not in attr_patterns_list:
            attr_patterns_list.append(pat)
    name_or_id_patterns = tuple(attr_patterns_list)

    clean_section = _clean_display_label(ctrl.section_text)
    section_patterns: tuple[str, ...] = (
        (rf"^{re.escape(clean_section)}[\s*:]*$",) if clean_section else ()
    )

    role_val = ctrl.role.strip() or None

    if not label_patterns and not name_or_id_patterns:
        return None

    return FieldAnchorSpec(
        label_patterns=label_patterns,
        name_or_id_patterns=name_or_id_patterns,
        section_patterns=section_patterns,
        css_selector=None,
        role=role_val,
    )


def _describe_control(ctrl: ObservedControl) -> str:
    label = _clean_display_label(ctrl.label_text)
    if label:
        return f"{ctrl.control_id} ({label})"
    return f"{ctrl.control_id} ({ctrl.dom_selector})"


def _find_button_anchor(
    snapshot: FormSnapshot,
    pattern: str,
) -> Optional[FieldAnchorSpec]:
    for ctrl in snapshot.controls:
        if not ctrl.visible or ctrl.disabled or not _is_action_button(ctrl):
            continue
        button_text = _clean_display_label(ctrl.label_text)
        attr_tokens = " ".join(
            _extract_stable_attr_tokens(f"{ctrl.name_attr} {ctrl.id_attr}")
        )
        probe = f"{button_text} {attr_tokens}".strip()
        if probe and re.search(pattern, probe, flags=re.IGNORECASE):
            anchor = _build_structural_anchor(ctrl)
            if anchor is not None:
                try:
                    matched = match_anchor_in_snapshot(anchor, snapshot)
                except AmbiguousAnchorError:
                    continue
                if matched is not None and matched.control_id == ctrl.control_id:
                    return anchor
    return None


def _induce_wizard_screens(
    snapshots: Sequence[FormSnapshot],
    *,
    commit_button_pattern: str,
    next_button_pattern: str,
) -> tuple[WizardScreenSpec, ...]:
    screens: list[WizardScreenSpec] = []
    total = len(snapshots)
    for idx, snap in enumerate(snapshots):
        screen_id = snap.screen_id or f"screen_{idx + 1}"
        clean_title = collapse_whitespace(snap.title)
        title_pattern = rf"^{re.escape(clean_title)}$" if clean_title else None

        next_anchor = _find_button_anchor(snap, next_button_pattern)
        commit_anchor = _find_button_anchor(snap, commit_button_pattern)

        is_final = idx == total - 1
        if is_final and commit_anchor is not None:
            screens.append(
                WizardScreenSpec(
                    screen_id=screen_id,
                    title_pattern=title_pattern,
                    next_button_anchor=None,
                    is_commit_screen=True,
                    commit_button_anchor=commit_anchor,
                )
            )
        else:
            screens.append(
                WizardScreenSpec(
                    screen_id=screen_id,
                    title_pattern=title_pattern,
                    next_button_anchor=next_anchor,
                    is_commit_screen=False,
                    commit_button_anchor=None,
                )
            )
    return tuple(screens)


def _induce_summary_check(
    summary_snapshot: FormSnapshot,
    domain: CanonicalDomainSchema,
    ambiguous_accum: list[str],
    unmatched_required_accum: list[str],
) -> SummaryCheckSpec:
    claimed_control_ids: set[str] = set()
    outputs: list[OutputExtractionSpec] = []
    readbacks: list[FieldBindingSpec] = []

    eligible_controls = [
        ctrl
        for ctrl in summary_snapshot.controls
        if ctrl.visible and not _is_action_button(ctrl)
    ]

    # 1. Match expected outputs first (e.g., quote_number, annual_premium, MRN)
    for out_spec in domain.expected_outputs:
        scored: list[tuple[int, ObservedControl]] = []
        for ctrl in eligible_controls:
            if ctrl.control_id in claimed_control_ids:
                continue
            sc = _score_control_for_spec(ctrl, out_spec)
            if sc > 0:
                scored.append((sc, ctrl))
        if not scored:
            if (
                out_spec.required
                and out_spec.dossier_key not in unmatched_required_accum
            ):
                unmatched_required_accum.append(out_spec.dossier_key)
            continue

        scored.sort(key=lambda item: item[0], reverse=True)
        top_score = scored[0][0]
        tied = [ctrl for sc, ctrl in scored if sc == top_score]
        if len(tied) > 1:
            for ctrl in tied:
                entry = f"{out_spec.dossier_key}: {_describe_control(ctrl)}"
                if entry not in ambiguous_accum:
                    ambiguous_accum.append(entry)
            if (
                out_spec.required
                and out_spec.dossier_key not in unmatched_required_accum
            ):
                unmatched_required_accum.append(out_spec.dossier_key)
            continue

        winner = tied[0]
        anchor = _build_structural_anchor(winner)
        if anchor is None:
            if (
                out_spec.required
                and out_spec.dossier_key not in unmatched_required_accum
            ):
                unmatched_required_accum.append(out_spec.dossier_key)
            continue
        try:
            resolved = match_anchor_in_snapshot(
                anchor, summary_snapshot, include_readonly=True
            )
        except AmbiguousAnchorError:
            entry = f"{out_spec.dossier_key}: {_describe_control(winner)}"
            if entry not in ambiguous_accum:
                ambiguous_accum.append(entry)
            if (
                out_spec.required
                and out_spec.dossier_key not in unmatched_required_accum
            ):
                unmatched_required_accum.append(out_spec.dossier_key)
            continue
        if resolved is None or resolved.control_id != winner.control_id:
            if (
                out_spec.required
                and out_spec.dossier_key not in unmatched_required_accum
            ):
                unmatched_required_accum.append(out_spec.dossier_key)
            continue

        claimed_control_ids.add(winner.control_id)
        outputs.append(
            OutputExtractionSpec(
                output_key=out_spec.dossier_key,
                anchor=anchor,
                codec=out_spec.expected_codec,
                required=out_spec.required,
            )
        )

    # 2. Match readonly controls on summary screen to canonical domain fields for read-back
    readonly_controls = [
        ctrl
        for ctrl in eligible_controls
        if ctrl.readonly and ctrl.control_id not in claimed_control_ids
    ]
    for field_spec in domain.fields:
        scored_rb: list[tuple[int, ObservedControl]] = []
        for ctrl in readonly_controls:
            if ctrl.control_id in claimed_control_ids:
                continue
            sc = _score_control_for_spec(ctrl, field_spec)
            if sc > 0:
                scored_rb.append((sc, ctrl))
        if not scored_rb:
            continue

        scored_rb.sort(key=lambda item: item[0], reverse=True)
        top_score = scored_rb[0][0]
        tied_rb = [ctrl for sc, ctrl in scored_rb if sc == top_score]
        if len(tied_rb) > 1:
            for ctrl in tied_rb:
                entry = f"summary.{field_spec.dossier_key}: {_describe_control(ctrl)}"
                if entry not in ambiguous_accum:
                    ambiguous_accum.append(entry)
            continue

        winner = tied_rb[0]
        anchor = _build_structural_anchor(winner)
        if anchor is None:
            continue
        try:
            resolved = match_anchor_in_snapshot(
                anchor, summary_snapshot, include_readonly=True
            )
        except AmbiguousAnchorError:
            entry = f"summary.{field_spec.dossier_key}: {_describe_control(winner)}"
            if entry not in ambiguous_accum:
                ambiguous_accum.append(entry)
            continue
        if resolved is None or resolved.control_id != winner.control_id:
            continue

        claimed_control_ids.add(winner.control_id)
        safe_id = re.sub(r"[^a-zA-Z0-9_]+", "_", field_spec.dossier_key)
        readbacks.append(
            FieldBindingSpec(
                binding_id=f"readback_{safe_id}",
                dossier_key=field_spec.dossier_key,
                anchor=anchor,
                control_kind=ControlKind.READONLY_OUTPUT,
                codec=field_spec.expected_codec,
                required_in_pack=field_spec.required,
            )
        )

    return SummaryCheckSpec(
        summary_url_path=_sanitize_url_path(summary_snapshot.url_origin_and_path)
        or None,
        field_readbacks=tuple(readbacks),
        outputs=tuple(outputs),
    )


_COMMIT_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DYNAMIC_PATH_SEGMENT_RE = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})$",
    flags=re.IGNORECASE,
)


def _normalize_commit_path_pattern(raw_url: str) -> Optional[str]:
    """Turn a recorded save URL into a path-only pattern without record IDs."""
    text = raw_url.strip()
    if not text:
        return None
    path = urlsplit(text).path.strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    segments = [
        "*" if _DYNAMIC_PATH_SEGMENT_RE.match(seg) else seg for seg in path.split("/")
    ]
    return "/".join(segments)


def _derive_commit_url_patterns(
    demo_commit_requests: Sequence[Mapping[str, Any]],
    explicit_patterns: Sequence[str] = (),
) -> tuple[str, ...]:
    """Derive commit endpoint URL patterns from successful demo save writes."""
    patterns: list[str] = []
    for raw in explicit_patterns:
        pat = str(raw).strip()
        if pat and pat not in patterns:
            patterns.append(pat)
    for req in demo_commit_requests:
        method = str(req.get("method", "")).strip().upper()
        if method not in _COMMIT_WRITE_METHODS:
            continue
        status_raw = req.get("status")
        try:
            status = int(status_raw) if status_raw is not None else -1
        except (TypeError, ValueError):
            continue
        if not (200 <= status < 400):
            continue
        raw_url = str(req.get("url") or req.get("path") or "")
        derived = _normalize_commit_path_pattern(raw_url)
        if derived and derived not in patterns:
            patterns.append(derived)
    return tuple(patterns)


def induce_form_pack(
    *,
    pack_id: str,
    ui_profile_id: str,
    domain: CanonicalDomainSchema,
    form_snapshots: Sequence[FormSnapshot],
    summary_snapshot: Optional[FormSnapshot] = None,
    commit_button_pattern: Optional[str] = None,
    next_button_pattern: str = r"next|continue",
    commit_url_patterns: Sequence[str] = (),
    demo_commit_requests: Sequence[Mapping[str, Any]] = (),
) -> PackInductionReport:
    """Deterministically induce a ``FormPack`` from scrubbed structural snapshots."""
    scrubbed_forms = tuple(_scrub_snapshot_values(snap) for snap in form_snapshots)
    scrubbed_summary = (
        _scrub_snapshot_values(summary_snapshot)
        if summary_snapshot is not None
        else None
    )

    # Index editable candidate controls across screens: (screen_idx, ctrl_idx, ctrl)
    editable_by_screen: list[list[tuple[int, ObservedControl]]] = []
    for snap in scrubbed_forms:
        screen_ctrls: list[tuple[int, ObservedControl]] = []
        for c_idx, ctrl in enumerate(snap.controls):
            if (
                not ctrl.visible
                or ctrl.disabled
                or ctrl.readonly
                or _is_action_button(ctrl)
            ):
                continue
            screen_ctrls.append((c_idx, ctrl))
        editable_by_screen.append(screen_ctrls)

    ambiguous_controls: list[str] = []
    ambiguous_spec_keys: set[str] = set()
    ambiguous_ctrl_refs: set[tuple[int, str]] = set()

    # Candidate unambiguous winners per CanonicalFieldSpec:
    # (score, spec_order, screen_idx, ctrl_idx, spec, ctrl)
    candidate_assignments: list[
        tuple[int, int, int, int, CanonicalFieldSpec, ObservedControl]
    ] = []

    for spec_order, spec in enumerate(domain.fields):
        best_overall_score = 0
        best_screen_candidates: list[tuple[int, int, ObservedControl]] = []

        for s_idx, screen_ctrls in enumerate(editable_by_screen):
            scored_on_screen: list[tuple[int, int, ObservedControl]] = []
            for c_idx, ctrl in screen_ctrls:
                sc = _score_control_for_spec(ctrl, spec)
                if sc > 0:
                    scored_on_screen.append((sc, c_idx, ctrl))
            if not scored_on_screen:
                continue

            scored_on_screen.sort(key=lambda item: item[0], reverse=True)
            screen_top = scored_on_screen[0][0]
            tied_on_screen = [
                (c_idx, ctrl)
                for sc, c_idx, ctrl in scored_on_screen
                if sc == screen_top
            ]

            if len(tied_on_screen) > 1:
                # Fail closed: two controls on the same screen scored equally!
                if screen_top >= best_overall_score:
                    ambiguous_spec_keys.add(spec.dossier_key)
                    for _, ctrl in tied_on_screen:
                        ambiguous_ctrl_refs.add((s_idx, ctrl.control_id))
                        entry = f"{spec.dossier_key}: {_describe_control(ctrl)}"
                        if entry not in ambiguous_controls:
                            ambiguous_controls.append(entry)
                continue

            if screen_top > best_overall_score:
                best_overall_score = screen_top
                win_c_idx, win_ctrl = tied_on_screen[0]
                best_screen_candidates = [(s_idx, win_c_idx, win_ctrl)]
            elif screen_top == best_overall_score and best_overall_score > 0:
                win_c_idx, win_ctrl = tied_on_screen[0]
                best_screen_candidates.append((s_idx, win_c_idx, win_ctrl))

        if spec.dossier_key in ambiguous_spec_keys or not best_screen_candidates:
            continue

        # Take the earliest screen when a field's top score occurs unambiguously
        win_s_idx, win_c_idx, win_ctrl = best_screen_candidates[0]
        candidate_assignments.append(
            (best_overall_score, spec_order, win_s_idx, win_c_idx, spec, win_ctrl)
        )

    # Sort highest score first (breaking ties deterministically by domain field order)
    candidate_assignments.sort(key=lambda item: (-item[0], item[1]))

    claimed_controls: set[tuple[int, str]] = set()
    matched_bindings_with_pos: list[tuple[int, int, FieldBindingSpec]] = []
    matched_keys_list: list[str] = []

    # Check if two distinct specs tied with the exact same top score on the exact same control
    top_claims_per_ctrl: dict[
        tuple[int, str], list[tuple[int, CanonicalFieldSpec, ObservedControl]]
    ] = {}
    for score, _, s_idx, _, spec, ctrl in candidate_assignments:
        top_claims_per_ctrl.setdefault((s_idx, ctrl.control_id), []).append(
            (score, spec, ctrl)
        )
    for ctrl_ref, claims in top_claims_per_ctrl.items():
        claims.sort(key=lambda item: item[0], reverse=True)
        top_sc = claims[0][0]
        tied_specs = [sp for sc, sp, _ in claims if sc == top_sc]
        if len(tied_specs) > 1:
            ctrl_obj = claims[0][2]
            ambiguous_ctrl_refs.add(ctrl_ref)
            for sp in tied_specs:
                ambiguous_spec_keys.add(sp.dossier_key)
                entry = f"{sp.dossier_key}: {_describe_control(ctrl_obj)}"
                if entry not in ambiguous_controls:
                    ambiguous_controls.append(entry)

    for score, _, s_idx, c_idx, spec, ctrl in candidate_assignments:
        ctrl_ref = (s_idx, ctrl.control_id)
        if (
            spec.dossier_key in ambiguous_spec_keys
            or ctrl_ref in ambiguous_ctrl_refs
            or ctrl_ref in claimed_controls
        ):
            continue

        anchor = _build_structural_anchor(ctrl)
        if anchor is None:
            continue

        # Self-verify induced anchor against the scrubbed snapshot
        try:
            resolved = match_anchor_in_snapshot(anchor, scrubbed_forms[s_idx])
        except AmbiguousAnchorError:
            ambiguous_spec_keys.add(spec.dossier_key)
            ambiguous_ctrl_refs.add(ctrl_ref)
            entry = f"{spec.dossier_key}: {_describe_control(ctrl)}"
            if entry not in ambiguous_controls:
                ambiguous_controls.append(entry)
            continue

        if resolved is None or resolved.control_id != ctrl.control_id:
            ambiguous_spec_keys.add(spec.dossier_key)
            ambiguous_ctrl_refs.add(ctrl_ref)
            entry = f"{spec.dossier_key}: {_describe_control(ctrl)}"
            if entry not in ambiguous_controls:
                ambiguous_controls.append(entry)
            continue

        claimed_controls.add(ctrl_ref)
        matched_keys_list.append(spec.dossier_key)
        safe_id = re.sub(r"[^a-zA-Z0-9_]+", "_", spec.dossier_key)
        binding = FieldBindingSpec(
            binding_id=f"bind_{safe_id}",
            dossier_key=spec.dossier_key,
            anchor=anchor,
            control_kind=infer_control_kind(ctrl),
            codec=spec.expected_codec,
            triggers_re_render=spec.triggers_re_render_hint,
            required_in_pack=bool(spec.required or ctrl.required),
        )
        matched_bindings_with_pos.append((s_idx, c_idx, binding))

    # Order bindings deterministically in natural screen & DOM traversal order
    matched_bindings_with_pos.sort(key=lambda item: (item[0], item[1]))
    ordered_bindings = tuple(item[2] for item in matched_bindings_with_pos)

    # Preserve domain field order for matched_keys & unmatched_required_keys
    matched_key_set = set(matched_keys_list)
    ordered_matched_keys = tuple(
        spec.dossier_key
        for spec in domain.fields
        if spec.dossier_key in matched_key_set
    )
    unmatched_required: list[str] = [
        spec.dossier_key
        for spec in domain.fields
        if spec.required and spec.dossier_key not in matched_key_set
    ]

    # Surface any required portal controls that matched no CanonicalFieldSpec
    unmapped_portal_required: list[str] = []
    for s_idx, screen_ctrls in enumerate(editable_by_screen):
        for _, ctrl in screen_ctrls:
            ctrl_ref = (s_idx, ctrl.control_id)
            if not ctrl.required:
                continue
            if ctrl_ref in claimed_controls or ctrl_ref in ambiguous_ctrl_refs:
                continue
            label_desc = (
                _clean_display_label(ctrl.label_text)
                or ctrl.name_attr.strip()
                or ctrl.id_attr.strip()
                or ctrl.control_id
            )
            if label_desc not in unmapped_portal_required:
                unmapped_portal_required.append(label_desc)

    screens = _induce_wizard_screens(
        scrubbed_forms,
        commit_button_pattern=commit_button_pattern or r"(?!)",
        next_button_pattern=next_button_pattern,
    )

    summary_check = (
        _induce_summary_check(
            scrubbed_summary,
            domain,
            ambiguous_controls,
            unmatched_required,
        )
        if scrubbed_summary is not None
        else None
    )

    resolved_commit_url_patterns = _derive_commit_url_patterns(
        demo_commit_requests,
        explicit_patterns=commit_url_patterns,
    )

    pack = FormPack(
        pack_id=pack_id,
        domain_schema=domain.schema_id,
        ui_profile_id=ui_profile_id,
        bindings=ordered_bindings,
        screens=screens,
        summary_check=summary_check,
        commit_url_patterns=resolved_commit_url_patterns,
    )

    has_record_lookup = bool(
        summary_check is not None
        and summary_check.search_by_output_key
        and summary_check.search_input_anchor is not None
        and summary_check.search_submit_anchor is not None
    )
    has_commit_evidence = bool(resolved_commit_url_patterns) or has_record_lookup

    certified_ready = (
        bool(ordered_matched_keys)
        and not unmatched_required
        and not ambiguous_controls
        and not unmapped_portal_required
        and commit_button_pattern is not None
        and any(screen.is_commit_screen for screen in screens)
        and has_commit_evidence
        and summary_check is not None
        and bool(summary_check.field_readbacks)
        and {binding.dossier_key for binding in ordered_bindings}
        <= {binding.dossier_key for binding in summary_check.field_readbacks}
    )

    return PackInductionReport(
        pack=pack,
        matched_keys=ordered_matched_keys,
        unmatched_required_keys=tuple(unmatched_required),
        ambiguous_controls=tuple(ambiguous_controls),
        unmapped_portal_required_labels=tuple(unmapped_portal_required),
        certified_ready=certified_ready,
    )
