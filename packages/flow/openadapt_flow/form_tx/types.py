"""Typed contracts for the OpenAdapt Governed Form & Wizard Transaction Layer (form_tx).

Zero application or vendor names. Pure declarative schemas for grounded input
dossiers, UI mechanics profiles, form bindings, popup subroutines, epistemic
halts, human co-pilot resolutions, summary read-back verdicts, and signed
receipts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORM_DOSSIER_SCHEMA: Literal["openadapt.form-dossier/v1"] = "openadapt.form-dossier/v1"
UI_PROFILE_SCHEMA: Literal["openadapt.ui-profile/v1"] = "openadapt.ui-profile/v1"
FORM_PACK_SCHEMA: Literal["openadapt.form-pack/v1"] = "openadapt.form-pack/v1"
FORM_RECEIPT_SCHEMA: Literal["openadapt.form-tx-receipt/v1"] = (
    "openadapt.form-tx-receipt/v1"
)
PACK_PATCH_SCHEMA: Literal["openadapt.form-pack-patch/v1"] = (
    "openadapt.form-pack-patch/v1"
)


def canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic canonical UTF-8 JSON bytes for hashing and signing."""
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    """Return hex SHA-256 over canonical JSON representation."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def salted_sha256(value: Any, salt_hex: str) -> str:
    """Return HMAC-SHA256 digest of canonical JSON value using ``salt_hex``."""
    key = bytes.fromhex(salt_hex) if salt_hex else b"openadapt-form-tx"
    return hmac.new(key, canonical_json_bytes(value), hashlib.sha256).hexdigest()


class ProvenanceKind(str, Enum):
    """Grounded authority for any value entered into a target form."""

    INTAKE_DOCUMENT = "intake_document"
    UPSTREAM_QUEUE = "upstream_queue"
    SIGNED_POLICY_DEFAULT = "signed_policy_default"
    HUMAN_OPERATOR = "human_operator"
    ACCEPTED_PORTAL_PREFILL = "accepted_portal_prefill"


class FormCodecKind(str, Enum):
    """Deterministic normalization codecs for form entry and read-back."""

    EXACT = "exact"
    CASEFOLD = "casefold"
    WHITESPACE_COLLAPSE = "whitespace_collapse"
    TITLE_CASE = "title_case"
    UPPER = "upper"
    PHONE_DIGITS = "phone_digits"
    ISO_DATE = "iso_date"
    INTEGER = "integer"
    CURRENCY_CENTS = "currency_cents"
    BOOLEAN_YES_NO = "boolean_yes_no"
    ENUM_MAP = "enum_map"


class ControlKind(str, Enum):
    """Structural UI control archetype."""

    TEXT = "text"
    SELECT = "select"
    PORTAL_COMBOBOX = "portal_combobox"
    RADIO_GROUP = "radio_group"
    CHECKBOX = "checkbox"
    DATE = "date"
    POPUP_LAUNCHER = "popup_launcher"
    READONLY_OUTPUT = "readonly_output"


class PrefillPolicy(str, Enum):
    """Fail-closed policy when a portal pre-populates a control on screen."""

    HALT_ON_CONFLICT = "halt_on_conflict"
    KEEP_IF_MATCHES_ELSE_HALT = "keep_if_matches_else_halt"
    OVERWRITE_WITH_DOSSIER = "overwrite_with_dossier"


class FormHaltKind(str, Enum):
    """Closed taxonomy of fail-closed halts in ``form_tx``."""

    UNGROUNDED_REQUIRED_FIELD = "ungrounded_required_field"
    PREFILL_VALUE_CONFLICT = "prefill_value_conflict"
    PREFILL_ENTITY_CONFLICT = "prefill_entity_conflict"
    AMBIGUOUS_OPTION_OR_TARGET = "ambiguous_option_or_target"
    PORTAL_VALIDATION_BLOCKED = "portal_validation_blocked"
    POPUP_CONTRACT_FAILED = "popup_contract_failed"
    MODAL_CONFIRMATION_REQUIRED = "modal_confirmation_required"
    AUTH_WALL_DETECTED = "auth_wall_detected"
    MUTATION_BUDGET_EXCEEDED = "mutation_budget_exceeded"
    # A sign-in or a jump back interrupted the run after fields were entered,
    # and some of them cannot be re-checked before submit.
    FIELDS_NOT_RECHECKED = "fields_not_rechecked"


class FormTxOutcome(str, Enum):
    """Honest terminal or paused outcome of a ``form_tx`` execution."""

    VERIFIED_DONE = "verified_done"
    HALTED_NEEDS_HUMAN = "halted_needs_human"
    ESCALATED_TO_OWNER = "escalated_to_owner"
    AUTH_REQUIRED = "auth_required"
    VALIDATION_BLOCKED = "validation_blocked"
    READBACK_REFUTED = "readback_refuted"
    # Submitted, and nothing read back contradicted it, but at least one field
    # that was typed, selected or kept could not be read back. Never verified.
    READBACK_INCOMPLETE = "readback_incomplete"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    ABORTED_BY_HUMAN = "aborted_by_human"


class HumanDecisionAction(str, Enum):
    """Governed human resolution actions at a live halt."""

    PROVIDE_VALUE = "provide_value"
    ACCEPT_PORTAL_PREFILL = "accept_portal_prefill"
    DISMISS_PORTAL_ENTITY = "dismiss_portal_entity"
    CONFIRM_MODAL_ACTION = "confirm_modal_action"
    FIXED_ON_SCREEN = "fixed_on_screen"
    ESCALATE_TO_OWNER = "escalate_to_owner"
    ABORT_RUN = "abort_run"


class DossierFact(BaseModel):
    """One grounded input value with explicit provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    value: Any
    raw_value: Optional[str] = None
    provenance: ProvenanceKind
    source_id: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class DossierEntity(BaseModel):
    """Expected repeating entity row (such as a household member or contact)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_kind: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    fields: dict[str, DossierFact] = Field(default_factory=dict)


class InputDossier(BaseModel):
    """Provenance-carrying structured input packet for a form workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.form-dossier/v1"] = FORM_DOSSIER_SCHEMA
    dossier_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    facts: dict[str, DossierFact] = Field(default_factory=dict)
    entities: tuple[DossierEntity, ...] = ()
    min_confidence: float = Field(default=0.9, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_fact_keys(self) -> "InputDossier":
        for map_key, fact in self.facts.items():
            if fact.key != map_key:
                raise ValueError(
                    f"Dossier fact key mismatch: dict key {map_key!r} != fact.key {fact.key!r}"
                )
        return self

    def grounded_fact(self, key: str) -> Optional[DossierFact]:
        """Return ``DossierFact`` only when present, non-empty, and above threshold."""
        fact = self.facts.get(key)
        if fact is None or fact.value is None:
            return None
        if isinstance(fact.value, str) and not fact.value.strip():
            return None
        if fact.confidence < self.min_confidence:
            return None
        return fact

    def entities_of_kind(self, entity_kind: str) -> tuple[DossierEntity, ...]:
        return tuple(e for e in self.entities if e.entity_kind == entity_kind)

    def digest(self) -> str:
        return canonical_sha256(self)


class UIProfileSpec(BaseModel):
    """Data-only profile describing asynchronous UI & validation mechanics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.ui-profile/v1"] = UI_PROFILE_SCHEMA
    profile_id: str = Field(min_length=1)
    description: str = ""
    busy_selectors: tuple[str, ...] = (
        '[aria-busy="true"]',
        '[data-busy="true"]',
    )
    error_banner_selectors: tuple[str, ...] = (
        '[role="alert"]',
        "[data-validation-banner]",
        ".validation-banner",
    )
    field_error_selectors: tuple[str, ...] = (
        '[aria-invalid="true"]',
        "[data-field-error]",
        ".field-error",
    )
    portal_listbox_selectors: tuple[str, ...] = (
        '[role="listbox"]',
        "[data-portal-listbox]",
    )
    portal_option_selectors: tuple[str, ...] = (
        '[role="option"]',
        "[data-option-value]",
    )
    modal_dialog_selectors: tuple[str, ...] = (
        "dialog[open]",
        '[role="dialog"][aria-modal="true"]',
        "[data-modal-open='true']",
    )
    auth_wall_selectors: tuple[str, ...] = (
        'input[type="password"]',
        '[data-auth-wall="true"]',
    )
    authenticated_landmark_selectors: tuple[str, ...] = ()
    # Case-insensitive regular expressions searched in error banners and open
    # dialogs. A match means the session ended: the run stops at a sign-in
    # wall instead of reporting a validation error.
    session_expired_text_patterns: tuple[str, ...] = (
        r"\bsession\b[^.]{0,40}\b(?:has\s+)?(?:expired|timed\s*out|ended)\b",
        r"\b(?:you\s+(?:have\s+been|were)\s+)?(?:signed|logged)\s+out\b",
        r"\b(?:please\s+)?(?:sign|log)\s*(?:in|on)\s+again\b",
        r"\bre-?(?:authenticate|login|log\s+in|sign\s+in)\b",
        r"\b(?:login|sign[- ]?in)\s+(?:has\s+)?expired\b",
    )
    keepalive_path: Optional[str] = None
    debounce_grace_ms: int = Field(default=120, ge=0, le=5000)
    settle_window_ms: int = Field(default=150, ge=20, le=5000)
    max_settle_ms: int = Field(default=12000, ge=200, le=60000)
    frame_path_patterns: tuple[str, ...] = ()
    """Ordered regular expressions, full-matched against frame URL paths, that
    route the surface into one same-origin iframe holding the form. Empty
    means the top-level document. See ``PlaywrightFormSurface``."""

    @field_validator("frame_path_patterns")
    @classmethod
    def _frame_patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if not pattern:
                raise ValueError("frame_path_patterns entries must be non-empty")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"Invalid frame path pattern {pattern!r}: {exc}"
                ) from exc
        return value


class FieldCodecSpec(BaseModel):
    """Declarative codec configuration for a field or extracted output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FormCodecKind = FormCodecKind.WHITESPACE_COLLAPSE
    date_order: Literal["YMD", "MDY", "DMY"] = "YMD"
    currency_symbol: Optional[Literal["$", "€", "£", "₹"]] = None
    enum_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class FieldAnchorSpec(BaseModel):
    """Semantic, non-pixel control locator resilient across DOM re-renders."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label_patterns: tuple[str, ...] = ()
    name_or_id_patterns: tuple[str, ...] = ()
    section_patterns: tuple[str, ...] = ()
    css_selector: Optional[str] = None
    role: Optional[str] = None
    # When several controls match (a results table with one "Select" button
    # per row), keep only the one whose table row has a cell equal to the
    # value of each of these dossier keys. Without facts, or with no single
    # row left, the anchor stays ambiguous.
    row_cell_dossier_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _require_at_least_one_signal(self) -> "FieldAnchorSpec":
        if (
            not self.label_patterns
            and not self.name_or_id_patterns
            and not self.css_selector
        ):
            raise ValueError("FieldAnchorSpec requires at least one locator signal")
        return self


class FieldBindingSpec(BaseModel):
    """Declarative binding from a canonical dossier key to a UI control."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1)
    dossier_key: str = Field(min_length=1)
    anchor: FieldAnchorSpec
    control_kind: ControlKind = ControlKind.TEXT
    codec: FieldCodecSpec = Field(default_factory=FieldCodecSpec)
    prefill_policy: PrefillPolicy = PrefillPolicy.HALT_ON_CONFLICT
    triggers_re_render: bool = False
    required_in_pack: bool = False
    placeholder_values: tuple[str, ...] = (
        "",
        "-- Select --",
        "Select...",
        "Choose...",
        "Select one",
        "-- Select one --",
        "Choose one",
        "Please choose",
        "Choose",
        "-- Choose one --",
    )
    # Type-ahead comboboxes: type only this many leading characters of the
    # formatted value before reading suggestions. None types the whole value.
    typeahead_query_chars: Optional[int] = Field(default=None, ge=1, le=200)
    # Values a blank form shows before anyone types (seen on the dev case's
    # first screen). A control still showing one of these, that this run has
    # not written, holds no client data, so the dossier value is written
    # over it. With no dossier value it still stops. Never applies to a
    # control the run already wrote.
    portal_default_values: tuple[str, ...] = ()


class EntityTableSpec(BaseModel):
    """Detects server-prefilled entity rows (e.g., household members or patient matches)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_id: str = Field(min_length=1)
    entity_kind: str = Field(min_length=1)
    row_selector: str = Field(min_length=1)
    column_selectors: dict[str, str] = Field(min_length=1)
    key_fields: tuple[str, ...] = Field(min_length=1)
    field_codecs: dict[str, FieldCodecSpec] = Field(default_factory=dict)
    dismiss_button_selector: Optional[str] = None


class PopupSubroutineSpec(BaseModel):
    """Declarative child-window or popup calculator/lookup subroutine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subroutine_id: str = Field(min_length=1)
    trigger_anchor: FieldAnchorSpec
    popup_url_pattern: Optional[str] = None
    popup_title_pattern: Optional[str] = None
    bindings: tuple[FieldBindingSpec, ...] = ()
    # Buttons clicked in order after the bindings are filled and before
    # ``commit_anchor``, for estimators that need, say, Calculate before the
    # result can be applied. Each one must match exactly one control.
    compute_anchors: tuple[FieldAnchorSpec, ...] = ()
    # "window": the trigger opens a child window. "in_page": the trigger opens
    # a dialog in the same page; only controls that were not on the page
    # before the trigger count as the dialog's, and the commit button must be
    # gone afterwards.
    popup_mode: Literal["window", "in_page"] = "window"
    # The popup's own displayed result, read just before apply. When set, the
    # value the popup puts back in the parent must equal it under
    # ``return_codec``, or the run stops before submit. A declared result that
    # cannot be read is also a stop.
    result_anchor: Optional[FieldAnchorSpec] = None
    commit_anchor: FieldAnchorSpec
    parent_return_anchor: FieldAnchorSpec
    return_output_key: str = Field(min_length=1)
    return_codec: FieldCodecSpec = Field(
        default_factory=lambda: FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS)
    )
    condition_dossier_key: Optional[str] = None


