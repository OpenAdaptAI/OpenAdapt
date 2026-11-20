"""Independent post-commit summary/search read-back judge for ``form_tx``.

Verifies committed portal state against the authoritative expected fact map
(grounded ``InputDossier`` facts overlaid with verified human co-pilot
resolutions) and extracts typed outputs. Zero model calls; fail-closed on
missing controls, mismatched values, or missing required outputs.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from openadapt_flow.form_tx.codecs import (
    CodecValueError,
    collapse_whitespace,
    normalize_with_codec,
    values_equivalent,
)
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    FormSurfaceBackend,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldBindingSpec,
    FormPack,
    FormSnapshot,
    HumanDecisionAction,
    HumanResolutionRecord,
    InputDossier,
    ObservedControl,
    OutputExtractionSpec,
    ProvenanceKind,
    ReadbackFieldVerdict,
    SummaryVerdict,
)

__all__ = [
    "SummaryJudge",
    "build_authoritative_fact_map",
]

_OVERRIDE_PROVENANCE: dict[HumanDecisionAction, ProvenanceKind] = {
    HumanDecisionAction.PROVIDE_VALUE: ProvenanceKind.HUMAN_OPERATOR,
    HumanDecisionAction.FIXED_ON_SCREEN: ProvenanceKind.HUMAN_OPERATOR,
    HumanDecisionAction.ACCEPT_PORTAL_PREFILL: ProvenanceKind.ACCEPTED_PORTAL_PREFILL,
}


def build_authoritative_fact_map(
    dossier: InputDossier,
    human_resolutions: Sequence[HumanResolutionRecord] = (),
    *,
    supplemental_facts: Optional[Mapping[str, DossierFact]] = None,
) -> dict[str, DossierFact]:
    """Construct expected facts from ``dossier`` overlaid with human overrides."""
    expected: dict[str, DossierFact] = {}
    if supplemental_facts:
        for key, fact in supplemental_facts.items():
            if fact is not None and fact.value is not None:
                expected[key] = fact

    for key in dossier.facts:
        grounded = dossier.grounded_fact(key)
        if grounded is not None:
            expected[key] = grounded

    for rec in human_resolutions:
        prov = _OVERRIDE_PROVENANCE.get(rec.action)
        if prov is None:
            continue
        if not rec.dossier_key or rec.resolved_value is None:
            continue
        if isinstance(rec.resolved_value, str) and not rec.resolved_value.strip():
            continue
        expected[rec.dossier_key] = DossierFact(
            key=rec.dossier_key,
            value=rec.resolved_value,
            provenance=prov,
            source_id=f"human:{rec.actor_id}:{rec.idempotency_key}",
            confidence=1.0,
        )

    return expected


def _url_matches(current_url: str, target_url_or_path: str) -> bool:
    cur = current_url.strip()
    tgt = target_url_or_path.strip()
    if not tgt:
        return True
    if cur == tgt:
        return True
    cur_parts = urlsplit(cur)
    tgt_parts = urlsplit(tgt)
    if not tgt_parts.scheme and not tgt_parts.netloc:
        cur_path = cur_parts.path.rstrip("/") or "/"
        tgt_path = tgt_parts.path.rstrip("/") or "/"
        return cur_path == tgt_path
    return (
        cur_parts.scheme.casefold() == tgt_parts.scheme.casefold()
        and cur_parts.netloc.casefold() == tgt_parts.netloc.casefold()
        and (cur_parts.path.rstrip("/") or "/") == (tgt_parts.path.rstrip("/") or "/")
    )


def _is_placeholder_or_empty(value: str, placeholders: Sequence[str]) -> bool:
    norm = collapse_whitespace(value).casefold()
    if not norm:
        return True
    placeholder_set = {collapse_whitespace(p).casefold() for p in placeholders}
    return norm in placeholder_set


def _observed_control_matches(
    expected_val: Any,
    ctrl: ObservedControl,
    binding: FieldBindingSpec,
) -> bool:
    if (
        binding.control_kind == ControlKind.CHECKBOX
        and not ctrl.current_value.strip()
        and ctrl.checked is not None
    ):
        return values_equivalent(expected_val, ctrl.checked, binding.codec)

    if values_equivalent(expected_val, ctrl.current_value, binding.codec):
        return True

    if ctrl.options:
        matching_opts = [opt for opt in ctrl.options if opt.value == ctrl.current_value]
        if len(matching_opts) == 1 and values_equivalent(
            expected_val, matching_opts[0].label, binding.codec
        ):
            return True

    return False


def _extract_present_outputs(
    snapshot: FormSnapshot,
    output_specs: Sequence[OutputExtractionSpec],
) -> tuple[dict[str, Any], set[str]]:
    """Return ``(normalized_outputs, invalid_or_ambiguous_keys)`` present on ``snapshot``."""
    extracted: dict[str, Any] = {}
    invalid_keys: set[str] = set()
    for spec in output_specs:
        try:
            ctrl = match_anchor_in_snapshot(
                spec.anchor, snapshot, include_readonly=True
            )
        except AmbiguousAnchorError:
            invalid_keys.add(spec.output_key)
            continue
        if ctrl is None:
            continue
        raw_val = ctrl.current_value.strip()
        if not raw_val:
            continue
        try:
            extracted[spec.output_key] = normalize_with_codec(raw_val, spec.codec)
        except CodecValueError:
            invalid_keys.add(spec.output_key)
    return extracted, invalid_keys


class SummaryJudge:
    """Independent read-back verifier for committed form transactions."""

    def __init__(
        self,
        surface: Optional[FormSurfaceBackend] = None,
        pack: Optional[FormPack] = None,
        dossier: Optional[InputDossier] = None,
        human_resolutions: Sequence[HumanResolutionRecord] = (),
    ) -> None:
        self._surface = surface
        self._pack = pack
        self._dossier = dossier
        self._human_resolutions = tuple(human_resolutions)

    def evaluate(
        self,
        surface: Optional[FormSurfaceBackend] = None,
        pack: Optional[FormPack] = None,
        dossier: Optional[InputDossier] = None,
        human_resolutions: Optional[Sequence[HumanResolutionRecord]] = None,
        *,
        known_outputs: Optional[Mapping[str, Any]] = None,
        supplemental_facts: Optional[Mapping[str, DossierFact]] = None,
    ) -> SummaryVerdict:
        """Navigate/search if configured, verify all read-backs, and extract outputs."""
        active_surface = surface if surface is not None else self._surface
        active_pack = pack if pack is not None else self._pack
        active_dossier = dossier if dossier is not None else self._dossier
        active_resolutions = (
            tuple(human_resolutions)
            if human_resolutions is not None
            else self._human_resolutions
        )
        if active_surface is None or active_pack is None or active_dossier is None:
            raise ValueError(
                "SummaryJudge.evaluate requires surface, pack, and dossier"
            )

        summary_check = active_pack.summary_check
        if summary_check is None:
            return SummaryVerdict(
                passed=True,
                checked_count=0,
                failed_keys=(),
                field_verdicts=(),
                extracted_outputs=dict(known_outputs or {}),
                missing_required_outputs=(),
            )

        expected_facts = build_authoritative_fact_map(
            active_dossier,
            active_resolutions,
            supplemental_facts=supplemental_facts,
        )

        snapshot = active_surface.snapshot(screens=active_pack.screens)
        seeded_outputs: dict[str, Any] = dict(known_outputs or {})
        pre_outputs, _ = _extract_present_outputs(snapshot, summary_check.outputs)
        seeded_outputs.update(pre_outputs)

        # Navigate to independent summary/search route if configured and not already there.
        if summary_check.summary_url_path and not _url_matches(
            snapshot.url_origin_and_path, summary_check.summary_url_path
        ):
            active_surface.goto(summary_check.summary_url_path)
            snapshot = active_surface.snapshot(screens=active_pack.screens)

        # Execute search-by-key lookup when configured.
        search_failure_reason: Optional[str] = None
        if (
            summary_check.search_by_output_key
            and summary_check.search_input_anchor is not None
            and summary_check.search_submit_anchor is not None
        ):
            lookup_key = summary_check.search_by_output_key
            lookup_val = seeded_outputs.get(lookup_key)
            if lookup_val is None and lookup_key in expected_facts:
                lookup_val = expected_facts[lookup_key].value

            if lookup_val is None or (
                isinstance(lookup_val, str) and not lookup_val.strip()
            ):
                search_failure_reason = (
                    f"Missing search key value for {lookup_key!r} prior to summary lookup"
                )
            else:
                try:
                    input_ctrl = match_anchor_in_snapshot(
                        summary_check.search_input_anchor,
                        snapshot,
                        include_readonly=False,
                    )
                    submit_ctrl = match_anchor_in_snapshot(
                        summary_check.search_submit_anchor,
                        snapshot,
                        include_readonly=True,
                    )
                except AmbiguousAnchorError as exc:
                    search_failure_reason = f"Ambiguous summary search anchor: {exc}"
                else:
                    if input_ctrl is None or submit_ctrl is None:
                        search_failure_reason = (
                            "Summary search input or submit control not found on surface"
                        )
                    else:
                        active_surface.fill_text(
                            input_ctrl.dom_selector, str(lookup_val)
                        )
                        active_surface.click(submit_ctrl.dom_selector)
                        snapshot = active_surface.snapshot(screens=active_pack.screens)

        verdicts: list[ReadbackFieldVerdict] = []
        for binding in summary_check.field_readbacks:
            fact = expected_facts.get(binding.dossier_key)
            if search_failure_reason is not None:
                prov = (
                    fact.provenance
                    if fact is not None
                    else ProvenanceKind.INTAKE_DOCUMENT
                )
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=False,
                        provenance=prov,
                        reason=search_failure_reason,
                    )
                )
                continue

            try:
                observed_ctrl = match_anchor_in_snapshot(
                    binding.anchor, snapshot, include_readonly=True
                )
            except AmbiguousAnchorError as exc:
                prov = (
                    fact.provenance
                    if fact is not None
                    else ProvenanceKind.INTAKE_DOCUMENT
                )
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=False,
                        provenance=prov,
                        reason=f"Ambiguous summary read-back anchor for {binding.dossier_key!r}: {exc}",
                    )
                )
                continue

            if fact is None:
                has_ungrounded_screen_val = observed_ctrl is not None and not _is_placeholder_or_empty(
                    observed_ctrl.current_value, binding.placeholder_values
                )
                if binding.required_in_pack:
                    verdicts.append(
                        ReadbackFieldVerdict(
                            dossier_key=binding.dossier_key,
                            codec_kind=binding.codec.kind,
                            passed=False,
                            provenance=ProvenanceKind.INTAKE_DOCUMENT,
                            reason=f"Required summary read-back key {binding.dossier_key!r} has no grounded fact",
                        )
                    )
                elif has_ungrounded_screen_val:
                    verdicts.append(
                        ReadbackFieldVerdict(
                            dossier_key=binding.dossier_key,
                            codec_kind=binding.codec.kind,
                            passed=False,
                            provenance=ProvenanceKind.INTAKE_DOCUMENT,
                            reason=(
                                f"Summary screen showed unexpected non-empty value for "
                                f"ungrounded key {binding.dossier_key!r}"
                            ),
                        )
                    )
                continue

            if observed_ctrl is None:
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=False,
                        provenance=fact.provenance,
                        reason=f"Summary read-back control missing for {binding.dossier_key!r}",
                    )
                )
                continue

            if _observed_control_matches(fact.value, observed_ctrl, binding):
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=True,
                        provenance=fact.provenance,
                        reason="",
                    )
                )
            else:
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=False,
                        provenance=fact.provenance,
                        reason=(
                            f"Read-back value mismatch for {binding.dossier_key!r} "
                            f"under codec {binding.codec.kind.value!r}"
                        ),
                    )
                )

        final_outputs: dict[str, Any] = {}
        missing_required: list[str] = []

        if search_failure_reason is not None:
            for out_spec in summary_check.outputs:
                if out_spec.required:
                    missing_required.append(out_spec.output_key)
        else:
            live_outputs, invalid_output_keys = _extract_present_outputs(
                snapshot, summary_check.outputs
            )
            for out_spec in summary_check.outputs:
                key = out_spec.output_key
                if key in invalid_output_keys:
                    if out_spec.required:
                        missing_required.append(key)
                    continue
                if key in live_outputs:
                    final_outputs[key] = live_outputs[key]
                elif key in seeded_outputs:
                    # Preserve output captured on post-commit receipt before search navigation,
                    # provided the live summary screen didn't render an empty version of that control.
                    live_ctrl = match_anchor_in_snapshot(
                        out_spec.anchor, snapshot, include_readonly=True
                    )
                    if live_ctrl is not None and not live_ctrl.current_value.strip():
                        if out_spec.required:
                            missing_required.append(key)
                    else:
                        final_outputs[key] = seeded_outputs[key]
                elif out_spec.required:
                    missing_required.append(key)

        failed_keys = tuple(v.dossier_key for v in verdicts if not v.passed)
        passed = (
            search_failure_reason is None
            and len(failed_keys) == 0
            and len(missing_required) == 0
        )

        return SummaryVerdict(
            passed=passed,
            checked_count=len(verdicts),
            failed_keys=failed_keys,
            field_verdicts=tuple(verdicts),
            extracted_outputs=final_outputs,
            missing_required_outputs=tuple(missing_required),
        )

    verify = evaluate
