"""Governed rule accumulation and pack patch admission for ``form_tx``.

Induces ``PackPatchCandidate`` proposals strictly from repeated, contradiction-free
human co-pilot resolutions across distinct runs (fail-closed on any disagreement),
and gates pack updates through explicit immutable admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional, Sequence

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
    approval_id: Optional[str] = None,
    qualification_check: Optional[Callable[[FormPack], bool]] = None,
) -> FormPack:
    """Apply a proposal only after explicit approval and qualification of the new pack.

    ``qualification_check`` is a caller-supplied trusted qualification hook, not
    evidence inferred from repeated operator answers. It must assess the exact
    resulting pack and return the Boolean ``True``. No rule is promoted by default.
    """
    if not approval_id or not approval_id.strip():
        raise ValueError("Rule admission requires an explicit approval_id")
    if qualification_check is None:
        raise ValueError("Rule admission requires qualification of the resulting pack")

    def qualified(updated_pack: FormPack) -> FormPack:
        if qualification_check(updated_pack) is not True:
            raise ValueError("Resulting pack did not pass qualification")
        return updated_pack

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
        return qualified(
            pack.model_copy(update={"bindings": (*pack.bindings, new_binding)})
        )

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
        return qualified(pack.model_copy(update={"bindings": tuple(updated_bindings)}))

    raise ValueError(
        f"Candidate kind {candidate.proposed_kind!r} cannot be admitted as a field binding"
    )


class RuleAccumulator:
    """Accumulates human resolutions across runs and induces consensus patch candidates."""

    def __init__(self) -> None:
        # Evidence is bound to an exact parent pack and one descriptor per run.
        self._observations: dict[str, dict[str, dict[str, _ResolutionDescriptor]]] = {}
        self._pack_digests: dict[str, str] = {}
        # pack_id -> set of field_signatures that had intra-run or inter-run contradiction
        self._contradicted: dict[str, set[str]] = {}
        # Reused keys must identify the same scoped decision, not hide contradictions.
        self._seen_idempotency_keys: dict[str, dict[str, str]] = {}
        # pack_id -> candidate_id -> admitted PackPatchCandidate
        self._admitted: dict[str, dict[str, PackPatchCandidate]] = {}

    def record_run_resolutions(
        self,
        pack_id: str,
        records: Sequence[HumanResolutionRecord],
        *,
        run_id: Optional[str] = None,
        pack_digest: Optional[str] = None,
    ) -> None:
        """Record support from an identified run of one exact pack version.

        Legacy calls without scope are accepted but cannot establish support.
        The caller supplies run and pack identity from the execution session;
        changing an operator idempotency key never creates another supporting run.
        """
        if not pack_id:
            raise ValueError("pack_id must be non-empty")

        if run_id is None or pack_digest is None:
            return
        if not run_id.strip() or not pack_digest.strip():
            raise ValueError("run_id and pack_digest must be non-empty")
        known_digest = self._pack_digests.get(pack_id)
        if known_digest is not None and known_digest != pack_digest:
            raise ValueError("Cannot mix rule evidence from different pack versions")
        self._pack_digests[pack_id] = pack_digest

        seen_keys = self._seen_idempotency_keys.setdefault(pack_id, {})
        pack_obs = self._observations.setdefault(pack_id, {})
        pack_contradicted = self._contradicted.setdefault(pack_id, set())

        per_run_by_sig: dict[str, set[_ResolutionDescriptor]] = {}
        for rec in records:
            if not _is_eligible_record(rec):
                continue
            sig = rec.field_signature.strip()
            if not sig:
                continue
            decision_digest = canonical_sha256(
                {"run_id": run_id, "record": rec.model_dump()}
            )
            prior_digest = seen_keys.get(rec.idempotency_key)
            if prior_digest is not None:
                if prior_digest != decision_digest:
                    pack_contradicted.add(sig)
                continue
            seen_keys[rec.idempotency_key] = decision_digest
            desc = _describe_resolution(rec)
            per_run_by_sig.setdefault(sig, set()).add(desc)

        for sig, descriptors in per_run_by_sig.items():
            if len(descriptors) > 1:
                pack_contradicted.add(sig)
                continue
            (only_desc,) = tuple(descriptors)
            history = pack_obs.setdefault(sig, {})
            if any(prev != only_desc for prev in history.values()):
                pack_contradicted.add(sig)
            history[run_id] = only_desc

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
            unique = set(history.values())
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
                    "pack_digest": self._pack_digests.get(pack_id),
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
        approval_id: Optional[str] = None,
        qualification_check: Optional[Callable[[FormPack], bool]] = None,
    ) -> FormPack:
        """Require current evidence, explicit approval, and qualification before promotion."""
        if self._pack_digests.get(pack.pack_id) != pack.digest():
            raise ValueError(
                "Candidate evidence does not match the current pack digest"
            )
        current_candidates = {
            item.candidate_id: item for item in self.induce_candidates(pack.pack_id)
        }
        if current_candidates.get(candidate.candidate_id) != candidate:
            raise ValueError(
                "Candidate is not supported by current accumulated evidence"
            )
        updated_pack = admit_candidate_into_pack(
            pack=pack,
            candidate=candidate,
            anchor=anchor,
            codec=codec,
            control_kind=control_kind,
            prefill_policy=prefill_policy,
            triggers_re_render=triggers_re_render,
            required_in_pack=required_in_pack,
            approval_id=approval_id,
            qualification_check=qualification_check,
        )
        admitted_cand = candidate.model_copy(update={"admitted": True})
        self._admitted.setdefault(pack.pack_id, {})[candidate.candidate_id] = (
            admitted_cand
        )
        return updated_pack

    def admitted_candidates(self, pack_id: str) -> tuple[PackPatchCandidate, ...]:
        """Return candidates previously admitted for ``pack_id``."""
        return tuple(self._admitted.get(pack_id, {}).values())