class ModalRuleSpec(BaseModel):
    """Declarative detection of dynamic confirmation/duplicate modals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modal_id: str = Field(min_length=1)
    container_selector: str = Field(min_length=1)
    message_selector: Optional[str] = None
    confirm_selector: str = Field(min_length=1)
    cancel_selector: Optional[str] = None
    policy: Literal["halt_for_human", "confirm_if_dossier_flag"] = "halt_for_human"
    dossier_flag_key: Optional[str] = None


class WizardScreenSpec(BaseModel):
    """One logical screen/step in a multi-screen form or wizard."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    screen_id: str = Field(min_length=1)
    landmark_selector: Optional[str] = None
    title_pattern: Optional[str] = None
    next_button_anchor: Optional[FieldAnchorSpec] = None
    is_commit_screen: bool = False
    commit_button_anchor: Optional[FieldAnchorSpec] = None
    # A CSS selector (a tab, a step link, a "Change" link) that brings this
    # screen back. After a sign-in or a jump back interrupts the run, the
    # layer returns to the earliest visited screen that has one and walks
    # forward again, re-checking every field, before it submits.
    revisit_selector: Optional[str] = None


class OutputExtractionSpec(BaseModel):
    """Typed output value extracted after commit/summary verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_key: str = Field(min_length=1)
    anchor: FieldAnchorSpec
    codec: FieldCodecSpec = Field(default_factory=FieldCodecSpec)
    required: bool = True
    # A regular expression with one group, searched in the page's visible
    # text, for an output shown inside a sentence ("Quote number FC-Q-1 was
    # issued"). Used when the anchor finds no control or text pair. Every
    # match must give the same value.
    value_pattern: Optional[str] = None


class SplitDateReadbackSpec(BaseModel):
    """A date entered in day, month and year boxes but shown as one value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: FieldAnchorSpec
    day_key: str = Field(min_length=1)
    month_key: str = Field(min_length=1)
    year_key: str = Field(min_length=1)
    # How the summary orders a numeric date ("03/04/1988").
    date_order: Literal["YMD", "MDY", "DMY"] = "MDY"


