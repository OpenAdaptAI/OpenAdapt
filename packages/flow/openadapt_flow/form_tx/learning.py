"""Governed rule accumulation and pack patch admission for ``form_tx``.

Induces ``PackPatchCandidate`` proposals strictly from repeated, contradiction-free
human co-pilot resolutions across distinct runs (fail-closed on any disagreement),
and gates pack updates through explicit immutable admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormHaltKind,
    FormPack,
    HumanDecisionAction,
    HumanResolutionRecord,
    PackPatchCandidate,
    PrefillPolicy,
    canonical_sha256,
)

__all__ = [
    "RuleAccumulator",
    "admit_candidate_into_pack",
]

ProposedKind = Literal["field_binding", "prefill_policy", "default_escalation"]


@dataclass(frozen=True)
class _ResolutionDescriptor:
    proposed_kind: ProposedKind
    proposed_dossier_key: Optional[str]
    proposed_action: HumanDecisionAction
    unmapped_value_digest: Optional[str] = None


def _is_eligible_record(rec: HumanResolutionRecord) -> bool:
    if rec.action == HumanDecisionAction.ABORT_RUN:
        return False
    if rec.remember_as_candidate_rule:
        return True
    return rec.action in (
        HumanDecisionAction.ESCALATE_TO_OWNER,
        HumanDecisionAction.DISMISS_PORTAL_ENTITY,
    )


def _describe_resolution(rec: HumanResolutionRecord) -> _ResolutionDescriptor:
    if rec.action == HumanDecisionAction.ESCALATE_TO_OWNER:
        return _ResolutionDescriptor(
            proposed_kind="default_escalation",
            proposed_dossier_key=rec.dossier_key,
            proposed_action=HumanDecisionAction.ESCALATE_TO_OWNER,
        )
    if rec.action == HumanDecisionAction.DISMISS_PORTAL_ENTITY:
        return _ResolutionDescriptor(
            proposed_kind="prefill_policy",
            proposed_dossier_key=rec.dossier_key,
            proposed_action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
        )
    if rec.action == HumanDecisionAction.ACCEPT_PORTAL_PREFILL:
        return _ResolutionDescriptor(
            proposed_kind="prefill_policy",
            proposed_dossier_key=rec.dossier_key,
            proposed_action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
        )
    if rec.halt_kind == FormHaltKind.PREFILL_VALUE_CONFLICT and rec.action in (
        HumanDecisionAction.PROVIDE_VALUE,
        HumanDecisionAction.FIXED_ON_SCREEN,
    ):
        val_digest = (
            canonical_sha256(rec.resolved_value) if rec.dossier_key is None else None
        )
        return _ResolutionDescriptor(
            proposed_kind="prefill_policy",
            proposed_dossier_key=rec.dossier_key,
            proposed_action=HumanDecisionAction.PROVIDE_VALUE,
            unmapped_value_digest=val_digest,
        )
    val_digest = (
        canonical_sha256(rec.resolved_value) if rec.dossier_key is None else None
    )
    return _ResolutionDescriptor(
        proposed_kind="field_binding",
        proposed_dossier_key=rec.dossier_key,
        proposed_action=HumanDecisionAction.PROVIDE_VALUE,
        unmapped_value_digest=val_digest,
    )


def _infer_control_kind_from_signature(field_signature: str) -> ControlKind:
    parts = field_signature.split("|")
    kind_token = parts[2].strip().casefold() if len(parts) >= 3 else ""
    if kind_token == "select":
        return ControlKind.SELECT
    if kind_token == "combobox":
        return ControlKind.PORTAL_COMBOBOX
    if kind_token == "checkbox":
        return ControlKind.CHECKBOX
    if kind_token in ("radio", "radiogroup"):
        return ControlKind.RADIO_GROUP
    if kind_token == "date":
        return ControlKind.DATE
    return ControlKind.TEXT


def admit_candidate_into_pack(
    pack: FormPack,
    candidate: PackPatchCandidate,
    anchor: FieldAnchorSpec,
    codec: FieldCodecSpec = FieldCodecSpec(),
    *,
    control_kind: Optional[ControlKind] = None,
    prefill_policy: Optional[PrefillPolicy] = None,
    triggers_re_render: bool = False,
    required_in_pack: bool = False,
) -> FormPack:
    """Produce a new immutable ``FormPack`` containing the admitted ``candidate``."""
    if candidate.pack_id != pack.pack_id:
        raise ValueError(
            f"Candidate pack_id {candidate.pack_id!r} does not match pack {pack.pack_id!r}"
        )
    if candidate.supporting_run_count < 2:
        raise ValueError(
            "Cannot admit a PackPatchCandidate with fewer than 2 supporting runs"
        )

    resolved_kind = (
        control_kind
        if control_kind is not None
        else _infer_control_kind_from_signature(candidate.field_signature)
    )

    if candidate.proposed_kind == "field_binding":
        if not candidate.proposed_dossier_key:
            raise ValueError(
                "field_binding PackPatchCandidate requires a non-empty proposed_dossier_key"
            )
        resolved_prefill = (
            prefill_policy
            if prefill_policy is not None
            else PrefillPolicy.HALT_ON_CONFLICT
        )
        new_binding = FieldBindingSpec(
            binding_id=f"admitted_{candidate.proposed_dossier_key}_{candidate.candidate_id[-8:]}",
            dossier_key=candidate.proposed_dossier_key,
            anchor=anchor,
            control_kind=resolved_kind,
            codec=codec,
            prefill_policy=resolved_prefill,
            triggers_re_render=triggers_re_render,
            required_in_pack=required_in_pack,
        )
        return pack.model_copy(update={"bindings": (*pack.bindings, new_binding)})

    if candidate.proposed_kind == "prefill_policy":
        if not candidate.proposed_dossier_key:
            raise ValueError(
                "prefill_policy admission into bindings requires proposed_dossier_key"
            )
        resolved_prefill = (
            prefill_policy
            if prefill_policy is not None
            else (
                PrefillPolicy.OVERWRITE_WITH_DOSSIER
                if candidate.proposed_action == HumanDecisionAction.PROVIDE_VALUE
                else PrefillPolicy.KEEP_IF_MATCHES_ELSE_HALT
            )
        )
        updated_bindings: list[FieldBindingSpec] = []
        replaced = False
        for existing in pack.bindings:
            if existing.dossier_key == candidate.proposed_dossier_key:
                updated_bindings.append(
                    existing.model_copy(update={"prefill_policy": resolved_prefill})
                )
                replaced = True
            else:
                updated_bindings.append(existing)
        if not replaced:
            updated_bindings.append(
                FieldBindingSpec(
                    binding_id=f"admitted_{candidate.proposed_dossier_key}_{candidate.candidate_id[-8:]}",
                    dossier_key=candidate.proposed_dossier_key,
                    anchor=anchor,
                    control_kind=resolved_kind,
                    codec=codec,
                    prefill_policy=resolved_prefill,
                    triggers_re_render=triggers_re_render,
                    required_in_pack=required_in_pack,
                )
            )
        return pack.model_copy(update={"bindings": tuple(updated_bindings)})

    raise ValueError(
        f"Candidate kind {candidate.proposed_kind!r} cannot be admitted as a field binding"
    )


class RuleAccumulator:
    """Accumulates human resolutions across runs and induces consensus patch candidates."""

    def __init__(self) -> None:
        # pack_id -> field_signature -> list of per-run _ResolutionDescriptor
        self._observations: dict[str, dict[str, list[_ResolutionDescriptor]]] = {}
        # pack_id -> set of field_signatures that had intra-run or inter-run contradiction
        self._contradicted: dict[str, set[str]] = {}
        # pack_id -> seen idempotency_keys to prevent duplicate replay of the same run
        self._seen_idempotency_keys: dict[str, set[str]] = {}
        # pack_id -> candidate_id -> admitted PackPatchCandidate
        self._admitted: dict[str, dict[str, PackPatchCandidate]] = {}

    def record_run_resolutions(
        self,
        pack_id: str,
        records: Sequence[HumanResolutionRecord],
    ) -> None:
        """Record eligible human resolution records from one completed/halted run."""
        if not pack_id:
            raise ValueError("pack_id must be non-empty")

        seen_keys = self._seen_idempotency_keys.setdefault(pack_id, set())
        pack_obs = self._observations.setdefault(pack_id, {})
        pack_contradicted = self._contradicted.setdefault(pack_id, set())

        per_run_by_sig: dict[str, set[_ResolutionDescriptor]] = {}
        for rec in records:
            if not _is_eligible_record(rec):
                continue
            if rec.idempotency_key in seen_keys:
                continue
            seen_keys.add(rec.idempotency_key)
            sig = rec.field_signature.strip()
            if not sig:
                continue
            desc = _describe_resolution(rec)
            per_run_by_sig.setdefault(sig, set()).add(desc)

        for sig, descriptors in per_run_by_sig.items():
            if len(descriptors) > 1:
                pack_contradicted.add(sig)
                continue
            (only_desc,) = tuple(descriptors)
            history = pack_obs.setdefault(sig, [])
            if any(prev != only_desc for prev in history):
                pack_contradicted.add(sig)
            history.append(only_desc)

    def induce_candidates(
        self,
        pack_id: str,
        min_support: int = 2,
    ) -> tuple[PackPatchCandidate, ...]:
        """Return un-admitted ``PackPatchCandidate`` items with ``>= min_support`` and zero contradictions."""
        if min_support < 2:
            raise ValueError("min_support must be at least 2")

        pack_obs = self._observations.get(pack_id, {})
        pack_contradicted = self._contradicted.get(pack_id, set())
        admitted_map = self._admitted.get(pack_id, {})

        candidates: list[PackPatchCandidate] = []
        for sig in sorted(pack_obs):
            if sig in pack_contradicted:
                continue
            history = pack_obs[sig]
            if len(history) < min_support:
                continue
            unique = set(history)
            if len(unique) != 1:
                continue
            (consensus,) = tuple(unique)
            if (
                consensus.proposed_kind == "field_binding"
                and not consensus.proposed_dossier_key
            ):
                # Cannot synthesize a field binding without a target dossier_key.
                continue

            cand_digest = canonical_sha256(
                {
                    "pack_id": pack_id,
                    "field_signature": sig,
                    "proposed_kind": consensus.proposed_kind,
                    "proposed_dossier_key": consensus.proposed_dossier_key,
                    "proposed_action": (
                        consensus.proposed_action.value
                        if consensus.proposed_action is not None
                        else None
                    ),
                }
            )
            cand_id = f"cand_{cand_digest[:16]}"
            if cand_id in admitted_map:
                continue

            candidates.append(
                PackPatchCandidate(
                    candidate_id=cand_id,
                    pack_id=pack_id,
                    field_signature=sig,
                    proposed_kind=consensus.proposed_kind,
                    proposed_dossier_key=consensus.proposed_dossier_key,
                    proposed_action=consensus.proposed_action,
                    supporting_run_count=len(history),
                    admitted=False,
                )
            )

        return tuple(candidates)

    def admit_candidate_into_pack(
        self,
        pack: FormPack,
        candidate: PackPatchCandidate,
        anchor: FieldAnchorSpec,
        codec: FieldCodecSpec = FieldCodecSpec(),
        *,
        control_kind: Optional[ControlKind] = None,
        prefill_policy: Optional[PrefillPolicy] = None,
        triggers_re_render: bool = False,
        required_in_pack: bool = False,
    ) -> FormPack:
        """Gate and admit ``candidate`` into a new immutable ``FormPack``."""
        updated_pack = admit_candidate_into_pack(
            pack=pack,
            candidate=candidate,
            anchor=anchor,
            codec=codec,
            control_kind=control_kind,
            prefill_policy=prefill_policy,
            triggers_re_render=triggers_re_render,
            required_in_pack=required_in_pack,
        )
        admitted_cand = candidate.model_copy(update={"admitted": True})
        self._admitted.setdefault(pack.pack_id, {})[candidate.candidate_id] = (
            admitted_cand
        )
        return updated_pack

    def admitted_candidates(self, pack_id: str) -> tuple[PackPatchCandidate, ...]:
        """Return candidates previously admitted for ``pack_id``."""
        return tuple(self._admitted.get(pack_id, {}).values())
