"""Inverted anti-rubber-stamping PREPARE queue, async clearinghouse state, and 4-mutant pack promotion.

Enforces four runtime invariants:
  1. Anti-Rubber-Stamping: PrepareCard rejects approve_all=True whenever unresolved_slots
     is non-empty. Every unresolved slot requires an explicit binding to a declared $var
     or an allowed enum option.
  2. Post-Human Verification: after an operator submits PrepareResolution, the kernel
     still runs verify_semantic_slot_bindings before commit and canonical_equal PutGet
     read-back after commit, catching operator wrong-slot picks and typos.
  3. Async Clearinghouse State (SUBMITTED_PENDING_RECONCILIATION): when a portal returns
     a receipt/tracking reference immediately while table indexing lags, the kernel records
     a pending reconciliation obligation and blocks duplicate re-submission.
  4. Cold-to-Warm Compilation (qualify_and_promote_pack): promotes a verified resolution
     and read-back lens into a zero-PII PortalPack only after passing 4 counterfactual
     mutants (dropped_write, mutated_amount_or_date, swapped_same_type_var, and
     format_variation_acceptance).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from openadapt_kernel.canonical import (
    DossierVariableSchema,
    FieldType,
    SemanticSlotVerificationReport,
    _coerce_schemas,
    _extract_control_labels,
    canonical_equal,
    canonical_var_name,
    extract_code_prefix_token,
    normalize_casefold_ws,
    parse_canonical_currency_cents,
    parse_canonical_date,
    parse_canonical_phone,
    verify_semantic_slot_bindings,
)
from openadapt_kernel.copilot import AutonomyStage, PortalPack
from openadapt_kernel.ledger import ObligationLedger, ReadBackProof
from openadapt_kernel.macro import ControlSchema
from openadapt_kernel.registry import scan_pack_for_pii

PrepareOutcome = Literal[
    "VERIFIED",
    "SUBMITTED_PENDING_RECONCILIATION",
    "REFUSED_PRE_COMMIT",
    "ESCALATED",
]
PrepareResumeStatus = Literal["RESUME", "HALT"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_readback_key(key: str) -> str:
    return normalize_casefold_ws(key).rstrip(":").strip()


class RubberStampViolationError(ValueError):
    """Raised when a PrepareResolution attempts approve_all=True or omits forced slot bindings."""


@dataclass(frozen=True)
class UnresolvedSlot:
    control_idx: int
    control_label: str
    reason: str
    candidate_vars: Tuple[str, ...] = ()
    allowed_enum_options: Tuple[str, ...] = ()
    expected_field_type: FieldType = "casefold_ws"


@dataclass(frozen=True)
class PrepareResolution:
    card_id: str
    operator_id: str
    slot_bindings: Dict[int, str] = field(default_factory=dict)
    approve_all: bool = False
    resolved_at_utc: str = field(default_factory=_utc_now)


@dataclass
class PrepareCard:
    card_id: str
    portal_origin: str
    staged_bindings: Dict[int, str]
    unresolved_slots: List[UnresolvedSlot] = field(default_factory=list)
    control_labels: Dict[int, str] = field(default_factory=dict)
    control_selectors: Dict[int, str] = field(default_factory=dict)
    created_at_utc: str = field(default_factory=_utc_now)

    def validate_resolution(
        self,
        resolution: PrepareResolution,
        variable_schemas: Union[
            Sequence[DossierVariableSchema],
            Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
        ],
    ) -> Dict[int, str]:
        """Validate forced-choice operator resolution and reject rubber-stamp overrides."""
        if resolution.card_id != self.card_id:
            raise RubberStampViolationError(
                f"Resolution card_id {resolution.card_id!r} does not match {self.card_id!r}"
            )
        if self.unresolved_slots and resolution.approve_all:
            raise RubberStampViolationError(
                "approve_all=True is forbidden when unresolved_slots is non-empty; "
                "supply explicit slot_bindings for every unresolved slot."
            )

        schemas = _coerce_schemas(variable_schemas)
        unresolved_by_idx = {slot.control_idx: slot for slot in self.unresolved_slots}
        merged: Dict[int, str] = dict(self.staged_bindings)

        for cidx, slot in unresolved_by_idx.items():
            if cidx not in resolution.slot_bindings:
                raise RubberStampViolationError(
                    f"Missing required forced-choice binding for unresolved slot {cidx} "
                    f"({slot.control_label!r})"
                )
            chosen = str(resolution.slot_bindings[cidx]).strip()
            if not chosen:
                raise RubberStampViolationError(
                    f"Empty binding supplied for unresolved slot {cidx} ({slot.control_label!r})"
                )

            allowed_vars = {
                canonical_var_name(v)
                for v in (slot.candidate_vars if slot.candidate_vars else tuple(schemas.keys()))
            }
            allowed_enums = set(slot.allowed_enum_options)
            cvar = canonical_var_name(chosen)

            if chosen in allowed_enums:
                merged[cidx] = chosen
            elif (chosen.startswith("$") or cvar in schemas) and cvar in allowed_vars:
                merged[cidx] = cvar
            else:
                raise RubberStampViolationError(
                    f"Binding {chosen!r} for slot {cidx} is not in allowed candidate_vars "
                    f"{sorted(allowed_vars)} or allowed_enum_options {sorted(allowed_enums)}"
                )

        # Allow explicit re-binding of existing controls only when valid in schemas or staged
        for cidx, raw_val in resolution.slot_bindings.items():
            if cidx in unresolved_by_idx:
                continue
            if cidx not in self.control_labels and cidx not in self.staged_bindings:
                raise RubberStampViolationError(f"Unknown slot index in resolution: {cidx}")
            chosen = str(raw_val).strip()
            cvar = canonical_var_name(chosen)
            if chosen.startswith("$") or cvar in schemas:
                if cvar not in schemas:
                    raise RubberStampViolationError(f"Unknown variable {chosen!r} for slot {cidx}")
                merged[cidx] = cvar
            else:
                merged[cidx] = chosen

        return merged


@dataclass(frozen=True)
class PrepareResumeResponse:
    card_id: str
    operator_id: str
    parameter_values: Dict[str, str] = field(default_factory=dict)
    confirmed_fields: Dict[str, str] = field(default_factory=dict)
    slot_bindings: Dict[int, str] = field(default_factory=dict)
    approve_all: bool = False
    rejected: bool = False
    timed_out: bool = False
    rejection_reason: str = ""


@dataclass
class PrepareResumeResult:
    status: PrepareResumeStatus
    reason: str
    updated_dossier: Dict[str, str] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)
    missing_params: List[str] = field(default_factory=list)


def resume_from_prepare_card(
    card: PrepareCard,
    response: PrepareResumeResponse,
    dossier: Mapping[str, str],
    *,
    required_params: Sequence[str] = (),
) -> PrepareResumeResult:
    """Validate an interactive PrepareCard response and bind user inputs into a resumed dossier."""
    if response.card_id != card.card_id:
        raise RubberStampViolationError(f"Response card_id {response.card_id!r} does not match {card.card_id!r}")
    if response.timed_out:
        return PrepareResumeResult("HALT", "prepare_card_timed_out", updated_dossier=dict(dossier))
    if response.rejected:
        return PrepareResumeResult("HALT", response.rejection_reason or "prepare_card_rejected", updated_dossier=dict(dossier))
    if response.approve_all:
        raise RubberStampViolationError("approve_all=True is forbidden; supply explicit parameter_values or confirmed_fields.")
    if not response.operator_id.strip():
        raise RubberStampViolationError("operator_id is required for PrepareResumeResponse")
    for slot in card.unresolved_slots:
        if slot.control_idx not in response.slot_bindings or not str(response.slot_bindings[slot.control_idx]).strip():
            raise RubberStampViolationError(f"Missing explicit binding for unresolved slot {slot.control_idx}")
    if not response.parameter_values and not response.confirmed_fields and not response.slot_bindings:
        raise RubberStampViolationError("Blank PrepareResumeResponse is forbidden")

    updated: Dict[str, str] = dict(dossier)
    provenance: Dict[str, str] = {}
    for src in (response.parameter_values, response.confirmed_fields):
        for k, v in src.items():
            sval = str(v).strip()
            if not sval:
                raise RubberStampViolationError(f"Blank value supplied for {k!r}")
            updated[k] = sval
            provenance[k] = "user_prepare_card"

    still_missing = [
        p for p in required_params
        if not str(updated.get(p) or updated.get(p.lstrip("$")) or updated.get(f"${p.lstrip('$')}") or "").strip()
    ]
    if still_missing:
        return PrepareResumeResult("HALT", "missing_required_params_after_prepare", updated, provenance, still_missing)
    return PrepareResumeResult("RESUME", "prepare_resumed_with_user_bindings", updated, provenance)


@dataclass(frozen=True)
class ReadBackFieldSpec:
    slot_key: str
    readback_key: str
    field_type: FieldType
    min_mask_suffix: int = 4


@dataclass(frozen=True)
class ReadBackLens:
    readback_url_pattern: str
    fields: Tuple[ReadBackFieldSpec, ...]
    receipt_ref_pattern: Optional[str] = None


@dataclass
class PendingReconciliationRecord:
    obligation_id: str
    card_id: str
    portal_origin: str
    receipt_ref: str
    expected_by_readback_key: Dict[str, Tuple[str, FieldType, int]]
    recorded_at_utc: str = field(default_factory=_utc_now)


@dataclass
class PrepareExecutionResult:
    outcome: PrepareOutcome
    resolved_bindings: Dict[int, str] = field(default_factory=dict)
    concrete_values: Dict[int, str] = field(default_factory=dict)
    matched_readback_keys: List[str] = field(default_factory=list)
    mismatched_readback_keys: Dict[str, str] = field(default_factory=dict)
    missing_readback_keys: List[str] = field(default_factory=list)
    slot_report: Optional[SemanticSlotVerificationReport] = None
    pending_reconciliation: Optional[PendingReconciliationRecord] = None
    receipt_ref: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class PackMutantReport:
    qualified: bool
    dropped_write_refuted: bool
    mutated_amount_or_date_refuted: bool
    swapped_same_type_var_refuted: bool
    format_variation_accepted: bool
    reasons: Tuple[str, ...] = ()
    pack: Optional[PortalPack] = None


def build_prepare_card(
    *,
    card_id: str,
    portal_origin: str,
    proposed_bindings: Mapping[int, str],
    variable_schemas: Union[
        Sequence[DossierVariableSchema],
        Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
    ],
    controls: Union[
        Sequence[ControlSchema],
        Mapping[int, Union[str, ControlSchema]],
    ],
    unbound_enum_slots: Sequence[UnresolvedSlot] = (),
) -> PrepareCard:
    """Partition proposed bindings into verified staged slots vs unresolved PREPARE slots."""
    schemas = _coerce_schemas(variable_schemas)
    labels_by_idx = _extract_control_labels(controls)
    selectors_by_idx: Dict[int, str] = {}
    if isinstance(controls, Sequence):
        for pos, ctrl in enumerate(controls):
            cidx = ctrl.control_index if ctrl.control_index >= 0 else pos
            if ctrl.selector:
                selectors_by_idx[cidx] = ctrl.selector
    elif isinstance(controls, Mapping):
        for idx, item in controls.items():
            if isinstance(item, ControlSchema) and item.selector:
                selectors_by_idx[int(idx)] = item.selector

    var_bindings: Dict[int, str] = {}
    static_bindings: Dict[int, str] = {}
    for idx, val in proposed_bindings.items():
        cidx = int(idx)
        sval = str(val).strip()
        if sval.startswith("$") or canonical_var_name(sval) in schemas:
            var_bindings[cidx] = canonical_var_name(sval)
        else:
            static_bindings[cidx] = sval

    report = verify_semantic_slot_bindings(var_bindings, schemas, controls)
    staged: Dict[int, str] = dict(static_bindings)
    unresolved: List[UnresolvedSlot] = []

    for verdict in report.verdicts:
        if verdict.status == "OK":
            staged[verdict.control_idx] = verdict.bound_var
        else:
            sch = schemas.get(verdict.bound_var)
            unresolved.append(
                UnresolvedSlot(
                    control_idx=verdict.control_idx,
                    control_label=verdict.control_label,
                    reason=verdict.reason,
                    candidate_vars=verdict.candidate_vars,
                    expected_field_type=sch.field_type if sch else "exact",
                )
            )

    existing_unresolved = {u.control_idx for u in unresolved}
    for extra in unbound_enum_slots:
        if extra.control_idx not in existing_unresolved:
            staged.pop(extra.control_idx, None)
            unresolved.append(extra)

    return PrepareCard(
        card_id=card_id,
        portal_origin=portal_origin,
        staged_bindings=staged,
        unresolved_slots=unresolved,
        control_labels=labels_by_idx,
        control_selectors=selectors_by_idx,
    )


def _build_default_lens(
    card: PrepareCard,
    merged_bindings: Mapping[int, str],
    schemas: Mapping[str, DossierVariableSchema],
    readback_url_pattern: str = "",
) -> ReadBackLens:
    unresolved_map = {s.control_idx: s for s in card.unresolved_slots}
    specs: List[ReadBackFieldSpec] = []
    for cidx in sorted(merged_bindings.keys()):
        bound = merged_bindings[cidx]
        slot_key = card.control_selectors.get(cidx) or f"slot_{cidx}"
        rb_key = card.control_labels.get(cidx) or slot_key
        cvar = canonical_var_name(bound)
        if cvar in schemas:
            ftype = schemas[cvar].field_type
        elif cidx in unresolved_map:
            ftype = unresolved_map[cidx].expected_field_type
        else:
            ftype = "casefold_ws"
        specs.append(ReadBackFieldSpec(slot_key=slot_key, readback_key=rb_key, field_type=ftype))
    return ReadBackLens(
        readback_url_pattern=readback_url_pattern or card.portal_origin,
        fields=tuple(specs),
    )


def _resolve_slot_index_for_spec(
    spec: ReadBackFieldSpec,
    card: PrepareCard,
    merged_bindings: Mapping[int, str],
) -> Optional[int]:
    for cidx in merged_bindings:
        sel = card.control_selectors.get(cidx) or f"slot_{cidx}"
        lbl = card.control_labels.get(cidx) or ""
        if spec.slot_key in (sel, str(cidx), f"slot_{cidx}") or (
            lbl and _norm_readback_key(spec.readback_key) == _norm_readback_key(lbl)
        ):
            return cidx
    return None


def execute_prepare_resolution(
    *,
    card: PrepareCard,
    resolution: PrepareResolution,
    variable_schemas: Union[
        Sequence[DossierVariableSchema],
        Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
    ],
    dossier_values: Mapping[str, str],
    observed_readback: Mapping[str, str],
    lens: Optional[ReadBackLens] = None,
    receipt_ref: Optional[str] = None,
    receipt_text: str = "",
    ledger: Optional[ObligationLedger] = None,
    submitted_registry: Optional[Dict[str, str]] = None,
) -> PrepareExecutionResult:
    """Validate PrepareResolution, re-verify slots, and run typed canonical PutGet read-back."""
    if submitted_registry is not None and card.card_id in submitted_registry:
        prior = submitted_registry[card.card_id]
        return PrepareExecutionResult(
            outcome="REFUSED_PRE_COMMIT",
            reason=f"duplicate_submission_blocked:{prior}",
        )

    schemas = _coerce_schemas(variable_schemas)
    merged_bindings = card.validate_resolution(resolution, schemas)

    # Post-Human Verification Part 1: re-verify semantic slot compatibility even after operator input
    var_only_bindings = {
        cidx: val
        for cidx, val in merged_bindings.items()
        if val.startswith("$") or canonical_var_name(val) in schemas
    }
    disambiguated_idxs = frozenset(s.control_idx for s in card.unresolved_slots)
    slot_report = verify_semantic_slot_bindings(
        var_only_bindings,
        schemas,
        card.control_labels,
        explicitly_disambiguated_slots=disambiguated_idxs,
    )
    if not slot_report.ok:
        reasons = ";".join(
            f"slot_{v.control_idx}:{v.reason}"
            for v in slot_report.verdicts
            if v.status == "AMBIGUOUS_SLOT"
        )
        return PrepareExecutionResult(
            outcome="REFUSED_PRE_COMMIT",
            resolved_bindings=merged_bindings,
            slot_report=slot_report,
            reason=f"post_human_slot_verification_failed:{reasons}",
        )

    norm_dossier = {canonical_var_name(k): str(v) for k, v in dossier_values.items()}
    concrete_values: Dict[int, str] = {}
    for cidx, bound in merged_bindings.items():
        if cidx in var_only_bindings:
            cvar = canonical_var_name(bound)
            if cvar not in norm_dossier or not norm_dossier[cvar].strip():
                return PrepareExecutionResult(
                    outcome="REFUSED_PRE_COMMIT",
                    resolved_bindings=merged_bindings,
                    slot_report=slot_report,
                    reason=f"missing_dossier_value:{cvar}",
                )
            concrete_values[cidx] = norm_dossier[cvar]
        else:
            concrete_values[cidx] = bound

    active_lens = lens or _build_default_lens(card, merged_bindings, schemas)
    norm_observed = {_norm_readback_key(k): str(v) for k, v in observed_readback.items()}

    extracted_ref = (receipt_ref or "").strip() or None
    if not extracted_ref and active_lens.receipt_ref_pattern and receipt_text:
        m = re.search(active_lens.receipt_ref_pattern, receipt_text)
        if m:
            extracted_ref = (m.group(1) if m.groups() else m.group(0)).strip() or None

    matched_keys: List[str] = []
    mismatched_keys: Dict[str, str] = {}
    missing_keys: List[str] = []
    expected_by_rb: Dict[str, Tuple[str, FieldType, int]] = {}

    for spec in active_lens.fields:
        spec_cidx = _resolve_slot_index_for_spec(spec, card, merged_bindings)
        if spec_cidx is None or spec_cidx not in concrete_values:
            missing_keys.append(spec.readback_key)
            continue
        written_val = concrete_values[spec_cidx]
        expected_by_rb[spec.readback_key] = (written_val, spec.field_type, spec.min_mask_suffix)

        obs_val = norm_observed.get(_norm_readback_key(spec.readback_key))
        if obs_val is None or not obs_val.strip():
            missing_keys.append(spec.readback_key)
            continue

        if canonical_equal(
            written_val, obs_val, spec.field_type, min_mask_suffix=spec.min_mask_suffix
        ):
            matched_keys.append(spec.readback_key)
        else:
            mismatched_keys[spec.readback_key] = (
                f"written={written_val!r}, observed={obs_val!r}, field_type={spec.field_type!r}"
            )

    if mismatched_keys:
        if ledger is not None:
            ledger.finalize("error", error_detail="putget_readback_mismatch")
        return PrepareExecutionResult(
            outcome="ESCALATED",
            resolved_bindings=merged_bindings,
            concrete_values=concrete_values,
            matched_readback_keys=matched_keys,
            mismatched_readback_keys=mismatched_keys,
            missing_readback_keys=missing_keys,
            slot_report=slot_report,
            receipt_ref=extracted_ref,
            reason="putget_readback_mismatch",
        )

    if not missing_keys and matched_keys:
        if submitted_registry is not None:
            submitted_registry[card.card_id] = extracted_ref or "VERIFIED"
        if ledger is not None:
            ob_id = f"commit:{card.card_id}"
            if ob_id not in ledger.obligations:
                ledger.add_obligation(
                    ob_id, f"Commit {card.card_id}", target_system=card.portal_origin
                )
            cited = [
                norm_observed[_norm_readback_key(k)]
                for k in matched_keys
                if _norm_readback_key(k) in norm_observed
            ]
            ledger.mark_committed(
                ob_id,
                ReadBackProof(
                    channel="fresh_reload",
                    url_or_locator=active_lens.readback_url_pattern or card.portal_origin,
                    cited_substrings=cited,
                    external_ref=extracted_ref,
                ),
            )
            ledger.finalize("done")
        return PrepareExecutionResult(
            outcome="VERIFIED",
            resolved_bindings=merged_bindings,
            concrete_values=concrete_values,
            matched_readback_keys=matched_keys,
            slot_report=slot_report,
            receipt_ref=extracted_ref,
            reason="putget_verified",
        )

    if missing_keys and extracted_ref:
        if submitted_registry is not None:
            submitted_registry[card.card_id] = extracted_ref
        ob_id = f"reconcile:{card.card_id}"
        if ledger is not None and ob_id not in ledger.obligations:
            ledger.add_obligation(
                ob_id,
                f"Reconcile indexed read-back for receipt {extracted_ref}",
                target_system=card.portal_origin,
            )
        pending_rec = PendingReconciliationRecord(
            obligation_id=ob_id,
            card_id=card.card_id,
            portal_origin=card.portal_origin,
            receipt_ref=extracted_ref,
            expected_by_readback_key=expected_by_rb,
        )
        return PrepareExecutionResult(
            outcome="SUBMITTED_PENDING_RECONCILIATION",
            resolved_bindings=merged_bindings,
            concrete_values=concrete_values,
            matched_readback_keys=matched_keys,
            missing_readback_keys=missing_keys,
            slot_report=slot_report,
            pending_reconciliation=pending_rec,
            receipt_ref=extracted_ref,
            reason=f"awaiting_async_table_reconciliation:{extracted_ref}",
        )

    if ledger is not None:
        ledger.finalize("error", error_detail="missing_readback_fields")
    return PrepareExecutionResult(
        outcome="ESCALATED",
        resolved_bindings=merged_bindings,
        concrete_values=concrete_values,
        matched_readback_keys=matched_keys,
        missing_readback_keys=missing_keys,
        slot_report=slot_report,
        reason="missing_readback_fields",
    )


def reconcile_pending_submission(
    pending: PendingReconciliationRecord,
    indexed_readback: Mapping[str, str],
    *,
    ledger: Optional[ObligationLedger] = None,
    locator: Optional[str] = None,
) -> PrepareExecutionResult:
    """Reconcile an async clearinghouse submission once table indexing completes."""
    norm_obs = {_norm_readback_key(k): str(v) for k, v in indexed_readback.items()}
    matched: List[str] = []
    mismatched: Dict[str, str] = {}
    missing: List[str] = []

    for rb_key, (written_val, ftype, min_suffix) in pending.expected_by_readback_key.items():
        obs_val = norm_obs.get(_norm_readback_key(rb_key))
        if obs_val is None or not obs_val.strip():
            missing.append(rb_key)
            continue
        if canonical_equal(written_val, obs_val, ftype, min_mask_suffix=min_suffix):
            matched.append(rb_key)
        else:
            mismatched[rb_key] = (
                f"written={written_val!r}, observed={obs_val!r}, field_type={ftype!r}"
            )

    if mismatched:
        if ledger is not None:
            ledger.finalize("error", error_detail="async_reconciliation_mismatch")
        return PrepareExecutionResult(
            outcome="ESCALATED",
            matched_readback_keys=matched,
            mismatched_readback_keys=mismatched,
            missing_readback_keys=missing,
            receipt_ref=pending.receipt_ref,
            reason="async_reconciliation_mismatch",
        )

    if missing:
        return PrepareExecutionResult(
            outcome="SUBMITTED_PENDING_RECONCILIATION",
            matched_readback_keys=matched,
            missing_readback_keys=missing,
            pending_reconciliation=pending,
            receipt_ref=pending.receipt_ref,
            reason=f"awaiting_async_table_reconciliation:{pending.receipt_ref}",
        )

    if ledger is not None:
        if pending.obligation_id not in ledger.obligations:
            ledger.add_obligation(
                pending.obligation_id,
                f"Reconcile {pending.receipt_ref}",
                target_system=pending.portal_origin,
            )
        cited = [norm_obs[_norm_readback_key(k)] for k in matched]
        ledger.mark_committed(
            pending.obligation_id,
            ReadBackProof(
                channel="fresh_reload",
                url_or_locator=locator or pending.portal_origin,
                cited_substrings=cited,
                external_ref=pending.receipt_ref,
            ),
        )
        ledger.finalize("done")

    return PrepareExecutionResult(
        outcome="VERIFIED",
        matched_readback_keys=matched,
        receipt_ref=pending.receipt_ref,
        reason="reconciled_verified",
    )


def _format_variation_for_field(written: str, field_type: FieldType, min_mask_suffix: int = 4) -> str:
    if field_type == "currency":
        cents = parse_canonical_currency_cents(written)
        if cents is not None:
            abs_c = abs(cents)
            body = f"${abs_c // 100:,}.{abs_c % 100:02d}"
            return f"({body})" if cents < 0 else body
    elif field_type == "date":
        dt = parse_canonical_date(written)
        if dt is not None:
            return f"{dt[1]:02d}/{dt[2]:02d}/{dt[0]:04d} (45y)"
    elif field_type == "phone":
        ph = parse_canonical_phone(written)
        if ph is not None and ph.startswith("+1") and len(ph) == 12:
            d10 = ph[2:]
            return f"({d10[:3]}) {d10[3:6]}-{d10[6:]}"
    elif field_type == "masked_id":
        clean = re.sub(r"[\s\-]", "", written).upper()
        if len(clean) > min_mask_suffix:
            return f"XXX-XX-{clean[-min_mask_suffix:]}"
    elif field_type == "code_prefix":
        tok = extract_code_prefix_token(written)
        if tok is not None:
            return f"{tok} - Standard description"
    elif field_type == "casefold_ws":
        return f"  {written.strip().lower()}  "
    return written


def _corrupt_value_for_field(written: str, field_type: FieldType, min_mask_suffix: int = 4) -> str:
    if field_type == "currency":
        cents = parse_canonical_currency_cents(written)
        if cents is not None:
            corrupted_cents = cents // 10 if (cents != 0 and cents // 10 != cents) else cents + 1000
            abs_c = abs(corrupted_cents)
            body = f"${abs_c // 100:,}.{abs_c % 100:02d}"
            return f"({body})" if corrupted_cents < 0 else body
    elif field_type == "date":
        dt = parse_canonical_date(written)
        if dt is not None:
            y, m, d = dt
            rev_d = (d % 10) * 10 + (d // 10)
            bad_d = rev_d if (1 <= rev_d <= 28 and rev_d != d) else ((d % 28) + 1)
            return f"{y:04d}-{m:02d}-{bad_d:02d}"
    elif field_type == "phone":
        ph = parse_canonical_phone(written)
        if ph is not None:
            return ph[2:] + "9" if ph.startswith("+1") else ph + "9"
    elif field_type == "masked_id":
        clean = re.sub(r"[\s\-]", "", written).upper()
        last = "0" if not clean.endswith("0") else "1"
        return f"XXX-XX-{clean[-min_mask_suffix:-1]}{last}"
    elif field_type == "code_prefix":
        tok = extract_code_prefix_token(written) or written
        return tok[:-1] if len(tok) > 2 else f"{tok}9"
    return f"{written}_CORRUPT"


def _distinct_pair_for_type(field_type: FieldType) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    if field_type == "date":
        return (("1955-08-19", "08/19/1955"), ("1981-04-12", "04/12/1981"))
    if field_type == "currency":
        return (("2450", "$2,450.00"), ("150", "$150.00"))
    if field_type == "phone":
        return (("4155550199", "(415) 555-0199"), ("2125550144", "(212) 555-0144"))
    if field_type == "masked_id":
        return (("123-45-6789", "XXX-XX-6789"), ("987-65-4321", "XXX-XX-4321"))
    if field_type == "code_prefix":
        return (("M54.50", "M54.50 - Low back pain"), ("J01.90", "J01.90 - Acute sinusitis"))
    return (("Primary", "Primary"), ("Secondary", "Secondary"))


def qualify_and_promote_pack(
    *,
    pack_id: str,
    card: PrepareCard,
    resolution: PrepareResolution,
    variable_schemas: Union[
        Sequence[DossierVariableSchema],
        Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
    ],
    dossier_values: Mapping[str, str],
    observed_readback: Mapping[str, str],
    lens: Optional[ReadBackLens] = None,
    putget_verifier: Optional[Callable[[Mapping[str, str], Mapping[str, str]], bool]] = None,
    target_stage: AutonomyStage = "VERIFIED_AUTOPILOT",
) -> PackMutantReport:
    """Compile a verified PrepareResolution into a warm PortalPack after 4-mutant qualification."""
    schemas = _coerce_schemas(variable_schemas)
    exec_res = execute_prepare_resolution(
        card=card,
        resolution=resolution,
        variable_schemas=schemas,
        dossier_values=dossier_values,
        observed_readback=observed_readback,
        lens=lens,
    )
    if exec_res.outcome != "VERIFIED":
        return PackMutantReport(
            qualified=False,
            dropped_write_refuted=False,
            mutated_amount_or_date_refuted=False,
            swapped_same_type_var_refuted=False,
            format_variation_accepted=False,
            reasons=(f"live_episode_not_verified:{exec_res.outcome}:{exec_res.reason}",),
            pack=None,
        )

    merged = exec_res.resolved_bindings
    active_lens = lens or _build_default_lens(card, merged, schemas)
    spec_by_norm_rb = {_norm_readback_key(s.readback_key): s for s in active_lens.fields}

    expected_map: Dict[str, str] = {}
    base_obs_map: Dict[str, str] = {}
    norm_live_obs = {_norm_readback_key(k): str(v) for k, v in observed_readback.items()}

    for spec in active_lens.fields:
        cidx = _resolve_slot_index_for_spec(spec, card, merged)
        if cidx is not None and cidx in exec_res.concrete_values:
            expected_map[spec.readback_key] = exec_res.concrete_values[cidx]
            base_obs_map[spec.readback_key] = norm_live_obs[_norm_readback_key(spec.readback_key)]

    def _default_verifier(exp: Mapping[str, str], obs: Mapping[str, str]) -> bool:
        norm_o = {_norm_readback_key(k): str(v) for k, v in obs.items()}
        if not exp:
            return False
        for rb_k, written_v in exp.items():
            nk = _norm_readback_key(rb_k)
            sp = spec_by_norm_rb.get(nk)
            ftype: FieldType = sp.field_type if sp else "exact"
            min_sfx = sp.min_mask_suffix if sp else 4
            got_v = norm_o.get(nk, "")
            if not canonical_equal(written_v, got_v, ftype, min_mask_suffix=min_sfx):
                return False
        return True

    verifier = putget_verifier or _default_verifier
    reasons: List[str] = []

    if not expected_map or not verifier(expected_map, base_obs_map):
        return PackMutantReport(
            qualified=False,
            dropped_write_refuted=False,
            mutated_amount_or_date_refuted=False,
            swapped_same_type_var_refuted=False,
            format_variation_accepted=False,
            reasons=("baseline_verifier_rejected_verified_episode",),
            pack=None,
        )

    keys = list(expected_map.keys())

    # Mutant 1: dropped_write (blank value and omitted key must both fail)
    drop_key = keys[0]
    obs_blank = {**base_obs_map, drop_key: ""}
    obs_omitted = {k: v for k, v in base_obs_map.items() if k != drop_key}
    dropped_write_refuted = (not verifier(expected_map, obs_blank)) and (
        not verifier(expected_map, obs_omitted)
    )
    if not dropped_write_refuted:
        reasons.append("dropped_write_falsely_accepted")

    # Mutant 2: mutated_amount_or_date
    target_mut_specs = [
        s for s in active_lens.fields if s.field_type in ("currency", "date") and s.readback_key in expected_map
    ]
    if not target_mut_specs:
        target_mut_specs = [s for s in active_lens.fields if s.readback_key in expected_map][:1]

    mut_checks: List[bool] = []
    for sp in target_mut_specs:
        bad_val = _corrupt_value_for_field(
            expected_map[sp.readback_key], sp.field_type, sp.min_mask_suffix
        )
        obs_mut = {**base_obs_map, sp.readback_key: bad_val}
        mut_checks.append(not verifier(expected_map, obs_mut))
    mutated_amount_or_date_refuted = bool(mut_checks) and all(mut_checks)
    if not mutated_amount_or_date_refuted:
        reasons.append("mutated_amount_or_date_falsely_accepted")

    # Mutant 3: swapped_same_type_var
    by_ftype: Dict[FieldType, List[ReadBackFieldSpec]] = {}
    for sp in active_lens.fields:
        if sp.readback_key in expected_map:
            by_ftype.setdefault(sp.field_type, []).append(sp)

    swap_checks: List[bool] = []
    for ftype, group in by_ftype.items():
        if len(group) >= 2:
            s1, s2 = group[0], group[1]
            k1, k2 = s1.readback_key, s2.readback_key
            exp_swap = dict(expected_map)
            obs_swap = dict(base_obs_map)
            if canonical_equal(exp_swap[k1], exp_swap[k2], ftype, min_mask_suffix=s1.min_mask_suffix):
                (w1, o1), (w2, o2) = _distinct_pair_for_type(ftype)
                exp_swap[k1], exp_swap[k2] = w1, w2
                obs_swap[k1], obs_swap[k2] = o2, o1
            else:
                obs_swap[k1], obs_swap[k2] = base_obs_map[k2], base_obs_map[k1]
            swap_checks.append(not verifier(exp_swap, obs_swap))

    if not swap_checks and len(keys) >= 2:
        k1, k2 = keys[0], keys[1]
        obs_swap = {**base_obs_map, k1: base_obs_map[k2], k2: base_obs_map[k1]}
        swap_checks.append(not verifier(expected_map, obs_swap))

    swapped_same_type_var_refuted = bool(swap_checks) and all(swap_checks)
    if not swapped_same_type_var_refuted:
        reasons.append("swapped_same_type_var_falsely_accepted")

    # Mutant 4: format_variation_acceptance ($2,450.00 MUST pass while $245.00 MUST fail)
    reformatted_obs = {
        sp.readback_key: _format_variation_for_field(
            expected_map[sp.readback_key], sp.field_type, sp.min_mask_suffix
        )
        for sp in active_lens.fields
        if sp.readback_key in expected_map
    }
    valid_fmt_ok = bool(verifier(expected_map, reformatted_obs))

    currency_specs = [
        sp for sp in active_lens.fields if sp.field_type == "currency" and sp.readback_key in expected_map
    ]
    if currency_specs:
        ckey = currency_specs[0].readback_key
        exp_2450 = {**expected_map, ckey: "2450"}
        obs_good_2450 = {**reformatted_obs, ckey: "$2,450.00"}
        obs_bad_245 = {**reformatted_obs, ckey: "$245.00"}
        valid_fmt_ok = valid_fmt_ok and bool(verifier(exp_2450, obs_good_2450))
        corrupt_fmt_rejected = not verifier(exp_2450, obs_bad_245)
    else:
        sp0 = active_lens.fields[0]
        obs_bad_fmt = {
            **reformatted_obs,
            sp0.readback_key: _corrupt_value_for_field(
                expected_map[sp0.readback_key], sp0.field_type, sp0.min_mask_suffix
            ),
        }
        corrupt_fmt_rejected = not verifier(expected_map, obs_bad_fmt)

    format_variation_accepted = valid_fmt_ok and corrupt_fmt_rejected
    if not valid_fmt_ok:
        reasons.append("valid_portal_format_variation_falsely_rejected")
    if not corrupt_fmt_rejected:
        reasons.append("corrupt_formatted_value_falsely_accepted")

    qualified = (
        dropped_write_refuted
        and mutated_amount_or_date_refuted
        and swapped_same_type_var_refuted
        and format_variation_accepted
    )
    if not qualified:
        return PackMutantReport(
            qualified=False,
            dropped_write_refuted=dropped_write_refuted,
            mutated_amount_or_date_refuted=mutated_amount_or_date_refuted,
            swapped_same_type_var_refuted=swapped_same_type_var_refuted,
            format_variation_accepted=format_variation_accepted,
            reasons=tuple(reasons),
            pack=None,
        )

    variable_fields: List[str] = []
    static_defaults: Dict[str, str] = {}
    provenance_map: Dict[str, str] = {}
    field_types_meta: Dict[str, str] = {}
    readback_keys_meta: Dict[str, str] = {}

    for spec in active_lens.fields:
        cidx = _resolve_slot_index_for_spec(spec, card, merged)
        if cidx is None or cidx not in merged:
            continue
        bound = merged[cidx]
        cvar = canonical_var_name(bound)
        field_types_meta[spec.slot_key] = spec.field_type
        readback_keys_meta[spec.slot_key] = spec.readback_key
        if bound.startswith("$") or cvar in schemas:
            variable_fields.append(spec.slot_key)
            provenance_map[spec.slot_key] = cvar
        else:
            static_defaults[spec.slot_key] = bound

    pack = PortalPack(
        pack_id=pack_id,
        portal_origin=card.portal_origin,
        variable_fields=variable_fields,
        static_defaults=static_defaults,
        provenance_map=provenance_map,
        readback_url_pattern=active_lens.readback_url_pattern,
        stage=target_stage,
        shadow_passes=1,
        edge_branches={
            "__field_types__": field_types_meta,
            "__readback_keys__": readback_keys_meta,
        },
    )

    pii_hits = scan_pack_for_pii(pack)
    if pii_hits:
        return PackMutantReport(
            qualified=False,
            dropped_write_refuted=dropped_write_refuted,
            mutated_amount_or_date_refuted=mutated_amount_or_date_refuted,
            swapped_same_type_var_refuted=swapped_same_type_var_refuted,
            format_variation_accepted=format_variation_accepted,
            reasons=(f"pii_detected_in_compiled_pack:{','.join(pii_hits)}",),
            pack=None,
        )

    return PackMutantReport(
        qualified=True,
        dropped_write_refuted=True,
        mutated_amount_or_date_refuted=True,
        swapped_same_type_var_refuted=True,
        format_variation_accepted=True,
        reasons=(),
        pack=pack,
    )


def verify_warm_pack_putget(
    pack: PortalPack,
    dossier_values: Mapping[str, str],
    observed_readback: Mapping[str, str],
    *,
    min_mask_suffix: int = 4,
) -> Tuple[bool, Dict[str, str]]:
    """Verify post-commit read-back for a promoted warm PortalPack using its compiled typed lens."""
    norm_dossier = {canonical_var_name(k): str(v) for k, v in dossier_values.items()}
    norm_obs = {_norm_readback_key(k): str(v) for k, v in observed_readback.items()}
    ftypes = pack.edge_branches.get("__field_types__", {})
    rb_keys = pack.edge_branches.get("__readback_keys__", {})
    mismatches: Dict[str, str] = {}

    for slot_key, source_var in pack.provenance_map.items():
        cvar = canonical_var_name(source_var)
        if cvar not in norm_dossier:
            mismatches[slot_key] = f"missing_dossier_var:{cvar}"
            continue
        written = norm_dossier[cvar]
        rb_key = rb_keys.get(slot_key, slot_key)
        ftype = ftypes.get(slot_key, "exact")
        observed = norm_obs.get(_norm_readback_key(rb_key), "")
        if not canonical_equal(written, observed, ftype, min_mask_suffix=min_mask_suffix):  # type: ignore[arg-type]
            mismatches[slot_key] = f"written={written!r}, observed={observed!r}, type={ftype!r}"

    for slot_key, static_val in pack.static_defaults.items():
        rb_key = rb_keys.get(slot_key, slot_key)
        ftype = ftypes.get(slot_key, "casefold_ws")
        observed = norm_obs.get(_norm_readback_key(rb_key), "")
        if not canonical_equal(static_val, observed, ftype, min_mask_suffix=min_mask_suffix):  # type: ignore[arg-type]
            mismatches[slot_key] = f"written={static_val!r}, observed={observed!r}, type={ftype!r}"

    return (len(mismatches) == 0), mismatches