class SummaryCheckSpec(BaseModel):
    """Independent summary/search read-back verification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_url_path: Optional[str] = None
    summary_landmark_selector: Optional[str] = None
    search_input_anchor: Optional[FieldAnchorSpec] = None
    search_submit_anchor: Optional[FieldAnchorSpec] = None
    search_by_output_key: Optional[str] = None
    field_readbacks: tuple[FieldBindingSpec, ...] = ()
    split_date_readbacks: tuple[SplitDateReadbackSpec, ...] = ()
    outputs: tuple[OutputExtractionSpec, ...] = ()


class UnboundControlSpec(BaseModel):
    """A control the pack does not fill, and the value it shows when blank."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: FieldAnchorSpec
    blank_values: tuple[str, ...] = ("",)


class FormPack(BaseModel):
    """Data-only per-target form contract. Zero executable code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.form-pack/v1"] = FORM_PACK_SCHEMA
    pack_id: str = Field(min_length=1)
    domain_schema: str = Field(min_length=1)
    ui_profile_id: str = Field(min_length=1)
    bindings: tuple[FieldBindingSpec, ...] = ()
    entity_tables: tuple[EntityTableSpec, ...] = ()
    popup_subroutines: tuple[PopupSubroutineSpec, ...] = ()
    modal_rules: tuple[ModalRuleSpec, ...] = ()
    screens: tuple[WizardScreenSpec, ...] = ()
    summary_check: Optional[SummaryCheckSpec] = None
    max_commit_mutations: int = Field(default=1, ge=0, le=10)
    # Controls the pack knowingly leaves alone, with the value a blank form
    # shows in each. Every other visible control that no binding covers is
    # watched: free text holding a value the run did not type stops before
    # submit, and any other value it did not set keeps the run from
    # ``verified_done``. These are listed as not covered on the receipt.
    unbound_controls: tuple["UnboundControlSpec", ...] = ()

    def digest(self) -> str:
        return canonical_sha256(self)


class ObservedOption(BaseModel):
    """An option available inside a select, radio group, or portal combobox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str
    label: str


