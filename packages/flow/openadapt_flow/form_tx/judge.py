"""Post-commit summary/search read-back judge for ``form_tx``.

The judge reads through the surface of the session that did the work
(``acting_session_reread``). It is not an independent check: only a read by a
fresh, separate session would be.

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
    is_radio,
    match_anchor_in_snapshot,
    match_text_pair_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldBindingSpec,
    FieldCodecSpec,
    FieldWriteRecord,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormSnapshot,
    HumanDecisionAction,
    HumanResolutionRecord,
    InputDossier,
    ObservedControl,
    ObservedTextPair,
    OutputExtractionSpec,
    ProvenanceKind,
    ReadbackFieldVerdict,
    ReadbackStatus,
    Subject,
    SummaryVerdict,
    WizardScreenSpec,
    read_subjects,
    subjects_digest,
    uncovered_subject_verdicts,
)

__all__ = [
    "Subject",
    "SummaryJudge",
    "build_authoritative_fact_map",
    "read_subjects",
    "uncovered_subject_verdicts",
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
    # partial-match-as-match: a radio option matches only when it is selected.
    if (
        ctrl.input_type.casefold() == "radio" or ctrl.role.casefold() == "radio"
    ) and ctrl.checked is not True:
        return False
    if (
        binding.control_kind == ControlKind.CHECKBOX
        and ctrl.checked is not None
        and (not ctrl.current_value.strip() or is_radio(ctrl))
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


def _text_pair_matches(
    expected_val: Any,
    pair: ObservedTextPair,
    binding: FieldBindingSpec,
) -> bool:
    """Compare an expected value with a value shown as page text."""
    if _is_placeholder_or_empty(pair.value, binding.placeholder_values):
        return False
    if values_equivalent(expected_val, pair.value, binding.codec):
        return True
    if binding.control_kind in (ControlKind.CHECKBOX, ControlKind.RADIO_GROUP):
        return values_equivalent(
            expected_val,
            pair.value,
            FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
        )
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
            # A read-only summary may show the output (a quote number, a
            # premium) as page text rather than in a control.
            try:
                pair = match_text_pair_in_snapshot(spec.anchor, snapshot)
            except AmbiguousAnchorError:
                invalid_keys.add(spec.output_key)
                continue
            if pair is None or not pair.value.strip():
                continue
            try:
                extracted[spec.output_key] = normalize_with_codec(
                    pair.value.strip(), spec.codec
                )
            except CodecValueError:
                invalid_keys.add(spec.output_key)
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
    """Read-back verifier for committed form transactions (acting session)."""

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
        writes: Sequence[FieldWriteRecord] = (),
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
                passed=False,
                checked_count=0,
                failed_keys=("missing_readback_contract",),
                field_verdicts=(),
                extracted_outputs=dict(known_outputs or {}),
                missing_required_outputs=(),
            )

        expected_facts = build_authoritative_fact_map(
            active_dossier,
            active_resolutions,
            supplemental_facts=supplemental_facts,
        )

        summary_screen_id = "__form_tx_readback__"
        observation_screens = active_pack.screens
        if summary_check.summary_landmark_selector:
            observation_screens = (
                WizardScreenSpec(
                    screen_id=summary_screen_id,
                    landmark_selector=summary_check.summary_landmark_selector,
                ),
                *observation_screens,
            )
        snapshot = active_surface.snapshot(screens=observation_screens)
        seeded_outputs: dict[str, Any] = dict(known_outputs or {})
        pre_outputs, _ = _extract_present_outputs(snapshot, summary_check.outputs)
        seeded_outputs.update(pre_outputs)

        # Navigate to the summary/search route if configured and not already there.
        if summary_check.summary_url_path and not _url_matches(
            snapshot.url_origin_and_path, summary_check.summary_url_path
        ):
            active_surface.goto(summary_check.summary_url_path)
            snapshot = active_surface.snapshot(screens=observation_screens)

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
                search_failure_reason = f"Missing search key value for {lookup_key!r} prior to summary lookup"
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
                        search_failure_reason = "Summary search input or submit control not found on surface"
                    else:
                        active_surface.fill_text(
                            input_ctrl.dom_selector, str(lookup_val)
                        )
                        active_surface.click(submit_ctrl.dom_selector)
                        snapshot = active_surface.snapshot(screens=observation_screens)

        verdicts: list[ReadbackFieldVerdict] = []

        def _codec_for(key: str) -> FormCodecKind:
            for binding in (*summary_check.field_readbacks, *active_pack.bindings):
                if binding.dossier_key == key:
                    return binding.codec.kind
            for subroutine in active_pack.popup_subroutines:
                for binding in subroutine.bindings:
                    if binding.dossier_key == key:
                        return binding.codec.kind
                if subroutine.return_output_key == key:
                    return subroutine.return_codec.kind
            return FormCodecKind.WHITESPACE_COLLAPSE

        def _not_checked(
            key: str, provenance: ProvenanceKind, reason: str
        ) -> ReadbackFieldVerdict:
            return ReadbackFieldVerdict(
                dossier_key=key,
                codec_kind=_codec_for(key),
                passed=False,
                status=ReadbackStatus.NOT_CHECKED,
                provenance=provenance,
                reason=reason,
            )

        def _failed(
            key: str, codec_kind: FormCodecKind, provenance: ProvenanceKind, reason: str
        ) -> ReadbackFieldVerdict:
            return ReadbackFieldVerdict(
                dossier_key=key,
                codec_kind=codec_kind,
                passed=False,
                status=ReadbackStatus.FAILED,
                provenance=provenance,
                reason=reason,
            )

        # Coverage is the union of what the pack binds and what the run
        # actually wrote. A matching subset does not establish that the
        # intended write landed: every bound grounded fact, every field typed
        # or selected (including inside popups and by the operator) and every
        # popup return must get a verdict. A field with no way to read it back
        # is reported as not_checked, never passed.
        declared_keys = {
            binding.dossier_key for binding in summary_check.field_readbacks
        }
        expected_keys = {
            binding.dossier_key
            for binding in active_pack.bindings
            if binding.dossier_key in expected_facts
        }
        for resolution in active_resolutions:
            if resolution.dossier_key and resolution.dossier_key in expected_facts:
                expected_keys.add(resolution.dossier_key)
        for subroutine in active_pack.popup_subroutines:
            if subroutine.return_output_key in expected_facts:
                expected_keys.add(subroutine.return_output_key)
        written_by_key: dict[str, list[FieldWriteRecord]] = {}
        for write in writes:
            if write.dossier_key:
                written_by_key.setdefault(write.dossier_key, []).append(write)
        unbound_writes: dict[str, list[FieldWriteRecord]] = {}
        for write in writes:
            if not write.dossier_key:
                unbound_writes.setdefault(write.field_key, []).append(write)
        unbound_human_fact = any(
            resolution.halt_kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
            and resolution.action in _OVERRIDE_PROVENANCE
            and not resolution.dossier_key
            for resolution in active_resolutions
        )

        def _where(field_writes: Sequence[FieldWriteRecord]) -> str:
            popups = sorted(
                {w.in_popup_subroutine for w in field_writes if w.in_popup_subroutine}
            )
            if not popups:
                return ""
            guarded = all(
                w.checked_before_apply for w in field_writes if w.in_popup_subroutine
            )
            note = f" (entered in popup {', '.join(popups)}"
            if guarded:
                note += "; it matched when re-read before apply, but the record does not show it"
            return note + ")"

        for key in sorted((expected_keys | set(written_by_key)) - declared_keys):
            fact = expected_facts.get(key)
            verdicts.append(
                _not_checked(
                    key,
                    fact.provenance
                    if fact is not None
                    else ProvenanceKind.INTAKE_DOCUMENT,
                    "No read-back is declared for this field"
                    + _where(written_by_key.get(key, ())),
                )
            )
        for field_key in sorted(unbound_writes):
            verdicts.append(
                _not_checked(
                    field_key,
                    ProvenanceKind.HUMAN_OPERATOR,
                    "Operator entered a value on a control with no binding, so "
                    "no read-back can cover it" + _where(unbound_writes[field_key]),
                )
            )
        if unbound_human_fact and not unbound_writes:
            verdicts.append(
                _not_checked(
                    "control:operator-entered",
                    ProvenanceKind.HUMAN_OPERATOR,
                    "Operator answered a required control with no binding, so "
                    "no read-back can cover it",
                )
            )

        for binding in summary_check.field_readbacks:
            fact = expected_facts.get(binding.dossier_key)
            prov = (
                fact.provenance if fact is not None else ProvenanceKind.INTAKE_DOCUMENT
            )
            if search_failure_reason is not None:
                verdicts.append(
                    _not_checked(binding.dossier_key, prov, search_failure_reason)
                )
                continue

            try:
                observed_ctrl = match_anchor_in_snapshot(
                    binding.anchor, snapshot, include_readonly=True
                )
            except AmbiguousAnchorError as exc:
                verdicts.append(
                    _not_checked(
                        binding.dossier_key,
                        prov,
                        f"Ambiguous summary read-back anchor for {binding.dossier_key!r}: {exc}",
                    )
                )
                continue

            # With no control, the summary may show the value as page text.
            observed_text: Optional[ObservedTextPair] = None
            if observed_ctrl is None:
                try:
                    observed_text = match_text_pair_in_snapshot(
                        binding.anchor, snapshot
                    )
                except AmbiguousAnchorError as exc:
                    verdicts.append(
                        _not_checked(
                            binding.dossier_key,
                            prov,
                            f"Page text shows {binding.dossier_key!r} more than "
                            f"once, so it cannot be read back: {exc}",
                        )
                    )
                    continue

            if fact is None:
                shown_value = (
                    observed_ctrl.current_value
                    if observed_ctrl is not None
                    else observed_text.value
                    if observed_text is not None
                    else ""
                )
                has_ungrounded_screen_val = not _is_placeholder_or_empty(
                    shown_value, binding.placeholder_values
                )
                if has_ungrounded_screen_val:
                    verdicts.append(
                        _failed(
                            binding.dossier_key,
                            binding.codec.kind,
                            prov,
                            f"Summary screen showed unexpected non-empty value for "
                            f"ungrounded key {binding.dossier_key!r}",
                        )
                    )
                elif binding.required_in_pack:
                    verdicts.append(
                        _not_checked(
                            binding.dossier_key,
                            prov,
                            f"Required summary read-back key {binding.dossier_key!r} "
                            f"has no expected value to compare",
                        )
                    )
                elif binding.dossier_key in written_by_key:
                    # The run wrote this field but there is no value to
                    # compare: it gets a verdict, never silence.
                    verdicts.append(
                        _not_checked(
                            binding.dossier_key,
                            prov,
                            f"{binding.dossier_key!r} was written with no expected "
                            "value, and the summary shows it empty"
                            + _where(written_by_key[binding.dossier_key]),
                        )
                    )
                continue

            if observed_ctrl is None and observed_text is not None:
                if _text_pair_matches(fact.value, observed_text, binding):
                    verdicts.append(
                        ReadbackFieldVerdict(
                            dossier_key=binding.dossier_key,
                            codec_kind=binding.codec.kind,
                            passed=True,
                            status=ReadbackStatus.PASSED,
                            provenance=prov,
                            reason=(
                                "Read from page text "
                                f"({observed_text.source}: {observed_text.label!r})"
                            ),
                        )
                    )
                else:
                    shown = (
                        "an empty value"
                        if _is_placeholder_or_empty(
                            observed_text.value, binding.placeholder_values
                        )
                        else "a different value"
                    )
                    verdicts.append(
                        _failed(
                            binding.dossier_key,
                            binding.codec.kind,
                            prov,
                            f"Page text shows {shown} for {binding.dossier_key!r} "
                            f"({observed_text.source}: {observed_text.label!r}) "
                            f"under codec {binding.codec.kind.value!r}",
                        )
                    )
                continue

            if observed_ctrl is None:
                verdicts.append(
                    _not_checked(
                        binding.dossier_key,
                        prov,
                        f"Summary read-back control missing for {binding.dossier_key!r}",
                    )
                )
                continue

            if _observed_control_matches(fact.value, observed_ctrl, binding):
                verdicts.append(
                    ReadbackFieldVerdict(
                        dossier_key=binding.dossier_key,
                        codec_kind=binding.codec.kind,
                        passed=True,
                        status=ReadbackStatus.PASSED,
                        provenance=prov,
                        reason="",
                    )
                )
            else:
                verdicts.append(
                    _failed(
                        binding.dossier_key,
                        binding.codec.kind,
                        prov,
                        f"Read-back value mismatch for {binding.dossier_key!r} "
                        f"under codec {binding.codec.kind.value!r}",
                    )
                )

        observations: dict[str, set[str]] = {}
        for rb in summary_check.field_readbacks:
            try:
                oc = match_anchor_in_snapshot(
                    rb.anchor, snapshot, include_readonly=True
                )
            except AmbiguousAnchorError:
                continue
            if oc is not None:
                observations.setdefault(rb.dossier_key, set()).add(oc.dom_selector)
                continue
            # A value shown as page text, not in a control, is an observation
            # too; two read-backs that land on the same shown pair are one.
            try:
                pair = match_text_pair_in_snapshot(rb.anchor, snapshot)
            except AmbiguousAnchorError:
                continue
            if pair is not None:
                observations.setdefault(rb.dossier_key, set()).add(
                    f"text|{pair.source}|{pair.section_text}|{pair.label}"
                )
        evaluated_subjects = read_subjects(writes, active_resolutions)
        verdicts.extend(
            uncovered_subject_verdicts(
                evaluated_subjects,
                verdicts,
                _codec_for,
                {k: len(v) for k, v in observations.items()},
            )
        )

        # A value that came only from a popup is no better checked than the
        # popup inputs that produced it. If one of those inputs is not
        # checked, the returned value is not checked either, unless the value
        # put back matched the popup's own displayed result and the summary
        # shows it.
        status_by_key: dict[str, ReadbackStatus] = {}
        for v in verdicts:
            status_by_key.setdefault(v.dossier_key, v.status)
            if v.status != ReadbackStatus.PASSED:
                status_by_key[v.dossier_key] = v.status
        for write in writes:
            sub_id = write.from_popup_subroutine
            if write.source != "popup_return" or not sub_id or not write.dossier_key:
                continue
            fact = expected_facts.get(write.dossier_key)
            if fact is None or fact.source_id != f"subroutine:{sub_id}":
                continue
            if write.matched_popup_result:
                continue
            unchecked_inputs = sorted(
                {
                    w.field_key
                    for w in writes
                    if w.in_popup_subroutine == sub_id
                    and status_by_key.get(w.field_key) != ReadbackStatus.PASSED
                }
            )
            if not unchecked_inputs:
                continue
            for i, v in enumerate(verdicts):
                if (
                    v.dossier_key == write.dossier_key
                    and v.status == ReadbackStatus.PASSED
                ):
                    verdicts[i] = _not_checked(
                        v.dossier_key,
                        v.provenance,
                        f"Value came from popup {sub_id!r}, whose inputs "
                        f"{unchecked_inputs} are not checked, and was not "
                        "compared with the popup's displayed result",
                    )

        screen_problems: list[str] = []
        if snapshot.auth_wall_present:
            screen_problems.append("sign-in wall")
        if snapshot.active_modals:
            screen_problems.append("open dialog")
        if snapshot.banner_errors:
            screen_problems.append("error banner")
        if snapshot.inflight_mutations:
            screen_problems.append("save still in flight")
        if (
            summary_check.summary_landmark_selector
            and snapshot.screen_id != summary_screen_id
        ):
            screen_problems.append("summary screen not reached")
        if screen_problems:
            verdicts.append(
                _failed(
                    "__summary_screen__",
                    FormCodecKind.WHITESPACE_COLLAPSE,
                    ProvenanceKind.INTAKE_DOCUMENT,
                    "Summary screen was not readable: " + ", ".join(screen_problems),
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

        failed_keys = tuple(
            v.dossier_key for v in verdicts if v.status == ReadbackStatus.FAILED
        )
        not_checked_keys = tuple(
            v.dossier_key for v in verdicts if v.status == ReadbackStatus.NOT_CHECKED
        )
        passed = (
            search_failure_reason is None
            and bool(verdicts)
            and len(failed_keys) == 0
            and len(not_checked_keys) == 0
            and len(missing_required) == 0
        )

        return SummaryVerdict(
            passed=passed,
            checked_count=sum(
                1 for v in verdicts if v.status != ReadbackStatus.NOT_CHECKED
            ),
            failed_keys=failed_keys,
            not_checked_keys=not_checked_keys,
            field_verdicts=tuple(verdicts),
            extracted_outputs=final_outputs,
            missing_required_outputs=tuple(missing_required),
            observations_per_key={k: len(v) for k, v in observations.items()},
            ledger_digest=subjects_digest(evaluated_subjects),
        )

    verify = evaluate
