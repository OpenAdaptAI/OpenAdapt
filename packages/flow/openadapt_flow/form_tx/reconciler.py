"""Stateful, resumable governed form and wizard reconciliation engine (``form_tx``).

Drives any ``FormSurfaceBackend`` against a declarative ``FormPack`` and
provenance-carrying ``InputDossier`` with zero model calls. Fails closed on
auth walls, dynamic modals, prefilled entity/value conflicts, ambiguous
options/anchors, popup contract failures, portal validation errors, and
ungrounded required fields (including newly materialized conditional fields).
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.form_tx.codecs import (
    CodecValueError,
    collapse_whitespace,
    format_for_text_entry,
    normalize_boolean_yes_no,
    normalize_with_codec,
    resolve_unique_option,
    values_equivalent,
)
from openadapt_flow.form_tx.judge import SummaryJudge
from openadapt_flow.form_tx.playwright_surface import (
    PopupContractError,
    SurfaceSettleTimeoutError,
)
from openadapt_flow.form_tx.receipt import issue_form_tx_receipt
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    FormSurfaceBackend,
    MissingAnchorError,
    collapse_radio_groups,
    compute_field_signature,
    match_anchor_in_snapshot,
    match_text_pair_in_snapshot,
    radio_group_key,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierEntity,
    DossierFact,
    EntityTableSpec,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FieldWriteRecord,
    FormCodecKind,
    FormHaltItem,
    FormHaltKind,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    FormTxResult,
    HumanDecisionAction,
    HumanResolutionInput,
    HumanResolutionRecord,
    InputDossier,
    ModalRuleSpec,
    ObservedControl,
    ObservedEntityRow,
    ObservedModal,
    ObservedOption,
    PopupSubroutineSpec,
    PrefillPolicy,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
    canonical_sha256,
)

__all__ = [
    "FormReconciler",
    "ReconcilerSession",
]

_PRIMARY_ENTITY_KINDS = frozenset(
    {
        "primary_applicant",
        "primary_insured",
        "applicant",
        "insured",
        "primary",
        "self",
    }
)
_DEFAULT_PLACEHOLDERS: tuple[str, ...] = (
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


class ReconcilerSession:
    """Mutable, resumable state for one governed ``form_tx`` execution."""

    run_id: str
    pack: FormPack
    dossier: InputDossier
    effective_facts: dict[str, DossierFact]
    dismissed_entities: set[str]
    confirmed_modals: set[str]
    human_resolutions: list[HumanResolutionRecord]
    seen_idempotency_keys: set[str]
    fields_filled_auto: int
    subroutines_completed: list[str]
    active_halts: list[FormHaltItem]
    signing_key: Ed25519PrivateKey

    def __init__(
        self,
        *args: Any,
        run_id: Optional[str] = None,
        pack: Optional[FormPack] = None,
        dossier: Optional[InputDossier] = None,
        effective_facts: Optional[dict[str, DossierFact]] = None,
        dismissed_entities: Optional[set[str]] = None,
        confirmed_modals: Optional[set[str]] = None,
        human_resolutions: Optional[list[HumanResolutionRecord]] = None,
        seen_idempotency_keys: Optional[set[str]] = None,
        fields_filled_auto: int = 0,
        subroutines_completed: Optional[list[str]] = None,
        active_halts: Optional[list[FormHaltItem]] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
    ) -> None:
        if args:
            if len(args) == 2 and isinstance(args[0], FormPack):
                pack, dossier = args
            elif len(args) == 3 and isinstance(args[0], str):
                run_id, pack, dossier = args
            else:
                raise TypeError(
                    "ReconcilerSession positional args must be (pack, dossier) "
                    "or (run_id, pack, dossier)"
                )
        if pack is None or dossier is None:
            raise ValueError("ReconcilerSession requires both pack and dossier")

        self.run_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
        self.pack = pack
        self.dossier = dossier
        if effective_facts is not None:
            self.effective_facts = dict(effective_facts)
        else:
            self.effective_facts = {
                key: gf
                for key in dossier.facts
                if (gf := dossier.grounded_fact(key)) is not None
            }
        self.dismissed_entities = (
            set(dismissed_entities) if dismissed_entities is not None else set()
        )
        self.confirmed_modals = (
            set(confirmed_modals) if confirmed_modals is not None else set()
        )
        self.human_resolutions = (
            list(human_resolutions) if human_resolutions is not None else []
        )
        self.seen_idempotency_keys = (
            set(seen_idempotency_keys) if seen_idempotency_keys is not None else set()
        )
        self.fields_filled_auto = fields_filled_auto
        self.subroutines_completed = (
            list(subroutines_completed) if subroutines_completed is not None else []
        )
        self.active_halts = list(active_halts) if active_halts is not None else []
        self.signing_key = (
            signing_key if signing_key is not None else Ed25519PrivateKey.generate()
        )

        # Internal session bookkeeping across halt/resume cycles:
        self._filled_selectors: set[str] = set()
        # Successful-save count when the commit control was clicked.
        self._pre_commit_confirmed: Optional[int] = None
        # Surface mutation count when this run first started.
        self._mutations_at_start: Optional[int] = None
        # True only while apply_human_resolution resumes the run.
        self._resuming: bool = False
        self._human_accepted_control_signatures: set[str] = set()
        self._human_accepted_values: dict[str, Any] = {}
        self._accepted_entity_rows: list[tuple[str, dict[str, str]]] = []
        self._halt_metadata: dict[str, dict[str, Any]] = {}
        self._idempotent_results: dict[str, FormTxResult] = {}
        self._idempotent_decisions: dict[str, str] = {}
        self._last_observation: Optional[FormSnapshot] = None
        self._halt_contexts: dict[str, tuple[str, Optional[str], Optional[str]]] = {}
        self._popup_manual_fills: dict[tuple[str, str], Any] = {}
        self._commit_in_progress: bool = False
        self._commit_clicked: bool = False

        # Set when the commit control is clicked and never cleared: after it,
        # a sign-in wall means the record may or may not exist.
        self._commit_attempted: bool = False
        self._pre_commit_mutations: int = 0
        self._last_result: Optional[FormTxResult] = None
        self._bound_surface: Optional[FormSurfaceBackend] = None
        # Every field typed, selected or kept, keyed by field identity. The
        # read-back judge must give each one a verdict.
        # Keyed by field, selector and popup, so a popup input never hides a
        # main-form write of the same dossier key.
        self._writes: dict[tuple[str, str, str, str], FieldWriteRecord] = {}

    def record_write(
        self,
        *,
        source: str,
        dossier_key: Optional[str] = None,
        label: Optional[str] = None,
        selector: Optional[str] = None,
        in_popup_subroutine: Optional[str] = None,
        from_popup_subroutine: Optional[str] = None,
        matched_popup_result: bool = False,
        keep_existing: bool = False,
    ) -> None:
        """Note that a field was written so the read-back must cover it.

        Keyed by field, screen, selector and popup: the same selector on two
        wizard screens is two controls.
        """
        if dossier_key and dossier_key != "human-current-field":
            field_key = dossier_key
        else:
            dossier_key = None
            field_key = f"control:{label or selector or 'unlabelled'}"
        screen_id = (
            self._last_observation.screen_id
            if self._last_observation is not None
            else None
        )
        ledger_key = (
            field_key,
            screen_id or "",
            selector or "",
            in_popup_subroutine or "",
        )
        if keep_existing and ledger_key in self._writes:
            return
        self._writes[ledger_key] = FieldWriteRecord(
            field_key=field_key,
            dossier_key=dossier_key,
            label=label,
            selector=selector,
            source=source,  # type: ignore[arg-type]
            in_popup_subroutine=in_popup_subroutine,
            screen_id=screen_id,
            from_popup_subroutine=from_popup_subroutine,
            matched_popup_result=matched_popup_result,
        )

    @property
    def writes(self) -> tuple[FieldWriteRecord, ...]:
        return tuple(self._writes.values())


def _is_action_button(ctrl: ObservedControl) -> bool:
    tag = ctrl.tag.casefold()
    itype = ctrl.input_type.casefold()
    role = ctrl.role.casefold()
    if role in {"combobox", "listbox", "checkbox", "radio", "switch", "textbox"}:
        return False
    return (
        tag == "button"
        or itype in ("button", "submit", "reset", "image")
        or role in ("button", "link")
    )


def _is_text_placeholder(value: str, placeholders: Sequence[str]) -> bool:
    norm = collapse_whitespace(value).casefold()
    if not norm:
        return True
    placeholder_set = {collapse_whitespace(p).casefold() for p in placeholders}
    return norm in placeholder_set


def _is_radio_control(ctrl: ObservedControl) -> bool:
    return ctrl.input_type.casefold() == "radio" or ctrl.role.casefold() == "radio"


def _is_control_empty(
    ctrl: ObservedControl,
    placeholders: Sequence[str] = _DEFAULT_PLACEHOLDERS,
) -> bool:
    """Return True if ``ctrl`` has no entered/selected value on screen."""
    itype = ctrl.input_type.casefold()
    role = ctrl.role.casefold()
    if _is_radio_control(ctrl) and ctrl.checked is False:
        # An unselected radio option holds no answer.
        return True
    if itype == "checkbox" or role == "checkbox":
        if ctrl.checked is True:
            return False
        if ctrl.checked is False:
            # An unchecked checkbox has not been positively prefilled/checked.
            return True
        return _is_text_placeholder(ctrl.current_value, placeholders)

    if _is_text_placeholder(ctrl.current_value, placeholders):
        return True

    if ctrl.options:
        matched_opts = [opt for opt in ctrl.options if opt.value == ctrl.current_value]
        if len(matched_opts) == 1 and _is_text_placeholder(
            matched_opts[0].label, placeholders
        ):
            return True

    return False


def _control_matches_fact(
    ctrl: ObservedControl,
    intended_value: Any,
    binding: FieldBindingSpec,
) -> bool:
    if binding.control_kind == ControlKind.CHECKBOX and ctrl.checked is not None:
        try:
            yn = normalize_boolean_yes_no(intended_value)
        except CodecValueError:
            return False
        return (yn == "yes") == bool(ctrl.checked)

    # partial-match-as-match: a radio option matches only when it is selected.
    if _is_radio_control(ctrl) and ctrl.checked is not True:
        return False

    if (
        binding.control_kind == ControlKind.PORTAL_COMBOBOX
        and binding.typeahead_query_chars is not None
    ):
        # A type-ahead that keeps only the leading part of its pick (see
        # _is_leading_part_of_choice); typed text alone never matches.
        try:
            formatted = format_for_text_entry(intended_value, binding.codec)
        except CodecValueError:
            formatted = ""
        if formatted and _is_leading_part_of_choice(
            ctrl.current_value,
            formatted,
            formatted[: binding.typeahead_query_chars],
        ):
            return True

    if values_equivalent(intended_value, ctrl.current_value, binding.codec):
        return True

    if ctrl.options:
        matched_opts = [opt for opt in ctrl.options if opt.value == ctrl.current_value]
        if len(matched_opts) == 1 and values_equivalent(
            intended_value, matched_opts[0].label, binding.codec
        ):
            return True

    return False


def _is_truthy_flag(value: Any) -> bool:
    if value is None:
        return False
    try:
        return normalize_boolean_yes_no(value) == "yes"
    except CodecValueError:
        return False


# Surface failures inside a popup routine that must become a stop for a
# person: a popup or dialog that will not open, closes or detaches while
# settling, a missing or doubled button, a keepalive that would write, a
# control that did not take its state, or a browser timeout. Matched by class
# name where the class lives in a backend this module does not import.
_POPUP_SURFACE_ERROR_NAMES = frozenset(
    {
        "PopupContractError",
        "SurfaceSettleTimeoutError",
        "SurfaceContextError",
        "SurfaceStateError",
        "KeepaliveMutationError",
        "MissingAnchorError",
        "AmbiguousAnchorError",
    }
)


def _is_popup_surface_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            PopupContractError,
            MissingAnchorError,
            AmbiguousAnchorError,
            SurfaceSettleTimeoutError,
            TimeoutError,
        ),
    ):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _POPUP_SURFACE_ERROR_NAMES:
            return True
        if cls.__module__.startswith("playwright") and cls.__name__ in (
            "TimeoutError",
            "Error",
            "TargetClosedError",
        ):
            return True
    return False


def _mutation_count_or_none(surface: Any) -> Optional[int]:
    try:
        return int(surface.mutation_count)
    except Exception:  # noqa: BLE001 - an unreadable count is treated as a send
        return None


class _InPageScope:
    """Which controls belong to an in-page popup (a dialog in the same page).

    Controls inside an open dialog that was not open before the trigger are
    the popup's, whatever their selectors (``ObservedControl.modal_container``).
    That survives a parent form that re-renders with new ids when the dialog
    opens, and keeps a dialog control whose selector a parent control once
    had. With no such dialog, the popup's controls are the ones whose
    selectors were not on the page before; that is only safe while every
    parent control is still there under its old selector, so a parent
    re-render, or two new dialogs, stops the run (``PopupContractError``)
    rather than letting a subroutine fill a parent control.
    """

    def __init__(self, parent_snapshot: FormSnapshot) -> None:
        self._parent_selectors = frozenset(
            c.dom_selector for c in parent_snapshot.controls
        )
        self._parent_dialogs = frozenset(
            c.modal_container for c in parent_snapshot.controls if c.modal_container
        )

    def apply(self, snapshot: FormSnapshot) -> FormSnapshot:
        dialogs = {
            c.modal_container
            for c in snapshot.controls
            if c.modal_container and c.modal_container not in self._parent_dialogs
        }
        if len(dialogs) > 1:
            raise PopupContractError(
                "More than one new dialog is open; the popup's controls cannot "
                "be told apart"
            )
        if dialogs:
            (dialog,) = dialogs
            kept = tuple(c for c in snapshot.controls if c.modal_container == dialog)
        else:
            present = {c.dom_selector for c in snapshot.controls}
            if not self._parent_selectors <= present:
                raise PopupContractError(
                    "The page re-rendered when the popup opened and the popup is "
                    "not a dialog, so its controls cannot be told apart from the "
                    "form's"
                )
            kept = tuple(
                c
                for c in snapshot.controls
                if c.dom_selector not in self._parent_selectors
            )
        return snapshot.model_copy(update={"controls": kept})


def _deterministic_halt_id(
    run_id: str,
    kind: FormHaltKind,
    screen_id: Optional[str],
    locator: str,
    signature: str,
) -> str:
    digest = canonical_sha256(
        {
            "run_id": run_id,
            "kind": kind.value,
            "screen_id": screen_id or "",
            "locator": locator,
            "signature": signature,
        }
    )
    return f"halt_{digest[:14]}"


# Backend errors that mean "the surface could not do what was asked": the
# control vanished or matched twice, a listbox or popup never appeared, the
# page never settled, or the browser timed out. Matched by class name so the
# reconciler does not import a browser backend.
_SURFACE_ACTION_ERROR_NAMES = frozenset(
    {
        "MissingAnchorError",
        "AmbiguousAnchorError",
        "PopupContractError",
        "SurfaceSettleTimeoutError",
        "SurfaceContextError",
        "SurfaceStateError",
    }
)


def _is_surface_action_error(exc: BaseException) -> bool:
    if isinstance(exc, (MissingAnchorError, AmbiguousAnchorError, TimeoutError)):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _SURFACE_ACTION_ERROR_NAMES:
            return True
        if cls.__module__.startswith("playwright") and cls.__name__ in (
            "TimeoutError",
            "Error",
        ):
            return True
    return False


def _is_editable_combobox(ctrl: ObservedControl) -> bool:
    """A combobox that is itself a text input takes typed text."""
    return (
        ctrl.tag.casefold() in ("input", "textarea")
        and not ctrl.readonly
        and not ctrl.disabled
    )


def _is_leading_part_of_choice(shown: str, label: str, typed: str) -> bool:
    """True when a pick left the leading part of its label in the input.

    Some type-aheads put only the first part of the chosen suggestion in the
    input ("2210 Juniper Loop" from "2210 Juniper Loop Apt 4, Brackenfield,
    OR") and fill the rest into other fields. That counts only when the input
    shows more than was typed, so text left over from typing never passes.
    """
    shown_n = collapse_whitespace(shown).casefold()
    label_n = collapse_whitespace(label).casefold()
    typed_n = collapse_whitespace(typed).casefold()
    if not shown_n or shown_n == typed_n or len(shown_n) <= len(typed_n):
        return False
    if not label_n.startswith(shown_n) or len(label_n) == len(shown_n):
        return False
    return label_n[len(shown_n)] in " ,;-/("


def _surface_action_halt(
    run_id: str,
    snapshot: FormSnapshot,
    exc: BaseException,
    *,
    ctrl: Optional[ObservedControl] = None,
    dossier_key: Optional[str] = None,
    dossier_value: Any = None,
    locator: str = "",
) -> FormHaltItem:
    """A stop for a person when the surface could not act on a control."""
    target = ctrl.dom_selector if ctrl is not None else locator
    label = ctrl.label_text if ctrl is not None else ""
    sig = (
        compute_field_signature(ctrl)
        if ctrl is not None
        else f"surface_action|{locator}"
    )
    halt_id = _deterministic_halt_id(
        run_id,
        FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
        snapshot.screen_id,
        target,
        sig,
    )
    where = label or target or "the form"
    allowed: tuple[HumanDecisionAction, ...] = (
        HumanDecisionAction.FIXED_ON_SCREEN,
        HumanDecisionAction.ESCALATE_TO_OWNER,
        HumanDecisionAction.ABORT_RUN,
    )
    if ctrl is not None:
        allowed = (HumanDecisionAction.PROVIDE_VALUE, *allowed)
    return FormHaltItem(
        halt_id=halt_id,
        kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
        screen_id=snapshot.screen_id,
        control_id=ctrl.control_id if ctrl is not None else None,
        dom_selector=target or None,
        field_label=label,
        section_text=ctrl.section_text if ctrl is not None else "",
        dossier_key=dossier_key,
        dossier_value=dossier_value,
        message=(
            f"Could not act on {where!r}: {type(exc).__name__}: "
            f"{collapse_whitespace(str(exc))[:400]}"
        ),
        allowed_actions=allowed,
    )


def _session_expired_text(
    snapshot: FormSnapshot, ui_profile: Optional[UIProfileSpec]
) -> Optional[str]:
    """Return the banner or dialog text that says the session ended, if any."""
    patterns = ui_profile.session_expired_text_patterns if ui_profile else ()
    if not patterns:
        return None
    texts = [*snapshot.banner_errors, *(m.message_text for m in snapshot.active_modals)]
    for text in texts:
        norm = collapse_whitespace(text)
        for pattern in patterns:
            try:
                if re.search(pattern, norm, flags=re.IGNORECASE):
                    return norm
            except re.error:
                continue
    return None


def _auth_wall_reason(
    snapshot: FormSnapshot, ui_profile: Optional[UIProfileSpec]
) -> Optional[str]:
    """Why ``snapshot`` is a sign-in wall, or ``None`` when it is not.

    A sign-in wall is a configured auth-wall element, a visible editable
    secret field, a missing signed-in landmark, or an error banner or dialog
    whose text says the session ended. Each is checked before any error is
    read as a validation failure, so an expired session is never reported as
    a field problem.
    """
    if snapshot.auth_wall_present:
        return "sign-in form on screen"
    if any(
        control.visible and control.is_secret and not control.readonly
        for control in snapshot.controls
    ):
        return "password or secret field on screen"
    if (
        ui_profile is not None
        and ui_profile.authenticated_landmark_selectors
        and not snapshot.authenticated_landmark_present
    ):
        return "signed-in landmark missing"
    expired = _session_expired_text(snapshot, ui_profile)
    if expired:
        return f"session ended: {expired}"
    return None


def _collect_surface_errors(snapshot: FormSnapshot) -> list[str]:
    errors = [collapse_whitespace(b) for b in snapshot.banner_errors if b.strip()]
    for ctrl in snapshot.controls:
        if ctrl.visible and ctrl.error_text.strip():
            label = collapse_whitespace(ctrl.label_text) or ctrl.dom_selector
            errors.append(f"{label}: {collapse_whitespace(ctrl.error_text)}")
    return errors


def _identify_wizard_screen(
    snapshot: FormSnapshot,
    screens: Sequence[WizardScreenSpec],
) -> Optional[WizardScreenSpec]:
    if not screens:
        return None
    if snapshot.screen_id:
        for spec in screens:
            if spec.screen_id == snapshot.screen_id:
                return spec
    if snapshot.title:
        for spec in screens:
            if spec.title_pattern and re.search(
                spec.title_pattern, snapshot.title, flags=re.IGNORECASE
            ):
                return spec
    for spec in screens:
        if spec.commit_button_anchor is not None:
            try:
                if (
                    match_anchor_in_snapshot(spec.commit_button_anchor, snapshot)
                    is not None
                ):
                    return spec
            except AmbiguousAnchorError:
                pass
        if spec.next_button_anchor is not None:
            try:
                if (
                    match_anchor_in_snapshot(spec.next_button_anchor, snapshot)
                    is not None
                ):
                    return spec
            except AmbiguousAnchorError:
                pass
    return None


def _compose_dismiss_selector(
    spec: EntityTableSpec,
    row: ObservedEntityRow,
) -> Optional[str]:
    if spec.dismiss_button_selector:
        btn = spec.dismiss_button_selector.strip()
        if row.row_selector and not btn.startswith(row.row_selector):
            return f"{row.row_selector} {btn}"
        return btn
    if row.has_dismiss_action and row.row_selector:
        return f"{row.row_selector} [data-dismiss]"
    return None


def _entity_row_matches_dossier_entity(
    row: ObservedEntityRow,
    entity: DossierEntity,
    spec: EntityTableSpec,
    min_confidence: float,
) -> bool:
    for kf in spec.key_fields:
        obs_val = row.values.get(kf)
        fact = entity.fields.get(kf)
        if obs_val is None or fact is None or fact.value is None:
            return False
        if fact.confidence < min_confidence:
            return False
        codec = spec.field_codecs.get(kf, FieldCodecSpec())
        if not values_equivalent(fact.value, obs_val, codec):
            return False
    return True


def _entity_row_matches_values_map(
    row: ObservedEntityRow,
    accepted_values: dict[str, str],
    spec: EntityTableSpec,
) -> bool:
    for kf in spec.key_fields:
        obs_val = row.values.get(kf)
        acc_val = accepted_values.get(kf)
        if obs_val is None or acc_val is None:
            return False
        codec = spec.field_codecs.get(kf, FieldCodecSpec())
        if not values_equivalent(acc_val, obs_val, codec):
            return False
    return True


def _is_return_value_empty_or_zero(
    raw_value: str,
    codec: FieldCodecSpec,
    placeholders: Sequence[str] = _DEFAULT_PLACEHOLDERS,
) -> bool:
    if _is_text_placeholder(raw_value, placeholders):
        return True
    try:
        norm = normalize_with_codec(raw_value, codec)
    except CodecValueError:
        return False
    if codec.kind in (FormCodecKind.CURRENCY_CENTS, FormCodecKind.INTEGER):
        return norm == 0
    if isinstance(norm, str):
        return not norm.strip()
    return False


def _is_valid_non_zero_return_value(
    raw_value: str,
    codec: FieldCodecSpec,
    placeholders: Sequence[str] = _DEFAULT_PLACEHOLDERS,
) -> bool:
    if _is_text_placeholder(raw_value, placeholders):
        return False
    try:
        norm = normalize_with_codec(raw_value, codec)
    except CodecValueError:
        return False
    if codec.kind in (FormCodecKind.CURRENCY_CENTS, FormCodecKind.INTEGER):
        return int(norm) > 0
    if isinstance(norm, str):
        return bool(norm.strip())
    return norm is not None


class FormReconciler:
    """Deterministic governed execution engine for multi-screen forms and wizards."""

    def __init__(
        self,
        surface: Optional[FormSurfaceBackend] = None,
        *,
        signing_key: Optional[Ed25519PrivateKey] = None,
        max_screen_transitions: int = 25,
        max_passes_per_screen: int = 10,
        summary_judge: Optional[SummaryJudge] = None,
    ) -> None:
        self._surface = surface
        self._default_signing_key = signing_key
        self.max_screen_transitions = max_screen_transitions
        self.max_passes_per_screen = max_passes_per_screen
        self._summary_judge = (
            summary_judge if summary_judge is not None else SummaryJudge()
        )

    def create_session(
        self,
        pack: FormPack,
        dossier: InputDossier,
        *,
        surface: Optional[FormSurfaceBackend] = None,
        run_id: Optional[str] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
    ) -> ReconcilerSession:
        """Initialize a fresh ``ReconcilerSession`` from ``pack`` and ``dossier``."""
        sess = ReconcilerSession(
            run_id=run_id,
            pack=pack,
            dossier=dossier,
            signing_key=signing_key
            if signing_key is not None
            else self._default_signing_key,
        )
        sess._bound_surface = surface
        return sess

    start_session = create_session

    def _resolve_surface(
        self,
        override: Optional[FormSurfaceBackend],
        session: Optional[ReconcilerSession] = None,
    ) -> FormSurfaceBackend:
        active = (
            override
            if override is not None
            else (
                session._bound_surface
                if session is not None and session._bound_surface is not None
                else self._surface
            )
        )
        if active is None:
            raise ValueError("FormReconciler requires a FormSurfaceBackend instance")
        return active

    def _build_result(
        self,
        session: ReconcilerSession,
        *,
        outcome: FormTxOutcome,
        safe_to_retry: bool,
        halts: Sequence[FormHaltItem] = (),
        outputs: Optional[dict[str, Any]] = None,
        summary_verdict: Any = None,
        receipt: Any = None,
        what_happened: str = "",
    ) -> FormTxResult:
        session.active_halts = list(halts)
        if session._last_observation is not None:
            for halt in halts:
                session._halt_contexts[halt.halt_id] = (
                    session._last_observation.url_origin_and_path,
                    session._last_observation.screen_id,
                    session._last_observation.context_id,
                )
        result = FormTxResult(
            run_id=session.run_id,
            request_id=session.dossier.request_id,
            outcome=outcome,
            safe_to_retry=safe_to_retry,
            fields_filled_auto=session.fields_filled_auto,
            subroutines_completed=tuple(session.subroutines_completed),
            halts=tuple(halts),
            human_resolutions=tuple(session.human_resolutions),
            outputs=dict(outputs or {}),
            summary_verdict=summary_verdict,
            receipt=receipt,
            what_happened=what_happened,
        )
        session._last_result = result
        return result

    def _match_modal_rule(
        self,
        obs_modal: ObservedModal,
        rules: Sequence[ModalRuleSpec],
    ) -> Optional[ModalRuleSpec]:
        for rule in rules:
            if (
                rule.modal_id == obs_modal.modal_id
                or rule.container_selector == obs_modal.container_selector
            ):
                return rule
        return None

    def _check_entity_table_conflicts(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
    ) -> Optional[FormHaltItem]:
        pack = session.pack
        dossier = session.dossier

        for spec in pack.entity_tables:
            expected_entities: list[DossierEntity] = list(
                dossier.entities_of_kind(spec.entity_kind)
            )
            for ent in dossier.entities:
                if (
                    ent.entity_kind in _PRIMARY_ENTITY_KINDS
                    and ent not in expected_entities
                ):
                    if all(kf in ent.fields for kf in spec.key_fields):
                        expected_entities.append(ent)

            accepted_for_table = [
                vals
                for tbl_id, vals in session._accepted_entity_rows
                if tbl_id == spec.table_id
            ]

            for row in snapshot.entity_rows:
                if row.table_id != spec.table_id:
                    continue
                matched_grounded = any(
                    _entity_row_matches_dossier_entity(
                        row, ent, spec, dossier.min_confidence
                    )
                    for ent in expected_entities
                )
                if matched_grounded:
                    continue
                matched_accepted = any(
                    _entity_row_matches_values_map(row, acc_vals, spec)
                    for acc_vals in accepted_for_table
                )
                if matched_accepted:
                    continue

                dismiss_sel = _compose_dismiss_selector(spec, row)
                sig = f"entity_table|{spec.table_id}|{spec.entity_kind}"
                halt_id = _deterministic_halt_id(
                    session.run_id,
                    FormHaltKind.PREFILL_ENTITY_CONFLICT,
                    snapshot.screen_id,
                    row.row_selector,
                    sig,
                )
                halt = FormHaltItem(
                    halt_id=halt_id,
                    kind=FormHaltKind.PREFILL_ENTITY_CONFLICT,
                    screen_id=snapshot.screen_id,
                    dom_selector=dismiss_sel or row.row_selector,
                    field_label=f"{spec.entity_kind}[{row.row_index}]",
                    portal_value=dict(row.values),
                    message=(
                        f"Portal prefilled unlisted {spec.entity_kind!r} entity row "
                        f"in table {spec.table_id!r}"
                    ),
                    allowed_actions=(
                        HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                        HumanDecisionAction.DISMISS_PORTAL_ENTITY,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                )
                session._halt_metadata[halt_id] = {
                    "field_signature": sig,
                    "entity_table_spec": spec,
                    "entity_row": row,
                    "dismiss_selector": dismiss_sel,
                }
                return halt

        return None

    def _actuate_control(
        self,
        surface: FormSurfaceBackend,
        snapshot: FormSnapshot,
        ctrl: ObservedControl,
        intended_value: Any,
        *,
        control_kind: ControlKind,
        codec: FieldCodecSpec,
        placeholder_values: Sequence[str] = _DEFAULT_PLACEHOLDERS,
        dossier_key: Optional[str] = None,
        run_id: str = "",
        typeahead_query_chars: Optional[int] = None,
    ) -> Optional[FormHaltItem]:
        """Actuate ``ctrl`` on ``surface``, returning a ``FormHaltItem`` instead of raising.

        A control the surface cannot act on (it vanished, matched twice, never
        opened its suggestions, or the page never settled) is a stop for a
        person, not an exception out of the run.
        """
        try:
            return self._actuate_control_unguarded(
                surface,
                snapshot,
                ctrl,
                intended_value,
                control_kind=control_kind,
                codec=codec,
                placeholder_values=placeholder_values,
                dossier_key=dossier_key,
                run_id=run_id,
                typeahead_query_chars=typeahead_query_chars,
            )
        except Exception as exc:
            if not _is_surface_action_error(exc):
                raise
            return _surface_action_halt(
                run_id,
                snapshot,
                exc,
                ctrl=ctrl,
                dossier_key=dossier_key,
                dossier_value=intended_value,
            )

    def _actuate_control_unguarded(
        self,
        surface: FormSurfaceBackend,
        snapshot: FormSnapshot,
        ctrl: ObservedControl,
        intended_value: Any,
        *,
        control_kind: ControlKind,
        codec: FieldCodecSpec,
        placeholder_values: Sequence[str] = _DEFAULT_PLACEHOLDERS,
        dossier_key: Optional[str] = None,
        run_id: str = "",
        typeahead_query_chars: Optional[int] = None,
    ) -> Optional[FormHaltItem]:
        typed_query: Optional[str] = None
        try:
            if control_kind in (ControlKind.TEXT, ControlKind.DATE):
                formatted = format_for_text_entry(intended_value, codec)
                surface.fill_text(ctrl.dom_selector, formatted)
                return None

            if control_kind == ControlKind.SELECT:
                opt = resolve_unique_option(
                    intended_value, ctrl.options, codec, placeholder_values
                )
                surface.select_native_option(ctrl.dom_selector, opt.value)
                return None

            if control_kind == ControlKind.PORTAL_COMBOBOX:
                typer = getattr(surface, "type_portal_combobox_query", None)
                if typer is not None and _is_editable_combobox(ctrl):
                    # Type-ahead: suggestions exist only after typed text.
                    typed_query = format_for_text_entry(intended_value, codec)
                    if typeahead_query_chars is not None:
                        typed_query = typed_query[:typeahead_query_chars]
                    opts = typer(ctrl.dom_selector, typed_query)
                else:
                    opts = surface.open_portal_combobox_options(ctrl.dom_selector)
                try:
                    opt = resolve_unique_option(
                        intended_value, opts, codec, placeholder_values
                    )
                except CodecValueError as exc:
                    sig = compute_field_signature(ctrl)
                    halt_id = _deterministic_halt_id(
                        run_id,
                        FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        snapshot.screen_id,
                        ctrl.dom_selector,
                        sig,
                    )
                    return FormHaltItem(
                        halt_id=halt_id,
                        kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        screen_id=snapshot.screen_id,
                        control_id=ctrl.control_id,
                        dom_selector=ctrl.dom_selector,
                        field_label=ctrl.label_text,
                        section_text=ctrl.section_text,
                        dossier_key=dossier_key,
                        dossier_value=intended_value,
                        message=str(exc),
                        allowed_actions=(
                            HumanDecisionAction.PROVIDE_VALUE,
                            HumanDecisionAction.FIXED_ON_SCREEN,
                            HumanDecisionAction.ESCALATE_TO_OWNER,
                            HumanDecisionAction.ABORT_RUN,
                        ),
                        available_options=opts,
                    )
                surface.choose_portal_combobox_option(ctrl.dom_selector, opt)
                if typed_query is not None:
                    return self._verify_typeahead_choice(
                        surface,
                        snapshot,
                        ctrl,
                        intended_value,
                        opt,
                        codec=codec,
                        dossier_key=dossier_key,
                        run_id=run_id,
                        typed_query=typed_query,
                    )
                return None

            if control_kind == ControlKind.CHECKBOX:
                yn = normalize_boolean_yes_no(intended_value)
                surface.set_checkbox_or_radio(ctrl.dom_selector, checked=(yn == "yes"))
                return None

            if control_kind == ControlKind.RADIO_GROUP:
                if ctrl.options:
                    opt = resolve_unique_option(
                        intended_value, ctrl.options, codec, placeholder_values
                    )
                    group = radio_group_key(ctrl)
                    radio_peers = [
                        c
                        for c in snapshot.controls
                        if c.visible
                        and group is not None
                        and radio_group_key(c) == group
                        and (c.radio_value or c.current_value) == opt.value
                    ]
                    if len(radio_peers) == 1:
                        surface.set_checkbox_or_radio(
                            radio_peers[0].dom_selector, checked=True
                        )
                    elif ctrl.tag.casefold() == "select":
                        surface.select_native_option(ctrl.dom_selector, opt.value)
                    else:
                        surface.fill_text(ctrl.dom_selector, opt.value)
                else:
                    yn = normalize_boolean_yes_no(intended_value)
                    surface.set_checkbox_or_radio(
                        ctrl.dom_selector, checked=(yn == "yes")
                    )
                return None

            raise CodecValueError(
                f"Control kind {control_kind.value!r} cannot be directly actuated as an input"
            )
        except Exception as exc:
            # A codec mismatch, or a control that did not take the state the
            # surface set (``SurfaceStateError``), stops for a person.
            if not isinstance(exc, CodecValueError) and (
                type(exc).__name__ != "SurfaceStateError"
            ):
                raise
            sig = compute_field_signature(ctrl)
            halt_id = _deterministic_halt_id(
                run_id,
                FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                snapshot.screen_id,
                ctrl.dom_selector,
                sig,
            )
            return FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                screen_id=snapshot.screen_id,
                control_id=ctrl.control_id,
                dom_selector=ctrl.dom_selector,
                field_label=ctrl.label_text,
                section_text=ctrl.section_text,
                dossier_key=dossier_key,
                dossier_value=intended_value,
                message=str(exc),
                allowed_actions=(
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
                available_options=ctrl.options,
            )

    def _verify_typeahead_choice(
        self,
        surface: FormSurfaceBackend,
        snapshot: FormSnapshot,
        ctrl: ObservedControl,
        intended_value: Any,
        option: ObservedOption,
        *,
        codec: FieldCodecSpec,
        dossier_key: Optional[str],
        run_id: str,
        typed_query: str = "",
    ) -> Optional[FormHaltItem]:
        """Stop unless the type-ahead now holds the chosen suggestion.

        Typed text left in the input looks like a value but is not a choice,
        so the control must show the chosen option (or the intended value).
        """
        after = surface.snapshot()
        live = next(
            (
                c
                for c in after.controls
                if c.visible and c.dom_selector == ctrl.dom_selector
            ),
            None,
        )
        if live is None:
            # Pages that redraw a step after a pick give the input a new id
            # (and often a new random name). Find it again by section, label
            # and kind; only a unique, labelled match counts.
            def _key(c: ObservedControl) -> tuple[str, str, str]:
                return (
                    collapse_whitespace(c.section_text).casefold(),
                    collapse_whitespace(c.label_text).casefold(),
                    (c.role or c.input_type or c.tag).casefold(),
                )

            if collapse_whitespace(ctrl.label_text):
                same = [
                    c for c in after.controls if c.visible and _key(c) == _key(ctrl)
                ]
                if len(same) == 1:
                    live = same[0]
        shown = collapse_whitespace(live.current_value) if live is not None else ""
        accepted = {
            collapse_whitespace(option.label).casefold(),
            collapse_whitespace(option.value).casefold(),
        } - {""}
        if (
            live is not None
            and shown
            and (
                shown.casefold() in accepted
                or values_equivalent(intended_value, live.current_value, codec)
                or _is_leading_part_of_choice(shown, option.label, typed_query)
            )
        ):
            return None
        reason = (
            "the control is gone after choosing"
            if live is None
            else f"it shows {shown!r} instead"
        )
        return _surface_action_halt(
            run_id,
            snapshot,
            MissingAnchorError(
                f"Chose suggestion {option.label!r} in "
                f"{ctrl.label_text or ctrl.dom_selector!r}, but {reason}"
            ),
            ctrl=ctrl,
            dossier_key=dossier_key,
            dossier_value=intended_value,
        )

    def _infer_unmapped_control_kind(self, ctrl: ObservedControl) -> ControlKind:
        tag = ctrl.tag.casefold()
        itype = ctrl.input_type.casefold()
        role = ctrl.role.casefold()
        if tag == "select":
            return ControlKind.SELECT
        if role == "combobox":
            return ControlKind.PORTAL_COMBOBOX
        if itype == "checkbox" or role == "checkbox":
            return ControlKind.CHECKBOX
        if itype == "radio" or role in ("radio", "radiogroup"):
            return ControlKind.RADIO_GROUP
        if itype == "date":
            return ControlKind.DATE
        return ControlKind.TEXT

    def _reconcile_bindings_pass(
        self,
        surface: FormSurfaceBackend,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        bindings: Sequence[FieldBindingSpec],
        *,
        in_popup_subroutine: Optional[str] = None,
    ) -> tuple[bool, bool, Optional[FormHaltItem]]:
        """Run one pass over ``bindings``.

        Returns ``(edited_any, triggered_re_render, halt_item)``.
        """
        edited_any = False
        for binding in bindings:
            if binding.control_kind in (
                ControlKind.POPUP_LAUNCHER,
                ControlKind.READONLY_OUTPUT,
            ):
                continue

            try:
                ctrl = match_anchor_in_snapshot(
                    binding.anchor, snapshot, include_readonly=False
                )
            except AmbiguousAnchorError as exc:
                sig = f"ambiguous_anchor|{binding.binding_id}|{binding.dossier_key}"
                halt_id = _deterministic_halt_id(
                    session.run_id,
                    FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                    snapshot.screen_id,
                    binding.binding_id,
                    sig,
                )
                halt = FormHaltItem(
                    halt_id=halt_id,
                    kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                    screen_id=snapshot.screen_id,
                    field_label=binding.binding_id,
                    dossier_key=binding.dossier_key,
                    message=str(exc),
                    allowed_actions=(
                        HumanDecisionAction.FIXED_ON_SCREEN,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                )
                session._halt_metadata[halt_id] = {
                    "field_signature": sig,
                    "binding": binding,
                    "in_popup_subroutine": in_popup_subroutine,
                }
                return edited_any, False, halt

            if ctrl is None:
                continue

            ctrl_sig = compute_field_signature(ctrl)
            filled_key = (
                f"popup:{in_popup_subroutine}:{ctrl.dom_selector}"
                if in_popup_subroutine
                else ctrl.dom_selector
            )
            was_filled_by_session = (
                filled_key in session._filled_selectors
                or ctrl_sig in session._human_accepted_control_signatures
            )
            is_empty = _is_control_empty(ctrl, binding.placeholder_values)
            fact = session.effective_facts.get(binding.dossier_key)

            # Prefill Value Conflict Check
            if not is_empty and not was_filled_by_session:
                if fact is None or (
                    not _control_matches_fact(ctrl, fact.value, binding)
                    and binding.prefill_policy != PrefillPolicy.OVERWRITE_WITH_DOSSIER
                ):
                    halt_id = _deterministic_halt_id(
                        session.run_id,
                        FormHaltKind.PREFILL_VALUE_CONFLICT,
                        snapshot.screen_id,
                        ctrl.dom_selector,
                        ctrl_sig,
                    )
                    halt = FormHaltItem(
                        halt_id=halt_id,
                        kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                        screen_id=snapshot.screen_id,
                        control_id=ctrl.control_id,
                        dom_selector=ctrl.dom_selector,
                        field_label=ctrl.label_text,
                        section_text=ctrl.section_text,
                        dossier_key=binding.dossier_key,
                        portal_value=ctrl.current_value,
                        dossier_value=fact.value if fact is not None else None,
                        message=(
                            f"Prefilled value on {ctrl.label_text or ctrl.dom_selector!r} "
                            f"conflicts with grounded dossier state for {binding.dossier_key!r}"
                        ),
                        allowed_actions=(
                            HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                            HumanDecisionAction.PROVIDE_VALUE,
                            HumanDecisionAction.FIXED_ON_SCREEN,
                            HumanDecisionAction.ESCALATE_TO_OWNER,
                            HumanDecisionAction.ABORT_RUN,
                        ),
                        available_options=ctrl.options,
                    )
                    session._halt_metadata[halt_id] = {
                        "field_signature": ctrl_sig,
                        "control": ctrl,
                        "binding": binding,
                        "in_popup_subroutine": in_popup_subroutine,
                    }
                    return edited_any, False, halt

            if fact is not None and (
                is_empty
                or binding.prefill_policy == PrefillPolicy.OVERWRITE_WITH_DOSSIER
                or was_filled_by_session
            ):
                if not _control_matches_fact(ctrl, fact.value, binding):
                    act_halt = self._actuate_control(
                        surface,
                        snapshot,
                        ctrl,
                        fact.value,
                        control_kind=binding.control_kind,
                        codec=binding.codec,
                        placeholder_values=binding.placeholder_values,
                        dossier_key=binding.dossier_key,
                        run_id=session.run_id,
                        typeahead_query_chars=binding.typeahead_query_chars,
                    )
                    if act_halt is not None:
                        session._halt_metadata[act_halt.halt_id] = {
                            "field_signature": ctrl_sig,
                            "control": ctrl,
                            "binding": binding,
                            "in_popup_subroutine": in_popup_subroutine,
                        }
                        return edited_any, False, act_halt

                    session.fields_filled_auto += 1
                    session._filled_selectors.add(filled_key)
                    session.record_write(
                        source="auto_fill",
                        dossier_key=binding.dossier_key,
                        label=ctrl.label_text,
                        selector=ctrl.dom_selector,
                        in_popup_subroutine=in_popup_subroutine,
                    )
                    edited_any = True
                    if binding.triggers_re_render:
                        return True, True, None
                    if self._controls_were_replaced(surface, snapshot):
                        # The page rebuilt its controls on change; every
                        # selector from this snapshot may now be stale.
                        return True, True, None
            if fact is not None and _control_matches_fact(ctrl, fact.value, binding):
                # A control that already held the intended value (whatever the
                # prefill policy, an unchecked box meant as "no" included) is
                # still a control this run relied on: it needs its own verdict.
                session.record_write(
                    source="kept_matching",
                    dossier_key=binding.dossier_key,
                    label=ctrl.label_text,
                    selector=ctrl.dom_selector,
                    in_popup_subroutine=in_popup_subroutine,
                    keep_existing=True,
                )

        return edited_any, False, None

    @staticmethod
    def _controls_were_replaced(
        surface: FormSurfaceBackend, snapshot: FormSnapshot
    ) -> bool:
        """True when a control seen in ``snapshot`` is no longer on the page."""
        fresh = surface.snapshot()
        now = {c.dom_selector for c in fresh.controls}
        return any(c.visible and c.dom_selector not in now for c in snapshot.controls)

    def _collect_ungrounded_required_halts(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        bindings: Sequence[FieldBindingSpec],
        *,
        in_popup_subroutine: Optional[str] = None,
    ) -> list[FormHaltItem]:
        matched_by_selector: dict[str, FieldBindingSpec] = {}
        matched_by_group: dict[str, FieldBindingSpec] = {}
        for candidate_binding in bindings:
            try:
                matched = match_anchor_in_snapshot(
                    candidate_binding.anchor, snapshot, include_readonly=False
                )
            except AmbiguousAnchorError:
                continue
            if matched is not None:
                matched_by_selector[matched.dom_selector] = candidate_binding
                group = radio_group_key(matched)
                if group is not None:
                    matched_by_group[group] = candidate_binding

        halts: list[FormHaltItem] = []
        # A radio group is one field: one halt for the group, not per radio.
        for ctrl in collapse_radio_groups(snapshot.controls):
            if not ctrl.visible or ctrl.disabled or ctrl.readonly:
                continue
            if _is_action_button(ctrl):
                continue

            binding = matched_by_selector.get(ctrl.dom_selector)
            group = radio_group_key(ctrl)
            if binding is None and group is not None:
                binding = matched_by_group.get(group)
            is_required = ctrl.required or (
                binding is not None and binding.required_in_pack
            )
            if not is_required:
                continue

            ctrl_sig = compute_field_signature(ctrl)
            fact = (
                session.effective_facts.get(binding.dossier_key)
                if binding is not None
                else None
            )
            if (
                fact is not None
                and binding is not None
                and _control_matches_fact(ctrl, fact.value, binding)
            ):
                continue
            accepted_binding = binding or FieldBindingSpec(
                binding_id="human-current-field",
                dossier_key="human-current-field",
                anchor=FieldAnchorSpec(css_selector=ctrl.dom_selector),
            )
            if ctrl_sig in session._human_accepted_values and _control_matches_fact(
                ctrl,
                session._human_accepted_values[ctrl_sig],
                accepted_binding,
            ):
                continue

            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                snapshot.screen_id,
                ctrl.dom_selector,
                ctrl_sig,
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                screen_id=snapshot.screen_id,
                control_id=ctrl.control_id,
                dom_selector=ctrl.dom_selector,
                field_label=ctrl.label_text,
                section_text=ctrl.section_text,
                dossier_key=binding.dossier_key if binding is not None else None,
                message=(
                    f"Required control {ctrl.label_text or ctrl.dom_selector!r} has no "
                    f"grounded value in dossier"
                ),
                allowed_actions=(
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
                available_options=ctrl.options,
            )
            session._halt_metadata[halt_id] = {
                "field_signature": ctrl_sig,
                "control": ctrl,
                "binding": binding,
                "in_popup_subroutine": in_popup_subroutine,
            }
            halts.append(halt)

        return halts

    def _popup_contract_halt(
        self,
        session: ReconcilerSession,
        parent_snapshot: FormSnapshot,
        subroutine: PopupSubroutineSpec,
        sig: str,
        message: str,
    ) -> FormHaltItem:
        halt_id = _deterministic_halt_id(
            session.run_id,
            FormHaltKind.POPUP_CONTRACT_FAILED,
            parent_snapshot.screen_id,
            subroutine.subroutine_id,
            sig,
        )
        halt = FormHaltItem(
            halt_id=halt_id,
            kind=FormHaltKind.POPUP_CONTRACT_FAILED,
            screen_id=parent_snapshot.screen_id,
            dossier_key=subroutine.return_output_key,
            message=message,
            allowed_actions=(
                HumanDecisionAction.FIXED_ON_SCREEN,
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            ),
        )
        session._halt_metadata[halt_id] = {"field_signature": sig}
        return halt

    def _run_popup_subroutine(
        self,
        parent_surface: FormSurfaceBackend,
        session: ReconcilerSession,
        parent_snapshot: FormSnapshot,
        subroutine: PopupSubroutineSpec,
    ) -> Optional[list[FormHaltItem]]:
        """Execute ``subroutine``; a surface failure inside it becomes a stop.

        A popup that will not open, a button that is missing or matches twice,
        or a popup that never settles ends in a ``POPUP_CONTRACT_FAILED`` halt
        for a person, never in an exception out of the run.
        """
        try:
            return self._run_popup_subroutine_steps(
                parent_surface, session, parent_snapshot, subroutine
            )
        except Exception as exc:  # noqa: BLE001 - narrowed just below
            if not _is_popup_surface_error(exc):
                raise
            sig = f"popup_error|{subroutine.subroutine_id}|{type(exc).__name__}"
            return [
                self._popup_contract_halt(
                    session,
                    parent_snapshot,
                    subroutine,
                    sig,
                    f"Popup {subroutine.subroutine_id!r} could not be run: {exc}",
                )
            ]

    @contextlib.contextmanager
    def _open_in_page_popup(
        self, parent_surface: FormSurfaceBackend, trigger_selector: str
    ) -> Any:
        parent_surface.click(trigger_selector)
        yield parent_surface

    @staticmethod
    def _scope_to_new_controls(
        snapshot: FormSnapshot, scope: Optional["_InPageScope"]
    ) -> FormSnapshot:
        """Keep only an in-page popup's own controls (see ``_InPageScope``)."""
        if scope is None:
            return snapshot
        return scope.apply(snapshot)

    def _run_popup_subroutine_steps(
        self,
        parent_surface: FormSurfaceBackend,
        session: ReconcilerSession,
        parent_snapshot: FormSnapshot,
        subroutine: PopupSubroutineSpec,
    ) -> Optional[list[FormHaltItem]]:
        """Execute ``subroutine``, returning ``None`` on verified return or halts on failure."""
        try:
            trigger_ctrl = match_anchor_in_snapshot(
                subroutine.trigger_anchor, parent_snapshot, include_readonly=False
            )
            return_before = match_anchor_in_snapshot(
                subroutine.parent_return_anchor, parent_snapshot, include_readonly=True
            )
        except AmbiguousAnchorError as exc:
            sig = f"popup_anchor|{subroutine.subroutine_id}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                parent_snapshot.screen_id,
                subroutine.subroutine_id,
                sig,
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                screen_id=parent_snapshot.screen_id,
                dossier_key=subroutine.return_output_key,
                message=str(exc),
                allowed_actions=(
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {"field_signature": sig}
            return [halt]

        if trigger_ctrl is None:
            return None

        if return_before is None:
            sig = f"popup_return|{subroutine.subroutine_id}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.POPUP_CONTRACT_FAILED,
                parent_snapshot.screen_id,
                subroutine.subroutine_id,
                sig,
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.POPUP_CONTRACT_FAILED,
                screen_id=parent_snapshot.screen_id,
                dossier_key=subroutine.return_output_key,
                message=(
                    f"Popup parent_return_anchor for {subroutine.subroutine_id!r} "
                    f"was not found on parent screen"
                ),
                allowed_actions=(
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {"field_signature": sig}
            return [halt]

        if not _is_return_value_empty_or_zero(
            return_before.current_value, subroutine.return_codec
        ):
            existing_fact = session.effective_facts.get(subroutine.return_output_key)
            if existing_fact is not None and values_equivalent(
                existing_fact.value,
                return_before.current_value,
                subroutine.return_codec,
            ):
                session.subroutines_completed.append(subroutine.subroutine_id)
                return None
            ctrl_sig = compute_field_signature(return_before)
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.PREFILL_VALUE_CONFLICT,
                parent_snapshot.screen_id,
                return_before.dom_selector,
                ctrl_sig,
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                screen_id=parent_snapshot.screen_id,
                control_id=return_before.control_id,
                dom_selector=return_before.dom_selector,
                field_label=return_before.label_text,
                section_text=return_before.section_text,
                dossier_key=subroutine.return_output_key,
                portal_value=return_before.current_value,
                dossier_value=existing_fact.value if existing_fact else None,
                message=(
                    f"Popup return field for {subroutine.subroutine_id!r} was unexpectedly "
                    f"non-empty prior to running subroutine"
                ),
                allowed_actions=(
                    HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {
                "field_signature": ctrl_sig,
                "control": return_before,
            }
            return [halt]

        in_page = subroutine.popup_mode == "in_page"
        parent_selectors: Optional[_InPageScope] = (
            _InPageScope(parent_snapshot) if in_page else None
        )
        parent_errors = (
            set(_collect_surface_errors(parent_snapshot)) if in_page else set()
        )

        def _popup_snapshot(surface: FormSurfaceBackend) -> FormSnapshot:
            return self._scope_to_new_controls(surface.snapshot(), parent_selectors)

        popup_cm = (
            self._open_in_page_popup(parent_surface, trigger_ctrl.dom_selector)
            if in_page
            else parent_surface.open_popup_surface(
                trigger_ctrl.dom_selector,
                url_pattern=subroutine.popup_url_pattern,
                title_pattern=subroutine.popup_title_pattern,
            )
        )
        commit_selector: Optional[str] = None
        displayed_result: Optional[str] = None
        with popup_cm as popup_surface:
            popup_snap = _popup_snapshot(popup_surface)

            # Replay any prior manual human fills for unmapped popup controls.
            for (sub_id, sel), val in list(session._popup_manual_fills.items()):
                if sub_id != subroutine.subroutine_id:
                    continue
                matching = [
                    c
                    for c in popup_snap.controls
                    if c.visible and c.dom_selector == sel
                ]
                if matching:
                    inferred = self._infer_unmapped_control_kind(matching[0])
                    act_halt = self._actuate_control(
                        popup_surface,
                        popup_snap,
                        matching[0],
                        val,
                        control_kind=inferred,
                        codec=FieldCodecSpec(),
                        run_id=session.run_id,
                    )
                    if act_halt is not None:
                        return [act_halt]
                    session.record_write(
                        source="operator_value",
                        label=matching[0].label_text,
                        selector=sel,
                        in_popup_subroutine=sub_id,
                    )
                    popup_snap = _popup_snapshot(popup_surface)

            for _ in range(self.max_passes_per_screen):
                edited, _, bind_halt = self._reconcile_bindings_pass(
                    popup_surface,
                    session,
                    popup_snap,
                    subroutine.bindings,
                    in_popup_subroutine=subroutine.subroutine_id,
                )
                if bind_halt is not None:
                    return [bind_halt]
                if not edited:
                    break
                popup_snap = _popup_snapshot(popup_surface)

            popup_ungrounded = self._collect_ungrounded_required_halts(
                session,
                popup_snap,
                subroutine.bindings,
                in_popup_subroutine=subroutine.subroutine_id,
            )
            if popup_ungrounded:
                return popup_ungrounded

            # Intermediate steps (for example Calculate) before apply. Inputs
            # are re-read before the first step and again before apply,
            # because a step may re-render the popup.
            if subroutine.compute_anchors:
                entry_halt = self._check_popup_entries_before_apply(
                    session,
                    popup_surface,
                    parent_snapshot,
                    subroutine,
                    scope=parent_selectors,
                )
                if entry_halt is not None:
                    return [entry_halt]
            for step_index, step_anchor in enumerate(subroutine.compute_anchors):
                popup_snap = _popup_snapshot(popup_surface)
                try:
                    step_ctrl = match_anchor_in_snapshot(
                        step_anchor, popup_snap, include_readonly=False
                    )
                except AmbiguousAnchorError as exc:
                    step_ctrl = None
                    step_err = str(exc)
                else:
                    step_err = (
                        f"Popup step {step_index + 1} button not found for "
                        f"{subroutine.subroutine_id!r}"
                    )
                if step_ctrl is None:
                    return [
                        self._popup_contract_halt(
                            session,
                            parent_snapshot,
                            subroutine,
                            f"popup_step|{subroutine.subroutine_id}|{step_index}",
                            step_err,
                        )
                    ]
                popup_surface.click(step_ctrl.dom_selector)
                popup_snap = _popup_snapshot(popup_surface)
                step_errors = [
                    e
                    for e in _collect_surface_errors(popup_snap)
                    if e not in parent_errors
                ]
                if step_errors:
                    return [
                        self._popup_contract_halt(
                            session,
                            parent_snapshot,
                            subroutine,
                            f"popup_step_error|{subroutine.subroutine_id}|{step_index}",
                            f"Popup {subroutine.subroutine_id!r} showed an error "
                            f"after step {step_index + 1}: {step_errors}",
                        )
                    ]

            entry_halt = self._check_popup_entries_before_apply(
                session,
                popup_surface,
                parent_snapshot,
                subroutine,
                scope=parent_selectors,
            )
            if entry_halt is not None:
                return [entry_halt]
            popup_snap = _popup_snapshot(popup_surface)

            try:
                popup_commit = match_anchor_in_snapshot(
                    subroutine.commit_anchor, popup_snap, include_readonly=False
                )
            except AmbiguousAnchorError as exc:
                popup_commit = None
                commit_err = str(exc)
            else:
                commit_err = (
                    f"Popup commit button not found for {subroutine.subroutine_id!r}"
                )

            if popup_commit is None:
                return [
                    self._popup_contract_halt(
                        session,
                        parent_snapshot,
                        subroutine,
                        f"popup_commit|{subroutine.subroutine_id}",
                        commit_err,
                    )
                ]

            if subroutine.result_anchor is not None:
                displayed_result, result_err = self._read_popup_result(
                    popup_snap, subroutine
                )
                if result_err is not None:
                    return [
                        self._popup_contract_halt(
                            session,
                            parent_snapshot,
                            subroutine,
                            f"popup_result|{subroutine.subroutine_id}",
                            result_err,
                        )
                    ]

            commit_selector = popup_commit.dom_selector
            popup_surface.click(commit_selector)

        # Verify parent return anchor after popup closes.
        after_parent_snap = parent_surface.snapshot(
            entity_tables=session.pack.entity_tables,
            modal_rules=session.pack.modal_rules,
            screens=session.pack.screens,
        )
        if in_page and any(
            c.visible and c.dom_selector == commit_selector
            for c in after_parent_snap.controls
        ):
            return [
                self._popup_contract_halt(
                    session,
                    parent_snapshot,
                    subroutine,
                    f"popup_open|{subroutine.subroutine_id}",
                    f"In-page popup {subroutine.subroutine_id!r} is still open "
                    f"after its apply button was clicked",
                )
            ]
        return_after = match_anchor_in_snapshot(
            subroutine.parent_return_anchor,
            after_parent_snap,
            include_readonly=True,
        )
        if return_after is None or not _is_valid_non_zero_return_value(
            return_after.current_value, subroutine.return_codec
        ):
            sig = f"popup_return|{subroutine.subroutine_id}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.POPUP_CONTRACT_FAILED,
                after_parent_snap.screen_id,
                return_before.dom_selector,
                sig,
            )
            obs_ret = return_after.current_value if return_after is not None else None
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.POPUP_CONTRACT_FAILED,
                screen_id=after_parent_snap.screen_id,
                control_id=return_after.control_id if return_after else None,
                dom_selector=return_after.dom_selector
                if return_after
                else return_before.dom_selector,
                field_label=return_after.label_text
                if return_after
                else return_before.label_text,
                dossier_key=subroutine.return_output_key,
                portal_value=obs_ret,
                message=(
                    f"Popup subroutine {subroutine.subroutine_id!r} exited without "
                    f"populating a valid {subroutine.return_codec.kind.value} value in "
                    f"{subroutine.return_output_key!r}"
                ),
                allowed_actions=(
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {
                "field_signature": sig,
                "control": return_after or return_before,
            }
            return [halt]

        # The value put back in the parent must be the figure the popup showed.
        # An estimator that shows one figure and hands back another (rounded,
        # capped, stale) stops here rather than going through unseen.
        if displayed_result is not None and not values_equivalent(
            displayed_result, return_after.current_value, subroutine.return_codec
        ):
            ctrl_sig = compute_field_signature(return_after)
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.PREFILL_VALUE_CONFLICT,
                after_parent_snap.screen_id,
                return_after.dom_selector,
                f"popup_result_mismatch|{ctrl_sig}",
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                screen_id=after_parent_snap.screen_id,
                control_id=return_after.control_id,
                dom_selector=return_after.dom_selector,
                field_label=return_after.label_text,
                section_text=return_after.section_text,
                dossier_key=subroutine.return_output_key,
                portal_value=return_after.current_value,
                dossier_value=displayed_result,
                message=(
                    f"Popup {subroutine.subroutine_id!r} showed {displayed_result!r} "
                    f"but put {return_after.current_value!r} in "
                    f"{subroutine.return_output_key!r}"
                ),
                allowed_actions=(
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {
                "field_signature": ctrl_sig,
                "control": return_after,
            }
            return [halt]

        # A value the intake already states must match what the popup put
        # back; otherwise stop before submit rather than keep the popup's.
        existing_fact = session.effective_facts.get(subroutine.return_output_key)
        if existing_fact is not None and not values_equivalent(
            existing_fact.value, return_after.current_value, subroutine.return_codec
        ):
            ctrl_sig = compute_field_signature(return_after)
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.PREFILL_VALUE_CONFLICT,
                after_parent_snap.screen_id,
                return_after.dom_selector,
                ctrl_sig,
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                screen_id=after_parent_snap.screen_id,
                control_id=return_after.control_id,
                dom_selector=return_after.dom_selector,
                field_label=return_after.label_text,
                section_text=return_after.section_text,
                dossier_key=subroutine.return_output_key,
                portal_value=return_after.current_value,
                dossier_value=existing_fact.value,
                message=(
                    f"Popup {subroutine.subroutine_id!r} returned a value that "
                    f"differs from the intake's {subroutine.return_output_key!r}"
                ),
                allowed_actions=(
                    HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {
                "field_signature": ctrl_sig,
                "control": return_after,
            }
            return [halt]

        session.subroutines_completed.append(subroutine.subroutine_id)
        session._filled_selectors.add(return_after.dom_selector)
        session.record_write(
            source="popup_return",
            dossier_key=subroutine.return_output_key,
            label=return_after.label_text,
            selector=return_after.dom_selector,
            from_popup_subroutine=subroutine.subroutine_id,
            matched_popup_result=displayed_result is not None,
        )
        session._human_accepted_control_signatures.add(
            compute_field_signature(return_after)
        )
        if subroutine.return_output_key not in session.effective_facts:
            session.effective_facts[subroutine.return_output_key] = DossierFact(
                key=subroutine.return_output_key,
                value=return_after.current_value,
                provenance=ProvenanceKind.ACCEPTED_PORTAL_PREFILL,
                source_id=f"subroutine:{subroutine.subroutine_id}",
                confidence=1.0,
            )
        return None

    @staticmethod
    def _read_popup_result(
        popup_snap: FormSnapshot, subroutine: PopupSubroutineSpec
    ) -> tuple[Optional[str], Optional[str]]:
        """Read the popup's displayed result: ``(value, None)`` or ``(None, why)``."""
        assert subroutine.result_anchor is not None
        try:
            ctrl = match_anchor_in_snapshot(
                subroutine.result_anchor, popup_snap, include_readonly=True
            )
        except AmbiguousAnchorError as exc:
            return None, (
                f"Popup {subroutine.subroutine_id!r} result is ambiguous: {exc}"
            )
        raw = ctrl.current_value.strip() if ctrl is not None else ""
        if ctrl is None:
            # A result shown as page text ("Estimated cost: $512,340").
            try:
                pair = match_text_pair_in_snapshot(subroutine.result_anchor, popup_snap)
            except AmbiguousAnchorError as exc:
                return None, (
                    f"Popup {subroutine.subroutine_id!r} result is ambiguous: {exc}"
                )
            raw = pair.value.strip() if pair is not None else ""
        if not raw:
            return None, (
                f"Popup {subroutine.subroutine_id!r} shows no result to check "
                f"the returned value against"
            )
        try:
            normalize_with_codec(raw, subroutine.return_codec)
        except CodecValueError:
            return None, (
                f"Popup {subroutine.subroutine_id!r} result {raw!r} is not a "
                f"{subroutine.return_codec.kind.value} value"
            )
        return raw, None

    def _check_popup_entries_before_apply(
        self,
        session: ReconcilerSession,
        popup_surface: FormSurfaceBackend,
        parent_snapshot: FormSnapshot,
        subroutine: PopupSubroutineSpec,
        *,
        scope: Optional["_InPageScope"] = None,
    ) -> Optional[FormHaltItem]:
        """Re-read every field typed in the popup before its apply button.

        A popup's inputs are often not stored with the record, so the summary
        cannot show them. Reading them back here, before they are used, is the
        only check they can get before use. A mismatch or a missing control
        stops the run before apply. A match is noted on the write, but the
        field is still reported as not_checked: a re-read of the popup is not a
        read-back of what the application recorded.
        """
        snap = self._scope_to_new_controls(popup_surface.snapshot(), scope)
        problems: list[str] = []
        for ledger_key, write in list(session._writes.items()):
            if write.in_popup_subroutine != subroutine.subroutine_id:
                continue
            # The binding that wrote this control: matched by its selector,
            # never just by key (two inputs may hold the same key).
            binding = None
            for b in subroutine.bindings:
                if not write.dossier_key or b.dossier_key != write.dossier_key:
                    continue
                try:
                    m = match_anchor_in_snapshot(b.anchor, snap, include_readonly=False)
                except AmbiguousAnchorError:
                    m = None
                if m is not None and m.dom_selector == write.selector:
                    binding = b
                    break
            if binding is not None:
                fact = session.effective_facts.get(binding.dossier_key)
                try:
                    ctrl = match_anchor_in_snapshot(
                        binding.anchor, snap, include_readonly=False
                    )
                except AmbiguousAnchorError:
                    ctrl = None
                ok = (
                    fact is not None
                    and ctrl is not None
                    and _control_matches_fact(ctrl, fact.value, binding)
                )
            else:
                intended = session._popup_manual_fills.get(
                    (subroutine.subroutine_id, write.selector or "")
                )
                ctrl = next(
                    (
                        c
                        for c in snap.controls
                        if c.visible and c.dom_selector == write.selector
                    ),
                    None,
                )
                ok = (
                    intended is not None
                    and ctrl is not None
                    and values_equivalent(
                        intended, ctrl.current_value, FieldCodecSpec()
                    )
                )
            if ok:
                session._writes[ledger_key] = write.model_copy(
                    update={"checked_before_apply": True}
                )
            else:
                problems.append(write.label or write.field_key)
        if not problems:
            return None
        sig = f"popup_entry|{subroutine.subroutine_id}|{'|'.join(sorted(problems))}"
        halt_id = _deterministic_halt_id(
            session.run_id,
            FormHaltKind.POPUP_CONTRACT_FAILED,
            parent_snapshot.screen_id,
            subroutine.subroutine_id,
            sig,
        )
        halt = FormHaltItem(
            halt_id=halt_id,
            kind=FormHaltKind.POPUP_CONTRACT_FAILED,
            screen_id=parent_snapshot.screen_id,
            dossier_key=subroutine.return_output_key,
            message=(
                f"Popup {subroutine.subroutine_id!r} does not hold the intended "
                f"value in {problems} before apply"
            ),
            allowed_actions=(
                HumanDecisionAction.FIXED_ON_SCREEN,
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            ),
        )
        session._halt_metadata[halt_id] = {"field_signature": sig}
        return halt

    def _emit_auth_wall(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        reason: str,
        *,
        delivery_uncertain: bool,
    ) -> FormTxResult:
        """Stop at a sign-in wall.

        Before any save request went out, the operator may sign in and the
        run resumes (``FIXED_ON_SCREEN``). After a save request went out, the
        record may or may not exist, so resuming could submit twice: the
        outcome is ``delivery_uncertain`` and the only choices are to hand it
        to the owner or abort.
        """
        sig = f"auth_wall|{snapshot.url_origin_and_path}"
        halt_id = _deterministic_halt_id(
            session.run_id,
            FormHaltKind.AUTH_WALL_DETECTED,
            snapshot.screen_id,
            snapshot.url_origin_and_path,
            sig,
        )
        message = (
            f"Sign-in wall at {snapshot.url_origin_and_path!r} ({reason})"
            if not delivery_uncertain
            else (
                f"Sign-in wall at {snapshot.url_origin_and_path!r} ({reason}) "
                "after a save request was sent; the record may or may not exist"
            )
        )
        allowed: tuple[HumanDecisionAction, ...] = (
            (HumanDecisionAction.ESCALATE_TO_OWNER, HumanDecisionAction.ABORT_RUN)
            if delivery_uncertain
            else (
                HumanDecisionAction.FIXED_ON_SCREEN,
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            )
        )
        halt = FormHaltItem(
            halt_id=halt_id,
            kind=FormHaltKind.AUTH_WALL_DETECTED,
            screen_id=snapshot.screen_id,
            message=message,
            allowed_actions=allowed,
        )
        session._halt_metadata[halt_id] = {"field_signature": sig}
        return self._build_result(
            session,
            outcome=(
                FormTxOutcome.DELIVERY_UNCERTAIN
                if delivery_uncertain
                else FormTxOutcome.AUTH_REQUIRED
            ),
            safe_to_retry=not delivery_uncertain,
            halts=(halt,),
            what_happened=message,
        )

    def _emit_validation_blocked(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        errors: Sequence[str],
        *,
        safe_to_retry: bool,
    ) -> FormTxResult:
        bound = session._bound_surface or self._surface
        auth_reason = _auth_wall_reason(
            snapshot, bound.ui_profile if bound is not None else None
        )
        if auth_reason is not None:
            # safe_to_retry is False only when a save request went out.
            return self._emit_auth_wall(
                session,
                snapshot,
                auth_reason,
                delivery_uncertain=(not safe_to_retry) or session._commit_attempted,
            )
        errored_ctrl = next(
            (c for c in snapshot.controls if c.visible and c.error_text.strip()),
            None,
        )
        matched_binding: Optional[FieldBindingSpec] = None
        if errored_ctrl is not None:
            for b in session.pack.bindings:
                try:
                    m = match_anchor_in_snapshot(
                        b.anchor, snapshot, include_readonly=False
                    )
                except AmbiguousAnchorError:
                    continue
                if m is not None and m.dom_selector == errored_ctrl.dom_selector:
                    matched_binding = b
                    break

        sig = (
            compute_field_signature(errored_ctrl)
            if errored_ctrl is not None
            else f"validation_banner|{snapshot.screen_id or snapshot.url_origin_and_path}"
        )
        locator = (
            errored_ctrl.dom_selector
            if errored_ctrl is not None
            else (snapshot.screen_id or snapshot.url_origin_and_path)
        )
        halt_id = _deterministic_halt_id(
            session.run_id,
            FormHaltKind.PORTAL_VALIDATION_BLOCKED,
            snapshot.screen_id,
            locator,
            sig,
        )
        msg = "; ".join(errors) if errors else "Portal blocked screen transition"
        halt = FormHaltItem(
            halt_id=halt_id,
            kind=FormHaltKind.PORTAL_VALIDATION_BLOCKED,
            screen_id=snapshot.screen_id,
            control_id=errored_ctrl.control_id if errored_ctrl else None,
            dom_selector=errored_ctrl.dom_selector if errored_ctrl else None,
            field_label=errored_ctrl.label_text if errored_ctrl else "",
            section_text=errored_ctrl.section_text if errored_ctrl else "",
            dossier_key=matched_binding.dossier_key if matched_binding else None,
            portal_value=list(errors),
            message=msg,
            allowed_actions=(
                HumanDecisionAction.PROVIDE_VALUE,
                HumanDecisionAction.FIXED_ON_SCREEN,
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            ),
            available_options=errored_ctrl.options if errored_ctrl else (),
        )
        session._halt_metadata[halt_id] = {
            "field_signature": sig,
            "control": errored_ctrl,
            "binding": matched_binding,
        }
        return self._build_result(
            session,
            outcome=FormTxOutcome.VALIDATION_BLOCKED,
            safe_to_retry=safe_to_retry,
            halts=(halt,),
            what_happened=msg,
        )

    def _finalize_commit(
        self,
        surface: FormSurfaceBackend,
        session: ReconcilerSession,
        post_commit_snapshot: FormSnapshot,
        pre_commit_mutations: int,
    ) -> FormTxResult:
        pack = session.pack
        auth_reason = _auth_wall_reason(post_commit_snapshot, surface.ui_profile)
        if auth_reason is not None:
            # A sign-in page after the commit (with or without an error
            # banner) never counts as a finished save.
            return self._emit_auth_wall(
                session,
                post_commit_snapshot,
                auth_reason,
                delivery_uncertain=(
                    session._commit_attempted
                    or surface.mutation_count > pre_commit_mutations
                ),
            )
        delta_mutations = max(0, surface.mutation_count - pre_commit_mutations)
        has_commit_endpoint = bool(pack.commit_url_patterns)
        has_record_lookup = bool(
            pack.summary_check
            and pack.summary_check.search_by_output_key
            and pack.summary_check.search_input_anchor is not None
            and pack.summary_check.search_submit_anchor is not None
        )
        has_confirmed_fn = getattr(surface, "has_confirmed_mutation_since", None)
        if not callable(has_confirmed_fn):
            save_confirmed = False
        elif has_commit_endpoint:
            save_confirmed = bool(
                has_confirmed_fn(
                    pre_commit_mutations, url_patterns=pack.commit_url_patterns
                )
            )
        elif has_record_lookup:
            save_confirmed = bool(has_confirmed_fn(pre_commit_mutations))
        else:
            # Without either a declared commit endpoint pattern or a record-specific
            # summary lookup, a same-page draft autosave cannot be distinguished
            # from the intended commit.
            save_confirmed = False
        no_confirmed_save = not save_confirmed
        still_on_commit_screen = self._is_still_on_uncommitted_commit_screen(
            post_commit_snapshot, pack
        )
        if delta_mutations == 0 or no_confirmed_save or still_on_commit_screen:
            # absent-evidence-as-success: a commit click that sent no save
            # request is no evidence the record exists; reading the form back
            # would only read the run's own edits.
            sig = f"commit_no_save|{pack.pack_id}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                post_commit_snapshot.screen_id,
                pack.pack_id,
                sig,
            )
            msg = (
                "The commit control was clicked but no save request was seen"
                if delta_mutations == 0
                else "No save request was answered with success"
                if no_confirmed_save
                else "The page is still on the commit screen after the commit click"
            ) + "; there is no evidence the record was saved"
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                screen_id=post_commit_snapshot.screen_id,
                portal_value=0,
                message=msg,
                allowed_actions=(
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {"field_signature": sig}
            return self._build_result(
                session,
                outcome=FormTxOutcome.DELIVERY_UNCERTAIN,
                safe_to_retry=False,
                halts=(halt,),
                what_happened=msg,
            )
        if delta_mutations > pack.max_commit_mutations:
            sig = f"commit_budget|{pack.pack_id}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                post_commit_snapshot.screen_id,
                pack.pack_id,
                sig,
            )
            msg = (
                f"Observed {delta_mutations} commit mutations exceeding "
                f"max_commit_mutations={pack.max_commit_mutations}"
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                screen_id=post_commit_snapshot.screen_id,
                portal_value=delta_mutations,
                dossier_value=pack.max_commit_mutations,
                message=msg,
                allowed_actions=(
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {"field_signature": sig}
            return self._build_result(
                session,
                outcome=FormTxOutcome.DELIVERY_UNCERTAIN,
                safe_to_retry=False,
                halts=(halt,),
                what_happened=msg,
            )

        verdict = self._summary_judge.evaluate(
            surface=surface,
            pack=pack,
            dossier=session.dossier,
            human_resolutions=session.human_resolutions,
            supplemental_facts=session.effective_facts,
            writes=session.writes,
        )
        extracted_outputs = dict(verdict.extracted_outputs)
        if not verdict.passed or verdict.missing_required_outputs:
            refuted = bool(
                verdict.failed_keys
                or verdict.missing_required_outputs
                or not verdict.not_checked_keys
            )
            if refuted:
                reasons = list(verdict.failed_keys) + [
                    f"missing_output:{k}" for k in verdict.missing_required_outputs
                ]
                if not reasons:
                    reasons = ["summary screen not in a readable state"]
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.READBACK_REFUTED,
                    safe_to_retry=False,
                    outputs=extracted_outputs,
                    summary_verdict=verdict,
                    receipt=None,
                    what_happened=(
                        f"Read-back by the acting session did not verify commit: {reasons}"
                    ),
                )
            return self._build_result(
                session,
                outcome=FormTxOutcome.READBACK_INCOMPLETE,
                safe_to_retry=False,
                outputs=extracted_outputs,
                summary_verdict=verdict,
                receipt=None,
                what_happened=(
                    "Submitted, but these fields could not be read back and "
                    f"need a person to check them: {list(verdict.not_checked_keys)}"
                ),
            )

        receipt = issue_form_tx_receipt(
            run_id=session.run_id,
            pack=pack,
            dossier=session.dossier,
            signing_key=session.signing_key,
            outcome=FormTxOutcome.VERIFIED_DONE,
            fields_filled_auto=session.fields_filled_auto,
            human_resolutions=session.human_resolutions,
            commit_mutations_observed=delta_mutations,
            summary_verdict=verdict,
            outputs=extracted_outputs,
            writes=session.writes,
        )
        return self._build_result(
            session,
            outcome=FormTxOutcome.VERIFIED_DONE,
            safe_to_retry=False,
            outputs=extracted_outputs,
            summary_verdict=verdict,
            receipt=receipt,
            what_happened="Committed and verified by the acting session's read-back",
        )

    def _is_still_on_uncommitted_commit_screen(
        self,
        snapshot: FormSnapshot,
        pack: FormPack,
    ) -> bool:
        screen = _identify_wizard_screen(snapshot, pack.screens)
        if screen is None or not (
            screen.is_commit_screen or screen.commit_button_anchor is not None
        ):
            return False
        if screen.commit_button_anchor is None:
            return False
        try:
            return (
                match_anchor_in_snapshot(screen.commit_button_anchor, snapshot)
                is not None
            )
        except AmbiguousAnchorError:
            return True

    def _surface_error_result(
        self,
        session: ReconcilerSession,
        active_surface: FormSurfaceBackend,
        exc: BaseException,
        mutations_at_entry: int,
    ) -> FormTxResult:
        """Turn a surface error into a stop; after a submit click, delivery is uncertain."""
        snapshot = session._last_observation or FormSnapshot(url_origin_and_path="")
        halt = _surface_action_halt(
            session.run_id,
            snapshot,
            exc,
            locator=snapshot.url_origin_and_path or session.pack.pack_id,
        )
        after_commit = session._commit_in_progress or session._commit_clicked
        if after_commit:
            halt = halt.model_copy(
                update={
                    "allowed_actions": (
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                    "message": "After the submit click: " + halt.message,
                }
            )
        session._halt_metadata[halt.halt_id] = {
            "field_signature": f"surface_action|{halt.dom_selector or ''}"
        }
        session._commit_in_progress = False
        try:
            mutated = active_surface.mutation_count != mutations_at_entry
        except Exception:
            mutated = True
        return self._build_result(
            session,
            outcome=(
                FormTxOutcome.DELIVERY_UNCERTAIN
                if after_commit
                else FormTxOutcome.HALTED_NEEDS_HUMAN
            ),
            safe_to_retry=not after_commit and not mutated,
            halts=(halt,),
            what_happened=halt.message,
        )

    def run_until_halt_or_done(
        self,
        session: ReconcilerSession,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Advance ``session`` deterministically until verified completion or a governed halt.

        A surface error (a control that vanished or matched twice, a listbox or
        popup that never opened, a page that never settled) ends the run as a
        stop for a person. It is never raised past this point and never
        followed by a submit; after the submit click it is reported as
        ``delivery_uncertain``.

        A session with an open stop does not move: a stop is cleared only by
        ``apply_human_resolution`` (absent-evidence-as-success: a retry is not
        a decision).
        """
        if session.active_halts and not session._resuming:
            return self._build_result(
                session,
                outcome=(
                    session._last_result.outcome
                    if session._last_result is not None
                    else FormTxOutcome.HALTED_NEEDS_HUMAN
                ),
                # Never more retryable than the stop it repeats.
                safe_to_retry=(
                    session._last_result.safe_to_retry
                    if session._last_result is not None
                    else False
                ),
                halts=tuple(session.active_halts),
                what_happened=(
                    "Refused to continue: a stop is still open and needs a "
                    "decision through apply_human_resolution"
                ),
            )
        session._resuming = False
        active_surface = self._resolve_surface(surface, session)
        try:
            mutations_at_entry = active_surface.mutation_count
        except Exception:
            mutations_at_entry = 0
        try:
            return self._run_until_halt_or_done_unguarded(
                session, active_surface=active_surface
            )
        except Exception as exc:
            if not _is_surface_action_error(exc):
                raise
            return self._surface_error_result(
                session, active_surface, exc, mutations_at_entry
            )

    def _run_until_halt_or_done_unguarded(
        self,
        session: ReconcilerSession,
        *,
        active_surface: FormSurfaceBackend,
    ) -> FormTxResult:
        pack = session.pack
        session.active_halts.clear()
        if session._mutations_at_start is None:
            try:
                session._mutations_at_start = active_surface.mutation_count
            except Exception:  # noqa: BLE001
                session._mutations_at_start = 0

        for _ in range(self.max_screen_transitions):
            snapshot = active_surface.snapshot(
                entity_tables=pack.entity_tables,
                modal_rules=pack.modal_rules,
                screens=pack.screens,
            )

            session._last_observation = snapshot

            # 2. Auth Wall Check
            auth_reason = _auth_wall_reason(snapshot, active_surface.ui_profile)
            if auth_reason is not None:
                return self._emit_auth_wall(
                    session,
                    snapshot,
                    auth_reason,
                    delivery_uncertain=(
                        session._commit_attempted or session._commit_in_progress
                    ),
                )

            # 3. Active Modal Check
            if snapshot.active_modals:
                obs_modal = snapshot.active_modals[0]
                rule = self._match_modal_rule(obs_modal, pack.modal_rules)
                flag_truthy = False
                if (
                    rule is not None
                    and rule.policy == "confirm_if_dossier_flag"
                    and rule.dossier_flag_key
                ):
                    flag_fact = session.effective_facts.get(
                        rule.dossier_flag_key
                    ) or session.dossier.grounded_fact(rule.dossier_flag_key)
                    if flag_fact is not None and _is_truthy_flag(flag_fact.value):
                        flag_truthy = True

                if rule is not None and flag_truthy:
                    active_surface.click(rule.confirm_selector)
                    session.confirmed_modals.add(obs_modal.modal_id)
                    continue

                confirm_sel = (
                    rule.confirm_selector
                    if rule is not None
                    else obs_modal.container_selector
                )
                sig = f"modal|{obs_modal.modal_id}"
                halt_id = _deterministic_halt_id(
                    session.run_id,
                    FormHaltKind.MODAL_CONFIRMATION_REQUIRED,
                    snapshot.screen_id,
                    confirm_sel,
                    sig,
                )
                halt = FormHaltItem(
                    halt_id=halt_id,
                    kind=FormHaltKind.MODAL_CONFIRMATION_REQUIRED,
                    screen_id=snapshot.screen_id,
                    dom_selector=confirm_sel,
                    field_label=obs_modal.modal_id,
                    dossier_key=rule.dossier_flag_key if rule is not None else None,
                    portal_value=obs_modal.message_text,
                    message=(
                        f"Modal {obs_modal.modal_id!r} requires explicit confirmation: "
                        f"{obs_modal.message_text or 'confirmation required'}"
                    ),
                    allowed_actions=(
                        HumanDecisionAction.CONFIRM_MODAL_ACTION,
                        HumanDecisionAction.ABORT_RUN,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                    ),
                )
                session._halt_metadata[halt_id] = {
                    "field_signature": sig,
                    "modal": obs_modal,
                    "modal_rule": rule,
                    "confirm_selector": confirm_sel,
                }
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=not session._commit_in_progress,
                    halts=(halt,),
                    what_happened=halt.message,
                )

            # Resume completion if a commit-triggered modal was just confirmed.
            if session._commit_in_progress:
                pre_muts = session._pre_commit_mutations
                errors = _collect_surface_errors(snapshot)
                if errors:
                    session._commit_in_progress = False
                    delta = active_surface.mutation_count - pre_muts
                    return self._emit_validation_blocked(
                        session,
                        snapshot,
                        errors,
                        safe_to_retry=(delta == 0),
                    )
                if (
                    active_surface.mutation_count > pre_muts
                    or not self._is_still_on_uncommitted_commit_screen(snapshot, pack)
                ):
                    session._commit_in_progress = False
                    return self._finalize_commit(
                        active_surface, session, snapshot, pre_muts
                    )
                session._commit_in_progress = False

            # 4. Entity Table Prefill Conflict Check
            entity_halt = self._check_entity_table_conflicts(session, snapshot)
            if entity_halt is not None:
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=True,
                    halts=(entity_halt,),
                    what_happened=entity_halt.message,
                )

            # 5. Screen Field Reconciliation Fixed-Point Loop + Popup Subroutines
            re_check_outer = False
            settled = False
            for _ in range(self.max_passes_per_screen):
                edited, _, bind_halt = self._reconcile_bindings_pass(
                    active_surface, session, snapshot, pack.bindings
                )
                if bind_halt is not None:
                    return self._build_result(
                        session,
                        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                        safe_to_retry=True,
                        halts=(bind_halt,),
                        what_happened=bind_halt.message,
                    )

                if edited:
                    snapshot = active_surface.snapshot(
                        entity_tables=pack.entity_tables,
                        modal_rules=pack.modal_rules,
                        screens=pack.screens,
                    )
                    if snapshot.active_modals:
                        re_check_outer = True
                        break
                    entity_halt = self._check_entity_table_conflicts(session, snapshot)
                    if entity_halt is not None:
                        return self._build_result(
                            session,
                            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                            safe_to_retry=True,
                            halts=(entity_halt,),
                            what_happened=entity_halt.message,
                        )
                    continue

                # Bindings settled; check eligible popup subroutines on this screen.
                ran_subroutine = False
                for sub in pack.popup_subroutines:
                    if sub.subroutine_id in session.subroutines_completed:
                        continue
                    if sub.condition_dossier_key:
                        cond_fact = session.effective_facts.get(
                            sub.condition_dossier_key
                        )
                        if cond_fact is None or not _is_truthy_flag(cond_fact.value):
                            continue
                    try:
                        trig = match_anchor_in_snapshot(
                            sub.trigger_anchor, snapshot, include_readonly=False
                        )
                    except AmbiguousAnchorError:
                        # Delegated to _run_popup_subroutine to emit structured halt
                        trig = ObservedControl(
                            control_id="__ambiguous__", dom_selector="__ambiguous__"
                        )
                    if trig is None:
                        continue

                    muts_before = _mutation_count_or_none(active_surface)
                    sub_halts = self._run_popup_subroutine(
                        active_surface, session, snapshot, sub
                    )
                    if sub_halts:
                        muts_after = _mutation_count_or_none(active_surface)
                        return self._build_result(
                            session,
                            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                            # Safe to retry only if the popup sent nothing.
                            safe_to_retry=(
                                muts_before is not None and muts_before == muts_after
                            ),
                            halts=sub_halts,
                            what_happened=sub_halts[0].message,
                        )
                    ran_subroutine = True
                    snapshot = active_surface.snapshot(
                        entity_tables=pack.entity_tables,
                        modal_rules=pack.modal_rules,
                        screens=pack.screens,
                    )
                    if snapshot.active_modals:
                        re_check_outer = True
                    break

                if re_check_outer:
                    break
                if ran_subroutine:
                    continue

                settled = True
                break

            if re_check_outer:
                continue

            if not settled:
                sig = (
                    f"pass_budget|{snapshot.screen_id or snapshot.url_origin_and_path}"
                )
                halt_id = _deterministic_halt_id(
                    session.run_id,
                    FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                    snapshot.screen_id,
                    pack.pack_id,
                    sig,
                )
                msg = (
                    f"Screen failed to settle within "
                    f"max_passes_per_screen={self.max_passes_per_screen}"
                )
                halt = FormHaltItem(
                    halt_id=halt_id,
                    kind=FormHaltKind.MUTATION_BUDGET_EXCEEDED,
                    screen_id=snapshot.screen_id,
                    message=msg,
                    allowed_actions=(
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                )
                session._halt_metadata[halt_id] = {"field_signature": sig}
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.DELIVERY_UNCERTAIN,
                    safe_to_retry=False,
                    halts=(halt,),
                    what_happened=msg,
                )

            # Epistemic Abstention Check on settled snapshot
            ungrounded_halts = self._collect_ungrounded_required_halts(
                session, snapshot, pack.bindings
            )
            if ungrounded_halts:
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=True,
                    halts=ungrounded_halts,
                    what_happened=ungrounded_halts[0].message,
                )

            # 6. Screen Transition / Commit & Validation Check
            current_screen = _identify_wizard_screen(snapshot, pack.screens)
            if current_screen is not None and (
                current_screen.is_commit_screen
                or current_screen.commit_button_anchor is not None
            ):
                if current_screen.commit_button_anchor is None:
                    raise ValueError(
                        f"Commit screen {current_screen.screen_id!r} has no commit_button_anchor"
                    )
                try:
                    commit_ctrl = match_anchor_in_snapshot(
                        current_screen.commit_button_anchor,
                        snapshot,
                        include_readonly=False,
                    )
                except AmbiguousAnchorError as exc:
                    sig = f"commit_anchor|{current_screen.screen_id}"
                    halt_id = _deterministic_halt_id(
                        session.run_id,
                        FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        snapshot.screen_id,
                        current_screen.screen_id,
                        sig,
                    )
                    halt = FormHaltItem(
                        halt_id=halt_id,
                        kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        screen_id=snapshot.screen_id,
                        message=str(exc),
                        allowed_actions=(
                            HumanDecisionAction.FIXED_ON_SCREEN,
                            HumanDecisionAction.ESCALATE_TO_OWNER,
                            HumanDecisionAction.ABORT_RUN,
                        ),
                    )
                    session._halt_metadata[halt_id] = {"field_signature": sig}
                    return self._build_result(
                        session,
                        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                        safe_to_retry=True,
                        halts=(halt,),
                        what_happened=str(exc),
                    )

                if commit_ctrl is None:
                    return self._emit_validation_blocked(
                        session,
                        snapshot,
                        [
                            f"Commit button missing on commit screen {current_screen.screen_id!r}"
                        ],
                        safe_to_retry=True,
                    )

                pre_commit_mutations = active_surface.mutation_count
                session._pre_commit_mutations = pre_commit_mutations
                session._pre_commit_confirmed = getattr(
                    active_surface, "confirmed_mutation_count", None
                )
                session._commit_in_progress = True
                session._commit_clicked = True

                session._commit_attempted = True
                active_surface.click(commit_ctrl.dom_selector)

                post_snap = active_surface.snapshot(
                    entity_tables=pack.entity_tables,
                    modal_rules=pack.modal_rules,
                    screens=pack.screens,
                )
                if post_snap.active_modals:
                    # Loop back to Step 3 to handle modal policy with _commit_in_progress=True.
                    continue

                session._commit_in_progress = False
                post_errors = _collect_surface_errors(post_snap)
                if post_errors:
                    delta = active_surface.mutation_count - pre_commit_mutations
                    return self._emit_validation_blocked(
                        session,
                        post_snap,
                        post_errors,
                        safe_to_retry=(delta == 0),
                    )

                return self._finalize_commit(
                    active_surface, session, post_snap, pre_commit_mutations
                )

            if (
                current_screen is not None
                and current_screen.next_button_anchor is not None
            ):
                try:
                    next_ctrl = match_anchor_in_snapshot(
                        current_screen.next_button_anchor,
                        snapshot,
                        include_readonly=False,
                    )
                except AmbiguousAnchorError as exc:
                    sig = f"next_anchor|{current_screen.screen_id}"
                    halt_id = _deterministic_halt_id(
                        session.run_id,
                        FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        snapshot.screen_id,
                        current_screen.screen_id,
                        sig,
                    )
                    halt = FormHaltItem(
                        halt_id=halt_id,
                        kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        screen_id=snapshot.screen_id,
                        message=str(exc),
                        allowed_actions=(
                            HumanDecisionAction.FIXED_ON_SCREEN,
                            HumanDecisionAction.ESCALATE_TO_OWNER,
                            HumanDecisionAction.ABORT_RUN,
                        ),
                    )
                    session._halt_metadata[halt_id] = {"field_signature": sig}
                    return self._build_result(
                        session,
                        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                        safe_to_retry=True,
                        halts=(halt,),
                        what_happened=str(exc),
                    )

                if next_ctrl is None:
                    return self._emit_validation_blocked(
                        session,
                        snapshot,
                        [f"Next button missing on screen {current_screen.screen_id!r}"],
                        safe_to_retry=True,
                    )

                before_sig = (
                    snapshot.screen_id,
                    snapshot.url_origin_and_path,
                    snapshot.title,
                )
                active_surface.click(next_ctrl.dom_selector)
                post_snap = active_surface.snapshot(
                    entity_tables=pack.entity_tables,
                    modal_rules=pack.modal_rules,
                    screens=pack.screens,
                )
                if post_snap.active_modals:
                    continue

                post_errors = _collect_surface_errors(post_snap)
                after_sig = (
                    post_snap.screen_id,
                    post_snap.url_origin_and_path,
                    post_snap.title,
                )
                if post_errors and (after_sig == before_sig or post_snap.banner_errors):
                    return self._emit_validation_blocked(
                        session,
                        post_snap,
                        post_errors,
                        safe_to_retry=True,
                    )
                if after_sig == before_sig:
                    return self._emit_validation_blocked(
                        session,
                        post_snap,
                        post_errors
                        or [
                            f"Screen {current_screen.screen_id!r} did not advance after clicking Next"
                        ],
                        safe_to_retry=True,
                    )
                continue

            # Single-screen pack with no WizardScreenSpec transition buttons.
            # With no commit control there is no evidence that this record
            # was saved (a request counted since the start may be unrelated
            # or may have failed), and reading back our own unsaved edits is
            # not evidence of a commit (absent-evidence-as-success): stop.
            sig = f"no_commit|{snapshot.screen_id or snapshot.url_origin_and_path}"
            halt_id = _deterministic_halt_id(
                session.run_id,
                FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                snapshot.screen_id,
                pack.pack_id,
                sig,
            )
            msg = (
                "No commit control on this screen, so there is no evidence the "
                "record was saved; the run will not treat its own edits as a commit"
            )
            halt = FormHaltItem(
                halt_id=halt_id,
                kind=FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                screen_id=snapshot.screen_id,
                message=msg,
                allowed_actions=(
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            )
            session._halt_metadata[halt_id] = {"field_signature": sig}
            return self._build_result(
                session,
                outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                # The page may have saved on its own; never resubmit blindly.
                safe_to_retry=False,
                halts=(halt,),
                what_happened=msg,
            )

        sig = f"transition_budget|{pack.pack_id}"
        halt_id = _deterministic_halt_id(
            session.run_id,
            FormHaltKind.MUTATION_BUDGET_EXCEEDED,
            None,
            pack.pack_id,
            sig,
        )
        msg = f"Exceeded max_screen_transitions={self.max_screen_transitions}"
        halt = FormHaltItem(
            halt_id=halt_id,
            kind=FormHaltKind.MUTATION_BUDGET_EXCEEDED,
            message=msg,
            allowed_actions=(
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            ),
        )
        session._halt_metadata[halt_id] = {"field_signature": sig}
        return self._build_result(
            session,
            outcome=FormTxOutcome.DELIVERY_UNCERTAIN,
            safe_to_retry=False,
            halts=(halt,),
            what_happened=msg,
        )

    def _check_live_resolution_context(
        self,
        session: ReconcilerSession,
        surface: FormSurfaceBackend,
        halt: FormHaltItem,
        metadata: Mapping[str, Any],
        action: HumanDecisionAction,
    ) -> None:
        """A decision authorizes the displayed target, never a replacement at its selector."""
        snapshot = surface.snapshot(
            entity_tables=session.pack.entity_tables,
            modal_rules=session.pack.modal_rules,
            screens=session.pack.screens,
        )
        if (
            halt.kind == FormHaltKind.AUTH_WALL_DETECTED
            and action == HumanDecisionAction.FIXED_ON_SCREEN
        ):
            return
        expected_context = session._halt_contexts.get(halt.halt_id)
        if expected_context is None or expected_context != (
            snapshot.url_origin_and_path,
            snapshot.screen_id,
            snapshot.context_id,
        ):
            raise ValueError("Human decision context changed; observe and review again")
        if snapshot.auth_wall_present or snapshot.inflight_mutations:
            raise ValueError("Human decision target is not settled and authenticated")

        previous_control: Optional[ObservedControl] = metadata.get("control")
        if previous_control is not None and not metadata.get("in_popup_subroutine"):
            live = [
                control
                for control in snapshot.controls
                if control.visible
                and control.dom_selector == previous_control.dom_selector
            ]
            if len(live) != 1 or compute_field_signature(
                live[0]
            ) != compute_field_signature(previous_control):
                raise ValueError(
                    "Human decision control changed; observe and review again"
                )
            control = live[0]
            if control.is_secret or control.disabled:
                raise ValueError(
                    "Human decision cannot actuate a secret or disabled control"
                )
            if action != HumanDecisionAction.FIXED_ON_SCREEN and (
                control.current_value != previous_control.current_value
                or control.checked != previous_control.checked
                or control.options != previous_control.options
            ):
                raise ValueError(
                    "Human decision field value changed; observe and review again"
                )

        previous_row: Optional[ObservedEntityRow] = metadata.get("entity_row")
        if previous_row is not None:
            live_rows = [
                row
                for row in snapshot.entity_rows
                if row.table_id == previous_row.table_id
                and row.row_selector == previous_row.row_selector
                and row.values == previous_row.values
            ]
            if len(live_rows) != 1:
                raise ValueError(
                    "Human decision entity row changed; observe and review again"
                )

        previous_modal: Optional[ObservedModal] = metadata.get("modal")
        if (
            previous_modal is not None
            and snapshot.active_modals.count(previous_modal) != 1
        ):
            raise ValueError("Human decision modal changed; observe and review again")

    def _resume_after_resolution(
        self, session: ReconcilerSession, surface: FormSurfaceBackend
    ) -> FormTxResult:
        session._resuming = True
        try:
            return self.run_until_halt_or_done(session, surface=surface)
        finally:
            session._resuming = False

    def apply_human_resolution(
        self,
        session: ReconcilerSession,
        resolution: HumanResolutionInput,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Apply a governed ``HumanResolutionInput`` to an active halt and resume if applicable.

        Surface errors while applying the decision become a stop, as in
        ``run_until_halt_or_done``.
        """
        active_surface = self._resolve_surface(surface, session)
        try:
            mutations_at_entry = active_surface.mutation_count
        except Exception:
            mutations_at_entry = 0
        try:
            return self._apply_human_resolution_unguarded(
                session, resolution, surface=surface
            )
        except Exception as exc:
            if not _is_surface_action_error(exc):
                raise
            return self._surface_error_result(
                session, active_surface, exc, mutations_at_entry
            )

    def _apply_human_resolution_unguarded(
        self,
        session: ReconcilerSession,
        resolution: HumanResolutionInput,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        decision_digest = canonical_sha256(resolution)
        if resolution.idempotency_key in session.seen_idempotency_keys:
            if (
                session._idempotent_decisions.get(resolution.idempotency_key)
                != decision_digest
            ):
                raise ValueError("Conflicting human decision reused an idempotency_key")
            cached = session._idempotent_results.get(resolution.idempotency_key)
            if cached is not None:
                return cached
            if session._last_result is not None:
                return session._last_result
            return self._build_result(
                session,
                outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                safe_to_retry=True,
                halts=session.active_halts,
                what_happened="Duplicate idempotency_key returned current session state",
            )

        active_halt = next(
            (h for h in session.active_halts if h.halt_id == resolution.halt_id),
            None,
        )
        if active_halt is None:
            raise ValueError(
                f"Resolution halt_id {resolution.halt_id!r} does not match any active halt"
            )
        if resolution.action not in active_halt.allowed_actions:
            raise ValueError(
                f"Action {resolution.action.value!r} is not allowed for halt "
                f"{active_halt.halt_id!r} ({active_halt.kind.value}); "
                f"allowed: {[a.value for a in active_halt.allowed_actions]}"
            )

        active_surface = self._resolve_surface(surface, session)
        meta = session._halt_metadata.get(active_halt.halt_id, {})
        field_sig: str = meta.get("field_signature") or (
            active_halt.dom_selector or active_halt.halt_id
        )
        effective_dossier_key = resolution.mapped_dossier_key or active_halt.dossier_key
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        action = resolution.action
        if action not in (
            HumanDecisionAction.ABORT_RUN,
            HumanDecisionAction.ESCALATE_TO_OWNER,
        ):
            self._check_live_resolution_context(
                session, active_surface, active_halt, meta, action
            )

        def _cache_and_return(res: FormTxResult) -> FormTxResult:
            session.seen_idempotency_keys.add(resolution.idempotency_key)
            session._idempotent_decisions[resolution.idempotency_key] = decision_digest
            session._idempotent_results[resolution.idempotency_key] = res
            return res

        def _append_record(resolved_val: Any) -> HumanResolutionRecord:
            rec = HumanResolutionRecord(
                halt_id=active_halt.halt_id,
                halt_kind=active_halt.kind,
                action=action,
                actor_id=resolution.actor_id,
                actor_role=resolution.actor_role,
                field_signature=field_sig,
                dossier_key=effective_dossier_key,
                resolved_value=resolved_val,
                remember_as_candidate_rule=resolution.remember_as_candidate_rule,
                timestamp_utc=now_utc,
                idempotency_key=resolution.idempotency_key,
            )
            session.human_resolutions.append(rec)
            return rec

        if action == HumanDecisionAction.ESCALATE_TO_OWNER:
            _append_record(None)
            res = self._build_result(
                session,
                outcome=FormTxOutcome.ESCALATED_TO_OWNER,
                safe_to_retry=not session._commit_in_progress,
                halts=(),
                what_happened=resolution.note
                or f"Escalated halt {active_halt.halt_id} to owner",
            )
            return _cache_and_return(res)

        if action == HumanDecisionAction.ABORT_RUN:
            _append_record(None)
            res = self._build_result(
                session,
                outcome=FormTxOutcome.ABORTED_BY_HUMAN,
                safe_to_retry=not session._commit_in_progress,
                halts=(),
                what_happened=resolution.note or f"Aborted by {resolution.actor_id}",
            )
            return _cache_and_return(res)

        if action == HumanDecisionAction.PROVIDE_VALUE:
            val = resolution.provided_value
            if val is None or (isinstance(val, str) and not val.strip()):
                raise ValueError("PROVIDE_VALUE requires a non-empty provided_value")

            if effective_dossier_key:
                session.effective_facts[effective_dossier_key] = DossierFact(
                    key=effective_dossier_key,
                    value=val,
                    provenance=ProvenanceKind.HUMAN_OPERATOR,
                    source_id=f"human:{resolution.actor_id}:{resolution.idempotency_key}",
                    confidence=1.0,
                )

            in_popup_sub: Optional[str] = meta.get("in_popup_subroutine")
            binding: Optional[FieldBindingSpec] = meta.get("binding")
            if in_popup_sub:
                if binding is None and active_halt.dom_selector:
                    session._popup_manual_fills[
                        (in_popup_sub, active_halt.dom_selector)
                    ] = val
            else:
                snap = active_surface.snapshot(
                    entity_tables=session.pack.entity_tables,
                    modal_rules=session.pack.modal_rules,
                    screens=session.pack.screens,
                )
                live_ctrl = next(
                    (
                        c
                        for c in snap.controls
                        if c.visible and c.dom_selector == active_halt.dom_selector
                    ),
                    None,
                )
                if live_ctrl is not None and (
                    binding is None
                    or active_halt.kind
                    in (
                        FormHaltKind.PREFILL_VALUE_CONFLICT,
                        FormHaltKind.PORTAL_VALIDATION_BLOCKED,
                        FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
                        FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                    )
                ):
                    ctrl_kind = (
                        binding.control_kind
                        if binding is not None
                        else self._infer_unmapped_control_kind(live_ctrl)
                    )
                    codec = binding.codec if binding is not None else FieldCodecSpec()
                    placeholders = (
                        binding.placeholder_values
                        if binding is not None
                        else _DEFAULT_PLACEHOLDERS
                    )
                    act_halt = self._actuate_control(
                        active_surface,
                        snap,
                        live_ctrl,
                        val,
                        control_kind=ctrl_kind,
                        codec=codec,
                        placeholder_values=placeholders,
                        dossier_key=effective_dossier_key,
                        run_id=session.run_id,
                        typeahead_query_chars=(
                            binding.typeahead_query_chars
                            if binding is not None
                            else None
                        ),
                    )
                    if act_halt is not None:
                        session._halt_metadata[act_halt.halt_id] = {
                            "field_signature": field_sig,
                            "control": live_ctrl,
                            "binding": binding,
                        }
                        res = self._build_result(
                            session,
                            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                            safe_to_retry=True,
                            halts=(act_halt,),
                            what_happened=act_halt.message,
                        )
                        return _cache_and_return(res)

                    session._filled_selectors.add(live_ctrl.dom_selector)
                    session._human_accepted_control_signatures.add(
                        compute_field_signature(live_ctrl)
                    )

            session._human_accepted_control_signatures.add(field_sig)
            session._human_accepted_values[field_sig] = val
            session.record_write(
                source="operator_value",
                dossier_key=effective_dossier_key,
                label=active_halt.field_label,
                selector=active_halt.dom_selector,
                in_popup_subroutine=in_popup_sub,
            )
            _append_record(val)
            res = self._resume_after_resolution(session, active_surface)
            return _cache_and_return(res)

        if action == HumanDecisionAction.ACCEPT_PORTAL_PREFILL:
            portal_val = active_halt.portal_value
            if active_halt.kind == FormHaltKind.PREFILL_ENTITY_CONFLICT:
                spec: Optional[EntityTableSpec] = meta.get("entity_table_spec")
                row: Optional[ObservedEntityRow] = meta.get("entity_row")
                if spec is None or row is None:
                    raise ValueError(
                        "Missing entity row metadata for ACCEPT_PORTAL_PREFILL"
                    )
                session._accepted_entity_rows.append((spec.table_id, dict(row.values)))
            else:
                if effective_dossier_key and portal_val is not None:
                    session.effective_facts[effective_dossier_key] = DossierFact(
                        key=effective_dossier_key,
                        value=portal_val,
                        provenance=ProvenanceKind.ACCEPTED_PORTAL_PREFILL,
                        source_id=f"human:{resolution.actor_id}:{resolution.idempotency_key}",
                        confidence=1.0,
                    )
                if active_halt.dom_selector:
                    session._filled_selectors.add(active_halt.dom_selector)
                session._human_accepted_control_signatures.add(field_sig)
                session._human_accepted_values[field_sig] = portal_val
                session.record_write(
                    source="operator_kept_prefill",
                    dossier_key=effective_dossier_key,
                    label=active_halt.field_label,
                    selector=active_halt.dom_selector,
                    in_popup_subroutine=meta.get("in_popup_subroutine"),
                )

            _append_record(portal_val)
            res = self._resume_after_resolution(session, active_surface)
            return _cache_and_return(res)

        if action == HumanDecisionAction.DISMISS_PORTAL_ENTITY:
            spec = meta.get("entity_table_spec")
            row = meta.get("entity_row")
            dismiss_sel: Optional[str] = meta.get("dismiss_selector")
            if spec is None or row is None or not dismiss_sel:
                raise ValueError(
                    f"Entity row for halt {active_halt.halt_id!r} has no dismiss selector"
                )

            pre_snap = active_surface.snapshot(
                entity_tables=session.pack.entity_tables,
                modal_rules=session.pack.modal_rules,
                screens=session.pack.screens,
            )
            before_matches = sum(
                1
                for r in pre_snap.entity_rows
                if r.table_id == spec.table_id
                and _entity_row_matches_values_map(r, row.values, spec)
            )
            active_surface.click(dismiss_sel)
            post_snap = active_surface.snapshot(
                entity_tables=session.pack.entity_tables,
                modal_rules=session.pack.modal_rules,
                screens=session.pack.screens,
            )
            after_matches = sum(
                1
                for r in post_snap.entity_rows
                if r.table_id == spec.table_id
                and _entity_row_matches_values_map(r, row.values, spec)
            )
            if before_matches != 1 or after_matches != 0:
                refusal_msg = (
                    f"Refused DISMISS_PORTAL_ENTITY: row in {spec.table_id!r} "
                    f"remained present after clicking {dismiss_sel!r}"
                )
                res = self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=True,
                    halts=list(session.active_halts),
                    what_happened=refusal_msg,
                )
                return _cache_and_return(res)

            entity_key = canonical_sha256(
                {"table_id": spec.table_id, "values": dict(row.values)}
            )
            session.dismissed_entities.add(entity_key)
            _append_record(dict(row.values))
            res = self._resume_after_resolution(session, active_surface)
            return _cache_and_return(res)

        if action == HumanDecisionAction.CONFIRM_MODAL_ACTION:
            obs_modal: Optional[ObservedModal] = meta.get("modal")
            confirm_sel = meta.get("confirm_selector") or active_halt.dom_selector
            if not confirm_sel:
                raise ValueError(
                    f"Modal halt {active_halt.halt_id!r} has no confirm_selector"
                )

            active_surface.click(confirm_sel)
            post_snap = active_surface.snapshot(
                entity_tables=session.pack.entity_tables,
                modal_rules=session.pack.modal_rules,
                screens=session.pack.screens,
            )
            modal_id = (
                obs_modal.modal_id if obs_modal is not None else active_halt.field_label
            )
            if any(m.modal_id == modal_id for m in post_snap.active_modals):
                refusal_msg = (
                    f"Refused CONFIRM_MODAL_ACTION: modal {modal_id!r} remained open"
                )
                res = self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=not session._commit_in_progress,
                    halts=list(session.active_halts),
                    what_happened=refusal_msg,
                )
                return _cache_and_return(res)

            session.confirmed_modals.add(modal_id)
            _append_record("confirmed")
            res = self._resume_after_resolution(session, active_surface)
            return _cache_and_return(res)

        if action == HumanDecisionAction.FIXED_ON_SCREEN:
            post_snap = active_surface.snapshot(
                entity_tables=session.pack.entity_tables,
                modal_rules=session.pack.modal_rules,
                screens=session.pack.screens,
            )
            if active_halt.kind == FormHaltKind.AUTH_WALL_DETECTED:
                still_blocked = (
                    _auth_wall_reason(post_snap, active_surface.ui_profile) is not None
                )
                if still_blocked:
                    res = self._build_result(
                        session,
                        outcome=FormTxOutcome.AUTH_REQUIRED,
                        safe_to_retry=True,
                        halts=list(session.active_halts),
                        what_happened=(
                            "Refused FIXED_ON_SCREEN: authentication wall is still active"
                        ),
                    )
                    return _cache_and_return(res)
                _append_record("authenticated")
                res = self._resume_after_resolution(session, active_surface)
                return _cache_and_return(res)

            binding = meta.get("binding")
            live_ctrl = None
            if active_halt.dom_selector:
                live_ctrl = next(
                    (
                        c
                        for c in post_snap.controls
                        if c.visible and c.dom_selector == active_halt.dom_selector
                    ),
                    None,
                )
            if live_ctrl is None and active_halt.control_id:
                live_ctrl = next(
                    (
                        c
                        for c in post_snap.controls
                        if c.visible and c.control_id == active_halt.control_id
                    ),
                    None,
                )
            if live_ctrl is None and binding is not None:
                try:
                    live_ctrl = match_anchor_in_snapshot(
                        binding.anchor, post_snap, include_readonly=True
                    )
                except AmbiguousAnchorError:
                    live_ctrl = None

            if (
                active_halt.kind == FormHaltKind.PORTAL_VALIDATION_BLOCKED
                and active_halt.dom_selector is None
            ):
                remaining_errors = _collect_surface_errors(post_snap)
                if remaining_errors:
                    res = self._build_result(
                        session,
                        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                        safe_to_retry=True,
                        halts=list(session.active_halts),
                        what_happened=(
                            f"Refused FIXED_ON_SCREEN: validation errors remain: "
                            f"{remaining_errors}"
                        ),
                    )
                    return _cache_and_return(res)
                _append_record("validation_cleared")
                res = self._resume_after_resolution(session, active_surface)
                return _cache_and_return(res)

            placeholders = (
                binding.placeholder_values
                if binding is not None
                else _DEFAULT_PLACEHOLDERS
            )
            if live_ctrl is None or _is_control_empty(live_ctrl, placeholders):
                refusal_msg = (
                    f"Refused FIXED_ON_SCREEN: control "
                    f"{active_halt.field_label or active_halt.dom_selector!r} "
                    f"is still empty on screen"
                )
                res = self._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=True,
                    halts=list(session.active_halts),
                    what_happened=refusal_msg,
                )
                return _cache_and_return(res)

            live_val: Any
            if (
                live_ctrl.input_type.casefold() == "checkbox"
                and not live_ctrl.current_value.strip()
                and live_ctrl.checked is not None
            ):
                live_val = live_ctrl.checked
            else:
                live_val = live_ctrl.current_value

            if effective_dossier_key:
                session.effective_facts[effective_dossier_key] = DossierFact(
                    key=effective_dossier_key,
                    value=live_val,
                    provenance=ProvenanceKind.HUMAN_OPERATOR,
                    source_id=f"human:{resolution.actor_id}:{resolution.idempotency_key}",
                    confidence=1.0,
                )
            session._filled_selectors.add(live_ctrl.dom_selector)
            session._human_accepted_control_signatures.add(
                compute_field_signature(live_ctrl)
            )
            session._human_accepted_control_signatures.add(field_sig)
            session._human_accepted_values[field_sig] = live_val
            session.record_write(
                source="operator_fixed_on_screen",
                dossier_key=effective_dossier_key,
                label=active_halt.field_label or live_ctrl.label_text,
                selector=live_ctrl.dom_selector,
                in_popup_subroutine=meta.get("in_popup_subroutine"),
            )
            _append_record(live_val)
            res = self._resume_after_resolution(session, active_surface)
            return _cache_and_return(res)

        raise ValueError(f"Unsupported human resolution action: {action!r}")