class ObservedControl(BaseModel):
    """Backend-agnostic snapshot of a single visible interactive or output element."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str = Field(min_length=1)
    dom_selector: str = Field(min_length=1)
    tag: str = ""
    input_type: str = ""
    role: str = ""
    name_attr: str = ""
    id_attr: str = ""
    label_text: str = ""
    section_text: str = ""
    current_value: str = ""
    checked: Optional[bool] = None
    required: bool = False
    disabled: bool = False
    readonly: bool = False
    visible: bool = True
    error_text: str = ""
    options: tuple[ObservedOption, ...] = ()
    is_secret: bool = False
    # A native radio's own value attribute. ``current_value`` holds it only
    # while the radio is checked; an unchecked radio reads as empty.
    radio_value: str = ""
    # The radio group this radio belongs to (native: its name; ARIA: its
    # radiogroup container), and the group's visible label, if any.
    radio_group: str = ""
    group_label: str = ""
    # Text of the control's own row: the nearest table row or list item, else
    # the largest ancestor that holds no other field. ``row_cells`` lists the
    # row's cell texts when it is a table row.
    row_text: str = ""
    row_cells: tuple[str, ...] = ()

    # Selector of the open dialog that holds this control, if any.
    modal_container: str = ""

    @model_validator(mode="after")
    def _scrub_secret_fields(self) -> "ObservedControl":
        secret_detected = self.is_secret or self.input_type.lower() == "password"
        if secret_detected:
            object.__setattr__(self, "is_secret", True)
            if self.current_value and self.current_value != "<secret>":
                object.__setattr__(self, "current_value", "<secret>")
        return self


class ObservedEntityRow(BaseModel):
    """One observed row in a dynamic entity table/card list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_id: str
    entity_kind: str
    row_index: int
    row_selector: str
    values: dict[str, str]
    has_dismiss_action: bool = False


