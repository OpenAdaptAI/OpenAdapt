"""Stateful, resumable governed form and wizard reconciliation engine (``form_tx``).

Drives any ``FormSurfaceBackend`` against a declarative ``FormPack`` and
provenance-carrying ``InputDossier`` with zero model calls. Fails closed on
auth walls, dynamic modals, prefilled entity/value conflicts, ambiguous
options/anchors, popup contract failures, portal validation errors, and
ungrounded required fields (including newly materialized conditional fields).
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, cast

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
from openadapt_flow.form_tx.receipt import issue_form_tx_receipt
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    FormSurfaceBackend,
    compute_field_signature,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierEntity,
    DossierFact,
    EntityTableSpec,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
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
    PopupSubroutineSpec,
    PrefillPolicy,
    ProvenanceKind,
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
# Retryability describes effects, not whether the same run may advance again.
# A pending post-commit modal can be resumable while safe_to_retry is False;
# an explicit abort can be terminal while no write was dispatched.
_RESUMABLE_OUTCOMES = frozenset(
    {
        FormTxOutcome.HALTED_NEEDS_HUMAN,
        FormTxOutcome.AUTH_REQUIRED,
        FormTxOutcome.VALIDATION_BLOCKED,
    }
)
_DEFAULT_PLACEHOLDERS: tuple[str, ...] = (
    "",
    "-- Select --",
    "Select...",
    "Choose...",
)


class _EffectState(Enum):
    NO_DISPATCH = "no_dispatch"
    MAY_HAVE_DISPATCHED = "may_have_dispatched"
    DELIVERY_UNCERTAIN = "delivery_uncertain"


class _SurfaceOperationFailure(Exception):
    """Value-free backend failure; the original exception remains its cause."""

    def __init__(self, operation: str, error_kind: str) -> None:
        super().__init__(f"Surface {operation} failed ({error_kind})")


class _EffectTrackedSurface:
    """Track dispatch before entering a backend, including popup children.

    A backend actuator may send a write and then fail while settling. Neither
    an exception nor a zero mutation counter proves that no write was sent.
    Unknown callable methods are conservatively actuators; only the protocol's
    explicit observer/local-counter methods are exempt.
    """

    _READ_ONLY_METHODS = frozenset(
        {"snapshot", "ping_keepalive", "reset_mutation_counter"}
    )

    def __init__(self, surface: FormSurfaceBackend, session: ReconcilerSession):
        self._surface = surface
        self._session = session

    def __getattr__(self, name: str) -> Any:
        try:
            value = getattr(self._surface, name)
        except Exception as exc:
            raise _SurfaceOperationFailure(name, type(exc).__name__) from exc
        if not callable(value):
            return value
        if name == "open_popup_surface":
            return self._open_popup_surface

        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._invoke(name, value, *args, **kwargs)

        return invoke

    def _invoke(
        self, name: str, method: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if name not in self._READ_ONLY_METHODS:
            if self._session._effect_state is _EffectState.NO_DISPATCH:
                self._session._effect_state = _EffectState.MAY_HAVE_DISPATCHED
            self._session._dispatch_attempts += 1
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            raise _SurfaceOperationFailure(name, type(exc).__name__) from exc

    @contextmanager
    def _open_popup_surface(
        self, *args: Any, **kwargs: Any
    ) -> Iterator[FormSurfaceBackend]:
        try:
            manager = self._invoke(
                "open_popup_surface", self._surface.open_popup_surface, *args, **kwargs
            )
            pending: Optional[BaseException] = None
            with manager as child:
                try:
                    yield cast(
                        FormSurfaceBackend, _EffectTrackedSurface(child, self._session)
                    )
                except BaseException as exc:
                    pending = exc
                    raise
            # A backend context manager cannot suppress a child delivery error
            # or cancellation and let the reconciler proceed to a receipt.
            if pending is not None:
                raise pending
        except _SurfaceOperationFailure:
            raise
        except Exception as exc:
            raise _SurfaceOperationFailure(
                "open_popup_surface", type(exc).__name__
            ) from exc


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
        self._pre_commit_mutations: int = 0
        self._last_result: Optional[FormTxResult] = None
        self._bound_surface: Optional[FormSurfaceBackend] = None
        self._effect_state = _EffectState.NO_DISPATCH
        self._dispatch_attempts = 0
        self._terminal_result: Optional[FormTxResult] = None


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


def _is_control_empty(
    ctrl: ObservedControl,
    placeholders: Sequence[str] = _DEFAULT_PLACEHOLDERS,
) -> bool:
    """Return True if ``ctrl`` has no entered/selected value on screen."""
    itype = ctrl.input_type.casefold()
    role = ctrl.role.casefold()
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
        if session is not None:
            if isinstance(active, _EffectTrackedSurface):
                if active._session is session:
                    return cast(FormSurfaceBackend, active)
                # An injected judge may retain a supplied surface. Reusing it
                # for another session must not charge dispatch to the old one.
                active = active._surface
            return cast(FormSurfaceBackend, _EffectTrackedSurface(active, session))
        return active

    def _effect_failure(
        self, session: ReconcilerSession, failure: Exception
    ) -> FormTxResult:
        uncertain = session._effect_state is not _EffectState.NO_DISPATCH
        if uncertain:
            session._effect_state = _EffectState.DELIVERY_UNCERTAIN
        result = self._build_result(
            session,
            outcome=(
                FormTxOutcome.DELIVERY_UNCERTAIN
                if uncertain
                else FormTxOutcome.HALTED_NEEDS_HUMAN
            ),
            safe_to_retry=not uncertain,
            receipt=None,
            what_happened=(
                "Delivery may have occurred; reconcile the system of record before another attempt."
                if uncertain
                else f"{failure}; stopped before any actuator was invoked."
            ),
        )
        return result

    def _guard_effects(
        self,
        session: ReconcilerSession,
        advance: Callable[[], FormTxResult],
        *,
        preserve_decision_refusals: bool = False,
    ) -> FormTxResult:
        if session._terminal_result is not None:
            return session._terminal_result
        attempts_before = session._dispatch_attempts
        try:
            return advance()
        except _SurfaceOperationFailure as exc:
            return self._effect_failure(session, exc)
        except Exception as exc:
            # Preserve explicit invalid/stale human-decision refusals when this
            # invocation did not attempt another actuator. Backend ValueErrors
            # are wrapped above, so they cannot take this path.
            if (
                preserve_decision_refusals
                and isinstance(exc, ValueError)
                and session._dispatch_attempts == attempts_before
            ):
                raise
            if session._effect_state is _EffectState.NO_DISPATCH:
                raise
            return self._effect_failure(session, exc)
        except BaseException:
            # Cancellation and process-control exceptions keep their normal
            # propagation semantics, while a later resume still cannot resend.
            if session._effect_state is not _EffectState.NO_DISPATCH:
                self._effect_failure(session, RuntimeError("execution interrupted"))
            raise

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
        if outcome is FormTxOutcome.DELIVERY_UNCERTAIN:
            session._effect_state = _EffectState.DELIVERY_UNCERTAIN
        resumable = outcome in _RESUMABLE_OUTCOMES and (
            safe_to_retry or session._commit_in_progress
        )
        if not resumable:
            # A terminal/no-retry result ends this run. A paused unsafe result
            # may continue only through an already pending commit (e.g. its
            # modal), never by revisiting the form and dispatching another save.
            # New outcome kinds fail closed unless explicitly resumable above.
            session._terminal_result = result
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
    ) -> Optional[FormHaltItem]:
        """Actuate ``ctrl`` on ``surface``, returning a ``FormHaltItem`` on ambiguity/codec failure."""
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
                    radio_peers = [
                        c
                        for c in snapshot.controls
                        if c.visible
                        and c.input_type.casefold() == "radio"
                        and c.name_attr
                        and c.name_attr == ctrl.name_attr
                        and c.current_value == opt.value
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
                available_options=ctrl.options,
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
                    edited_any = True
                    if binding.triggers_re_render:
                        return True, True, None

        return edited_any, False, None

    def _collect_ungrounded_required_halts(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        bindings: Sequence[FieldBindingSpec],
        *,
        in_popup_subroutine: Optional[str] = None,
    ) -> list[FormHaltItem]:
        matched_by_selector: dict[str, FieldBindingSpec] = {}
        for candidate_binding in bindings:
            try:
                matched = match_anchor_in_snapshot(
                    candidate_binding.anchor, snapshot, include_readonly=False
                )
            except AmbiguousAnchorError:
                continue
            if matched is not None:
                matched_by_selector[matched.dom_selector] = candidate_binding

        halts: list[FormHaltItem] = []
        for ctrl in snapshot.controls:
            if not ctrl.visible or ctrl.disabled or ctrl.readonly:
                continue
            if _is_action_button(ctrl):
                continue

            binding = matched_by_selector.get(ctrl.dom_selector)
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

    def _run_popup_subroutine(
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

        with parent_surface.open_popup_surface(
            trigger_ctrl.dom_selector,
            url_pattern=subroutine.popup_url_pattern,
            title_pattern=subroutine.popup_title_pattern,
        ) as popup_surface:
            popup_snap = popup_surface.snapshot()

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
                    popup_snap = popup_surface.snapshot()

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
                popup_snap = popup_surface.snapshot()

            popup_ungrounded = self._collect_ungrounded_required_halts(
                session,
                popup_snap,
                subroutine.bindings,
                in_popup_subroutine=subroutine.subroutine_id,
            )
            if popup_ungrounded:
                return popup_ungrounded

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
                sig = f"popup_commit|{subroutine.subroutine_id}"
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
                    message=commit_err,
                    allowed_actions=(
                        HumanDecisionAction.FIXED_ON_SCREEN,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                )
                session._halt_metadata[halt_id] = {"field_signature": sig}
                return [halt]

            popup_surface.click(popup_commit.dom_selector)

        # Verify parent return anchor after popup closes.
        after_parent_snap = parent_surface.snapshot(
            entity_tables=session.pack.entity_tables,
            modal_rules=session.pack.modal_rules,
            screens=session.pack.screens,
        )
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

        session.subroutines_completed.append(subroutine.subroutine_id)
        session._filled_selectors.add(return_after.dom_selector)
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

    def _emit_validation_blocked(
        self,
        session: ReconcilerSession,
        snapshot: FormSnapshot,
        errors: Sequence[str],
        *,
        safe_to_retry: bool,
    ) -> FormTxResult:
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
        delta_mutations = max(0, surface.mutation_count - pre_commit_mutations)
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
        )
        extracted_outputs = dict(verdict.extracted_outputs)
        if not verdict.passed or verdict.missing_required_outputs:
            reasons = list(verdict.failed_keys) + [
                f"missing_output:{k}" for k in verdict.missing_required_outputs
            ]
            return self._build_result(
                session,
                outcome=FormTxOutcome.READBACK_REFUTED,
                safe_to_retry=False,
                outputs=extracted_outputs,
                summary_verdict=verdict,
                receipt=None,
                what_happened=f"Independent read-back did not verify commit: {reasons}",
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
        )
        return self._build_result(
            session,
            outcome=FormTxOutcome.VERIFIED_DONE,
            safe_to_retry=False,
            outputs=extracted_outputs,
            summary_verdict=verdict,
            receipt=receipt,
            what_happened="Committed and verified via independent read-back",
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

    def run_until_halt_or_done(
        self,
        session: ReconcilerSession,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Advance safely; an uncertain delivery freezes this session against resend."""
        return self._guard_effects(
            session, lambda: self._run_until_halt_or_done(session, surface=surface)
        )

    def _run_until_halt_or_done(
        self,
        session: ReconcilerSession,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Advance ``session`` deterministically until verified completion or a governed halt."""
        active_surface = self._resolve_surface(surface, session)
        pack = session.pack
        session.active_halts.clear()

        for _ in range(self.max_screen_transitions):
            snapshot = active_surface.snapshot(
                entity_tables=pack.entity_tables,
                modal_rules=pack.modal_rules,
                screens=pack.screens,
            )

            session._last_observation = snapshot

            # 2. Auth Wall Check
            if (
                snapshot.auth_wall_present
                or any(
                    control.visible and control.is_secret and not control.readonly
                    for control in snapshot.controls
                )
                or (
                    active_surface.ui_profile.authenticated_landmark_selectors
                    and not snapshot.authenticated_landmark_present
                )
            ):
                sig = f"auth_wall|{snapshot.url_origin_and_path}"
                halt_id = _deterministic_halt_id(
                    session.run_id,
                    FormHaltKind.AUTH_WALL_DETECTED,
                    snapshot.screen_id,
                    snapshot.url_origin_and_path,
                    sig,
                )
                halt = FormHaltItem(
                    halt_id=halt_id,
                    kind=FormHaltKind.AUTH_WALL_DETECTED,
                    screen_id=snapshot.screen_id,
                    message=(
                        f"Authentication wall or missing session landmark at "
                        f"{snapshot.url_origin_and_path!r}"
                    ),
                    allowed_actions=(
                        HumanDecisionAction.FIXED_ON_SCREEN,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                )
                session._halt_metadata[halt_id] = {"field_signature": sig}
                return self._build_result(
                    session,
                    outcome=FormTxOutcome.AUTH_REQUIRED,
                    safe_to_retry=True,
                    halts=(halt,),
                    what_happened=halt.message,
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

                    sub_halts = self._run_popup_subroutine(
                        active_surface, session, snapshot, sub
                    )
                    if sub_halts:
                        return self._build_result(
                            session,
                            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                            safe_to_retry=True,
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
                session._commit_in_progress = True
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

            # Single-screen pack with no WizardScreenSpec transition buttons
            return self._finalize_commit(
                active_surface, session, snapshot, active_surface.mutation_count
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

    def apply_human_resolution(
        self,
        session: ReconcilerSession,
        resolution: HumanResolutionInput,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Apply a live human decision through the same dispatch safety boundary."""
        # A terminal cached result must not hide a conflicting reuse of an
        # already accepted decision key. This check performs no surface call.
        self._validate_decision_key(session, resolution)
        return self._guard_effects(
            session,
            lambda: self._apply_human_resolution(session, resolution, surface=surface),
            preserve_decision_refusals=True,
        )

    def _validate_decision_key(
        self, session: ReconcilerSession, resolution: HumanResolutionInput
    ) -> str:
        decision_digest = canonical_sha256(resolution)
        if (
            resolution.idempotency_key in session.seen_idempotency_keys
            and session._idempotent_decisions.get(resolution.idempotency_key)
            != decision_digest
        ):
            raise ValueError("Conflicting human decision reused an idempotency_key")
        return decision_digest

    def _apply_human_resolution(
        self,
        session: ReconcilerSession,
        resolution: HumanResolutionInput,
        *,
        surface: Optional[FormSurfaceBackend] = None,
    ) -> FormTxResult:
        """Apply a governed ``HumanResolutionInput`` to an active halt and resume if applicable."""
        decision_digest = self._validate_decision_key(session, resolution)
        if resolution.idempotency_key in session.seen_idempotency_keys:
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
            _append_record(val)
            res = self.run_until_halt_or_done(session, surface=active_surface)
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

            _append_record(portal_val)
            res = self.run_until_halt_or_done(session, surface=active_surface)
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
            res = self.run_until_halt_or_done(session, surface=active_surface)
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
            res = self.run_until_halt_or_done(session, surface=active_surface)
            return _cache_and_return(res)

        if action == HumanDecisionAction.FIXED_ON_SCREEN:
            post_snap = active_surface.snapshot(
                entity_tables=session.pack.entity_tables,
                modal_rules=session.pack.modal_rules,
                screens=session.pack.screens,
            )
            if active_halt.kind == FormHaltKind.AUTH_WALL_DETECTED:
                still_blocked = post_snap.auth_wall_present or (
                    active_surface.ui_profile.authenticated_landmark_selectors
                    and not post_snap.authenticated_landmark_present
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
                res = self.run_until_halt_or_done(session, surface=active_surface)
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
                res = self.run_until_halt_or_done(session, surface=active_surface)
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
            _append_record(live_val)
            res = self.run_until_halt_or_done(session, surface=active_surface)
            return _cache_and_return(res)

        raise ValueError(f"Unsupported human resolution action: {action!r}")