class ObservedModal(BaseModel):
    """An active modal dialog blocking or prompting on the current surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modal_id: str
    container_selector: str
    message_text: str = ""


class ObservedTextPair(BaseModel):
    """A label and value shown as page text, not in a form control.

    Read-only summary pages often show stored values as a definition list, a
    table or "Label: value" text. These pairs are kept apart from ``controls``
    so nothing ever treats them as fillable; only the read-back judge and
    output extraction look at them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    value: str = ""
    section_text: str = ""
    source: Literal["dl", "table_row", "table_columns", "inline"] = "inline"


class FormSnapshot(BaseModel):
    """Complete structural snapshot of the current form surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url_origin_and_path: str
    # Optional for non-browser backends; opaque document/record identity, not a URL.
    context_id: Optional[str] = None
    title: str = ""
    screen_id: Optional[str] = None
    auth_wall_present: bool = False
    authenticated_landmark_present: bool = False
    controls: tuple[ObservedControl, ...] = ()
    entity_rows: tuple[ObservedEntityRow, ...] = ()
    active_modals: tuple[ObservedModal, ...] = ()
    banner_errors: tuple[str, ...] = ()
    inflight_mutations: int = 0
    text_pairs: tuple[ObservedTextPair, ...] = ()
    # The visible text of the page (capped), for outputs shown in a sentence.
    page_text: str = ""


class FormHaltItem(BaseModel):
    """Structured fail-closed halt emitted when autonomous action is unsafe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    halt_id: str = Field(min_length=1)
    kind: FormHaltKind
    screen_id: Optional[str] = None
    control_id: Optional[str] = None
    dom_selector: Optional[str] = None
    field_label: str = ""
    section_text: str = ""
    dossier_key: Optional[str] = None
    portal_value: Optional[Any] = None
    dossier_value: Optional[Any] = None
    message: str = Field(min_length=1)
    allowed_actions: tuple[HumanDecisionAction, ...] = (
        HumanDecisionAction.PROVIDE_VALUE,
        HumanDecisionAction.FIXED_ON_SCREEN,
        HumanDecisionAction.ESCALATE_TO_OWNER,
        HumanDecisionAction.ABORT_RUN,
    )
    available_options: tuple[ObservedOption, ...] = ()


class HumanResolutionInput(BaseModel):
    """Operator/co-pilot submission resolving an active ``FormHaltItem``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    halt_id: str = Field(min_length=1)
    action: HumanDecisionAction
    actor_id: str = Field(min_length=1)
    actor_role: str = "operator"
    provided_value: Optional[Any] = None
    mapped_dossier_key: Optional[str] = None
    remember_as_candidate_rule: bool = False
    note: str = ""
    idempotency_key: str = Field(min_length=1)


class HumanResolutionRecord(BaseModel):
    """Immutable ledger entry for a verified human decision during a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    halt_id: str
    halt_kind: FormHaltKind
    action: HumanDecisionAction
    actor_id: str
    actor_role: str
    field_signature: str
    dossier_key: Optional[str] = None
    resolved_value: Optional[Any] = None
    remember_as_candidate_rule: bool = False
    timestamp_utc: str
    idempotency_key: str


class FieldWriteRecord(BaseModel):
    """One field the layer typed, selected or kept during a run.

    The read-back judge covers every record: each one gets a passed, failed or
    not_checked verdict. ``field_key`` is the dossier key when the field is
    bound, otherwise ``control:<label or selector>``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str = Field(min_length=1)
    dossier_key: Optional[str] = None
    label: Optional[str] = None
    selector: Optional[str] = None
    source: Literal[
        "auto_fill",
        "operator_value",
        "operator_fixed_on_screen",
        "operator_kept_prefill",
        "popup_return",
    ]
    in_popup_subroutine: Optional[str] = None
    # Set when a popup input was re-read from its control and matched just
    # before the popup's apply button. This guards the apply; it is not a
    # read-back of the application's record, so the field stays not_checked
    # unless the summary shows it.
    checked_before_apply: bool = False
    # For a popup return: the popup that produced it, and whether the value
    # put back in the parent matched the popup's own displayed result.
    from_popup_subroutine: Optional[str] = None
    matched_popup_result: bool = False


class ReadbackStatus(str, Enum):
    """Result of reading back one field after submit."""

    PASSED = "passed"
    FAILED = "failed"
    # The field was written, selected or kept, but the layer could not read it
    # back (no read-back declared, control absent, anchor ambiguous, lookup
    # failed). It is never counted as passed.
    NOT_CHECKED = "not_checked"


class ReadbackFieldVerdict(BaseModel):
    """Field-by-field comparison between intended state and post-run read-back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dossier_key: str
    codec_kind: FormCodecKind
    passed: bool
    provenance: ProvenanceKind
    reason: str = ""
    status: Optional[ReadbackStatus] = None

    @model_validator(mode="after")
    def _status_agrees_with_passed(self) -> "ReadbackFieldVerdict":
        if self.status is None:
            object.__setattr__(
                self,
                "status",
                ReadbackStatus.PASSED if self.passed else ReadbackStatus.FAILED,
            )
        elif self.passed != (self.status == ReadbackStatus.PASSED):
            raise ValueError("passed must be True exactly when status is 'passed'")
        return self


class SummaryVerdict(BaseModel):
    """Aggregate read-back verification result over summary/search screen."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    checked_count: int
    failed_keys: tuple[str, ...] = ()
    not_checked_keys: tuple[str, ...] = ()
    field_verdicts: tuple[ReadbackFieldVerdict, ...] = ()
    extracted_outputs: dict[str, Any] = Field(default_factory=dict)
    missing_required_outputs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _passed_needs_every_field_checked(self) -> "SummaryVerdict":
        if self.passed and (
            self.failed_keys
            or self.not_checked_keys
            or any(v.status != ReadbackStatus.PASSED for v in self.field_verdicts)
        ):
            raise ValueError("A read-back cannot pass with a failed or unchecked field")
        return self


class PackPatchCandidate(BaseModel):
    """Governed candidate binding or policy induced from >=2 consistent human fixes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.form-pack-patch/v1"] = PACK_PATCH_SCHEMA
    candidate_id: str
    pack_id: str
    field_signature: str
    proposed_kind: Literal["field_binding", "prefill_policy", "default_escalation"]
    proposed_dossier_key: Optional[str] = None
    proposed_action: Optional[HumanDecisionAction] = None
    supporting_run_count: int = Field(ge=2)
    admitted: bool = False


class FormTxReceiptPayload(BaseModel):
    """Value-scrubbed cryptographic receipt payload proving verified form commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.form-tx-receipt/v1"] = FORM_RECEIPT_SCHEMA
    receipt_id: str
    run_id: str
    request_id: str
    pack_id: str
    pack_digest: str
    dossier_digest: str
    outcome: FormTxOutcome
    timestamp_utc: str
    salt_hex: str
    fields_filled_auto: int = Field(ge=0)
    human_resolutions_count: int = Field(ge=0)
    human_resolution_digests: tuple[str, ...] = ()
    commit_mutations_observed: int = Field(ge=0)
    readback_checked_count: int = Field(ge=0)
    readback_passed: bool
    readback_not_checked_count: int = Field(default=0, ge=0)
    salted_output_digests: dict[str, str] = Field(default_factory=dict)
    # What the receipt covers: the dossier keys whose read-back passed. A
    # receipt says nothing about any other field. ``not_covered`` names the
    # controls the run saw but did not check (pack-declared unbound controls,
    # shown at their blank value) and intake keys no binding entered.
    covered_keys: tuple[str, ...] = ()
    not_covered: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _verified_means_fully_checked(self) -> "FormTxReceiptPayload":
        if self.outcome == FormTxOutcome.VERIFIED_DONE and (
            not self.readback_passed or self.readback_not_checked_count
        ):
            raise ValueError(
                "A verified receipt needs a passing read-back with no unchecked field"
            )
        return self


class SignedFormTxReceipt(BaseModel):
    """Ed25519-signed receipt envelope for ``FormTxReceiptPayload``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: FormTxReceiptPayload
    payload_sha256: str
    algorithm: Literal["Ed25519"] = "Ed25519"
    public_key_hex: str
    signature_hex: str


class FormTxResult(BaseModel):
    """Structured result returned by ``FormReconciler`` and ``QueueRunner``."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    request_id: str
    outcome: FormTxOutcome
    safe_to_retry: bool
    fields_filled_auto: int = 0
    subroutines_completed: tuple[str, ...] = ()
    halts: tuple[FormHaltItem, ...] = ()
    human_resolutions: tuple[HumanResolutionRecord, ...] = ()
    outputs: dict[str, Any] = Field(default_factory=dict)
    summary_verdict: Optional[SummaryVerdict] = None
    receipt: Optional[SignedFormTxReceipt] = None
    what_happened: str = ""
