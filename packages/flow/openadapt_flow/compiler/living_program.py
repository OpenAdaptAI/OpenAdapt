"""Living program compilation: 2-run coincidence pruning and guarded branch splicing.

Extends single-demonstration compiled workflows across subsequent runs without
LLM calls on the replay hot path:

1. **Automatic 2-run coincidence pruning** (``prune_coincidences_from_two_runs``):
   Compares a target's Run 1 scale-normalized OCR constellation against Run 2's
   OCR tokens around the aligned target region. Tokens that vanished or changed
   between Run 1 and Run 2 (such as dynamic table rows, patient names, or
   ephemeral IDs) are pruned automatically while invariant immediate field
   labels and static chrome tokens are preserved.
2. **Incremental PrepareCard branch splicing** (``splice_prepare_branch`` and
   ``match_branch``): Compiles a signed, zero-PHI operator ``PrepareResolution``
   for an unseen modal or choice dialog into a guarded conditional branch in a
   ``LivingBranchTable``. Subsequent runs auto-resolve that exact modal at $0
   and 0 human stops only when both the structural prompt guard tokens and the
   exact option set match with zero ambiguity; novel or unsafe modals return
   ``None`` so execution halts safely to ``PrepareCard``.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from openadapt_flow.cloud_prepare_bridge import (
    ZeroPhiViolationError,
    assert_prepare_card_zero_phi,
    assert_zero_phi_value,
    prepare_resolution_to_human_input,
    verify_resolution_digest,
)
from openadapt_flow.copilot_protocol import HumanDecisionAction
from openadapt_flow.ir import Point, Region
from openadapt_flow.pixel_prepare_card import (
    OcrWord,
    _group_ocr_rows,
    _normalize_ocr_word,
    pixel_prepare_card_to_form_halt,
)
from openadapt_flow.report_redaction import REDACTED, find_unredacted
from openadapt_flow.runtime.constellation_grounder import (
    _IMMEDIATE_MAX_DX_ERR_NORM,
    _IMMEDIATE_MAX_DY_ERR_NORM,
    DEFAULT_MAX_ERR_NORM,
    DEFAULT_SIGMA_NORM,
    ConstellationToken,
    OcrToken,
    TargetConstellation,
    _immediate_text_similarity,
    _median_token_height,
    _point_in_region,
    _token_text_similarity,
    normalize_token_text,
)

#: Minimum Run 2 spatial-text match score for a Run 1 constellation token to survive.
DEFAULT_PRUNE_MIN_MATCH_SCORE = 0.35

#: Glue words dropped only from redacted-template guards when content tokens remain.
_GLUE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)

#: Safety-critical negation or hazard tokens that must never appear unexpectedly
#: inside a redacted-slot match unless they were part of the approved guard.
_HAZARD_CONTRADICTION_TOKENS = frozenset(
    {
        "abort",
        "alert",
        "blocked",
        "cancel",
        "cancelled",
        "canceled",
        "cannot",
        "conflict",
        "corrupt",
        "delete",
        "denied",
        "deny",
        "different",
        "duplicate",
        "error",
        "expired",
        "fail",
        "failed",
        "failure",
        "fraud",
        "inactive",
        "incorrect",
        "invalid",
        "locked",
        "mismatch",
        "missing",
        "never",
        "no",
        "not",
        "overwrite",
        "rejected",
        "revoked",
        "suspended",
        "terminated",
        "unable",
        "unauthorized",
        "unknown",
        "unverified",
        "void",
        "warning",
        "wrong",
    }
)


class PruningReport(BaseModel):
    """Structured report returned by :func:`prune_coincidences_from_two_runs`."""

    constellation: TargetConstellation
    kept_tokens: list[ConstellationToken]
    pruned_tokens: list[ConstellationToken]
    preserved_immediate_labels: list[str]
    run1_token_count: int
    run2_token_count: int
    kept_count: int
    pruned_count: int
    pruning_applied: bool = True
    skipped_reason: Optional[str] = None

    @property
    def cleaned_constellation(self) -> TargetConstellation:
        """Alias for ``constellation``."""
        return self.constellation


class GuardedBranch(BaseModel):
    """Deterministic zero-PHI conditional branch compiled from a PrepareResolution."""

    branch_id: str
    portal_origin: str
    control_label: str
    prompt_guard_tokens: tuple[str, ...]
    option_set: tuple[str, ...]
    allowed_options: tuple[str, ...]
    chosen_option: str
    allows_redacted_slots: bool = False
    max_variable_tokens: int = 0
    source_card_id: str
    resolution_digest_sha256: str
    operator_id: str
    remembered: bool = True

    def guard_key(self) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
        """Return the canonical uniqueness key for this branch guard."""
        return (
            self.portal_origin.strip().casefold(),
            self.prompt_guard_tokens,
            self.option_set,
            self.allows_redacted_slots,
        )


class LivingBranchTable(BaseModel):
    """Collection of guarded modal/choice branches spliced across runs."""

    branches: list[GuardedBranch] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.branches)

    def add(self, branch: GuardedBranch) -> None:
        """Insert or update a branch keyed by its exact guard signature."""
        key = branch.guard_key()
        for idx, existing in enumerate(self.branches):
            if existing.guard_key() == key:
                self.branches[idx] = branch
                return
        self.branches.append(branch)

    def match(
        self,
        *,
        modal_ocr_words: Sequence[OcrWord],
        observed_options: Sequence[str],
        portal_origin: Optional[str] = None,
    ) -> Optional[GuardedBranch]:
        """Match a live modal against stored guarded branches."""
        return match_branch(
            self,
            modal_ocr_words=modal_ocr_words,
            observed_options=observed_options,
            portal_origin=portal_origin,
        )


def _ocr_words_to_tokens(words: Sequence[OcrWord]) -> list[OcrToken]:
    """Convert ``OcrWord`` entries into word-level ``OcrToken`` instances."""
    tokens: list[OcrToken] = []
    for raw_item in words:
        word = _normalize_ocr_word(raw_item)
        if word is None:
            continue
        raw_text = word.text
        x = float(word.x)
        y = float(word.y)
        w = (
            float(word.width)
            if word.width > 0
            else max(1.0, float(len(raw_text)) * 8.0)
        )
        h = float(word.height) if word.height > 0 else 16.0
        conf = float(word.confidence)
        line_len = len(raw_text)
        for match in re.finditer(r"\S+", raw_text):
            piece = match.group(0)
            parts = re.split(r"(?<=[a-z])(?=[A-Z])", piece)
            cursor = match.start()
            sub_spans: list[tuple[int, int, str]] = []
            for part in parts:
                end = cursor + len(part)
                sub_spans.append((cursor, end, part))
                cursor = end
            for s_idx, e_idx, sub_word in sub_spans:
                norm = normalize_token_text(sub_word)
                if not norm:
                    continue
                start_frac = s_idx / line_len
                end_frac = e_idx / line_len
                wx0 = x + w * start_frac
                wx1 = x + w * end_frac
                ww = max(1.0, wx1 - wx0)
                tokens.append(
                    OcrToken(
                        text=sub_word,
                        norm_text=norm,
                        cx=(wx0 + wx1) / 2.0,
                        cy=y + h / 2.0,
                        w=ww,
                        h=h,
                        confidence=conf,
                    )
                )
    return tokens


def _score_constellation_token_against_run2(
    ct: ConstellationToken,
    tx2: float,
    ty2: float,
    run2_outside: Sequence[OcrToken],
    run2_med_h: float,
    *,
    sigma_norm: float,
    max_err_norm: float,
) -> float:
    """Return the best spatial-text match score in [0, 1] for ``ct`` on Run 2."""
    ex = tx2 + ct.dx_norm * run2_med_h
    ey = ty2 + ct.dy_norm * run2_med_h
    best_score = 0.0

    if ct.is_immediate_label:
        max_dx_err = _IMMEDIATE_MAX_DX_ERR_NORM + 0.15 * abs(ct.dx_norm)
        max_dy_err = _IMMEDIATE_MAX_DY_ERR_NORM + 0.08 * abs(ct.dy_norm)
        eff_sigma_x = sigma_norm * (1.0 + 0.08 * abs(ct.dx_norm))
        eff_sigma_y = min(sigma_norm, 1.1) * (1.0 + 0.05 * abs(ct.dy_norm))
        for lt in run2_outside:
            text_sim = _immediate_text_similarity(ct.norm_text, lt.norm_text)
            if text_sim <= 0.0:
                continue
            dx_err = abs(lt.cx - ex) / run2_med_h
            dy_err = abs(lt.cy - ey) / run2_med_h
            if dx_err > max_dx_err or dy_err > max_dy_err:
                continue
            spatial = math.exp(
                -0.5 * ((dx_err / eff_sigma_x) ** 2 + (dy_err / eff_sigma_y) ** 2)
            )
            combined = text_sim * spatial
            if combined > best_score:
                best_score = combined
        return best_score

    dist_norm = math.hypot(ct.dx_norm, ct.dy_norm)
    eff_sigma = sigma_norm * (1.0 + 0.08 * dist_norm)
    eff_max_err = max_err_norm * (1.0 + 0.08 * dist_norm)
    for lt in run2_outside:
        text_sim = _token_text_similarity(ct.norm_text, lt.norm_text)
        if text_sim <= 0.0:
            continue
        err_norm = math.hypot(lt.cx - ex, lt.cy - ey) / run2_med_h
        if err_norm > eff_max_err:
            continue
        spatial = math.exp(-0.5 * (err_norm / eff_sigma) ** 2)
        combined = text_sim * spatial
        if combined > best_score:
            best_score = combined
    return best_score


def prune_coincidences_from_two_runs(
    constellation: TargetConstellation,
    run2_words: Sequence[OcrWord],
    *,
    target_region_run2: Region,
    target_point_run2: Optional[Point] = None,
    sigma_norm: float = DEFAULT_SIGMA_NORM,
    max_err_norm: float = DEFAULT_MAX_ERR_NORM,
    min_token_match_score: float = DEFAULT_PRUNE_MIN_MATCH_SCORE,
    min_surviving_tokens: int = 1,
) -> PruningReport:
    """Prune dynamic instance tokens by diffing Run 1's constellation against Run 2.

    Tokens present in Run 1 that vanished, moved away, or changed text in Run 2
    (such as patient table rows or session-specific IDs) are removed from the
    static constellation. Invariant immediate field labels and static UI tokens
    are preserved. If Run 2 produces too few surviving tokens (for example, due
    to empty OCR or misaligned screens), the original constellation is returned
    unchanged with ``pruning_applied=False``.
    """
    if min_surviving_tokens < 1:
        raise ValueError("min_surviving_tokens must be at least 1.")
    if not (0.0 < min_token_match_score <= 1.0):
        raise ValueError("min_token_match_score must lie in (0, 1].")

    run1_tokens = list(constellation.tokens)
    run2_tokens = _ocr_words_to_tokens(run2_words)

    if not run1_tokens:
        return PruningReport(
            constellation=constellation,
            kept_tokens=[],
            pruned_tokens=[],
            preserved_immediate_labels=[],
            run1_token_count=0,
            run2_token_count=len(run2_tokens),
            kept_count=0,
            pruned_count=0,
            pruning_applied=False,
            skipped_reason="empty_constellation",
        )

    if not run2_tokens:
        return PruningReport(
            constellation=constellation,
            kept_tokens=run1_tokens,
            pruned_tokens=[],
            preserved_immediate_labels=[
                ct.text for ct in run1_tokens if ct.is_immediate_label
            ],
            run1_token_count=len(run1_tokens),
            run2_token_count=0,
            kept_count=len(run1_tokens),
            pruned_count=0,
            pruning_applied=False,
            skipped_reason="empty_run2_ocr",
        )

    run2_med_h = _median_token_height(run2_tokens)
    tx2, ty2 = (
        (float(target_point_run2[0]), float(target_point_run2[1]))
        if target_point_run2 is not None
        else (
            target_region_run2[0] + target_region_run2[2] / 2.0,
            target_region_run2[1] + target_region_run2[3] / 2.0,
        )
    )
    run2_outside = [
        lt
        for lt in run2_tokens
        if not _point_in_region(lt.cx, lt.cy, target_region_run2)
    ]
    if not run2_outside:
        return PruningReport(
            constellation=constellation,
            kept_tokens=run1_tokens,
            pruned_tokens=[],
            preserved_immediate_labels=[
                ct.text for ct in run1_tokens if ct.is_immediate_label
            ],
            run1_token_count=len(run1_tokens),
            run2_token_count=len(run2_tokens),
            kept_count=len(run1_tokens),
            pruned_count=0,
            pruning_applied=False,
            skipped_reason="no_surrounding_run2_tokens",
        )

    kept: list[ConstellationToken] = []
    pruned: list[ConstellationToken] = []
    preserved_immediate: list[str] = []

    for ct in run1_tokens:
        score = _score_constellation_token_against_run2(
            ct,
            tx2,
            ty2,
            run2_outside,
            run2_med_h,
            sigma_norm=sigma_norm,
            max_err_norm=max_err_norm,
        )
        if score >= min_token_match_score:
            kept.append(ct)
            if ct.is_immediate_label:
                preserved_immediate.append(ct.text)
        else:
            pruned.append(ct)

    if len(kept) < min_surviving_tokens:
        return PruningReport(
            constellation=constellation,
            kept_tokens=run1_tokens,
            pruned_tokens=[],
            preserved_immediate_labels=[
                ct.text for ct in run1_tokens if ct.is_immediate_label
            ],
            run1_token_count=len(run1_tokens),
            run2_token_count=len(run2_tokens),
            kept_count=len(run1_tokens),
            pruned_count=0,
            pruning_applied=False,
            skipped_reason="insufficient_surviving_tokens",
        )

    cleaned = constellation.model_copy(update={"tokens": kept})
    return PruningReport(
        constellation=cleaned,
        kept_tokens=kept,
        pruned_tokens=pruned,
        preserved_immediate_labels=preserved_immediate,
        run1_token_count=len(run1_tokens),
        run2_token_count=len(run2_tokens),
        kept_count=len(kept),
        pruned_count=len(pruned),
        pruning_applied=True,
        skipped_reason=None,
    )


def _normalize_option(text: str) -> str:
    return " ".join(text.split()).casefold()


def _tokenize_structural_words(text: str) -> tuple[str, ...]:
    """Split text into normalized word tokens, stripping redacted markers."""
    without_redacted = re.sub(re.escape(REDACTED), " ", text, flags=re.IGNORECASE)
    tokens: list[str] = []
    for match in re.finditer(r"\S+", without_redacted):
        piece = match.group(0)
        for part in re.split(r"(?<=[a-z])(?=[A-Z])", piece):
            norm = normalize_token_text(part)
            if norm:
                tokens.append(norm)
    return tuple(tokens)


def _extract_prompt_text_from_ocr(
    modal_ocr_words: Sequence[OcrWord],
    option_labels: Sequence[str],
) -> str:
    """Extract modal prompt text from OCR words while removing option button text."""
    normalized_words = [
        word
        for item in modal_ocr_words
        if (word := _normalize_ocr_word(item)) is not None
    ]
    if not normalized_words:
        return ""

    option_norm_set = {
        _normalize_option(opt) for opt in option_labels if _normalize_option(opt)
    }
    row_segments = _group_ocr_rows(normalized_words)
    prompt_segments: list[str] = []
    for row in row_segments:
        kept_in_row: list[str] = []
        for seg in row:
            clean_seg = " ".join(seg.split()).strip()
            if not clean_seg:
                continue
            if _normalize_option(clean_seg) in option_norm_set:
                continue
            kept_in_row.append(clean_seg)
        if kept_in_row:
            prompt_segments.append(" ".join(kept_in_row))

    prompt_text = " ".join(prompt_segments).strip()
    if not prompt_text or not option_labels:
        return prompt_text

    # Handle geometry-less word-by-word OCR input where multi-word options were
    # split across separate segments: strip trailing token runs matching options.
    prompt_tokens = list(_tokenize_structural_words(prompt_text))
    option_token_seqs = [
        list(_tokenize_structural_words(opt)) for opt in option_labels if opt.strip()
    ]
    option_token_seqs = [seq for seq in option_token_seqs if seq]
    changed = True
    while changed and prompt_tokens:
        changed = False
        for seq in option_token_seqs:
            seq_len = len(seq)
            if seq_len <= len(prompt_tokens) and prompt_tokens[-seq_len:] == seq:
                prompt_tokens = prompt_tokens[:-seq_len]
                changed = True
                break
    return " ".join(prompt_tokens)


def _intersect_ordered_tokens(
    card_tokens: Sequence[str],
    modal_tokens: Sequence[str],
) -> tuple[str, ...]:
    """Keep card prompt tokens that appear in the modal OCR prompt in order."""
    modal_counts: dict[str, int] = {}
    for tok in modal_tokens:
        modal_counts[tok] = modal_counts.get(tok, 0) + 1

    kept: list[str] = []
    for tok in card_tokens:
        remaining = modal_counts.get(tok, 0)
        if remaining > 0:
            kept.append(tok)
            modal_counts[tok] = remaining - 1
    return tuple(kept)


def _assert_branch_zero_phi(
    *,
    portal_origin: str,
    control_label: str,
    prompt_guard_tokens: Sequence[str],
    allowed_options: Sequence[str],
    chosen_option: str,
) -> None:
    """Verify no raw PHI or secret material enters a stored GuardedBranch."""
    guard_phrase = " ".join(prompt_guard_tokens)
    for label, text in (
        ("portal_origin", portal_origin),
        ("control_label", control_label),
        ("prompt_guard_tokens", guard_phrase),
        ("chosen_option", chosen_option),
    ):
        assert_zero_phi_value(text, label)
    for idx, opt in enumerate(allowed_options):
        assert_zero_phi_value(opt, f"allowed_options[{idx}]")

    payload = {
        "portal_origin": portal_origin,
        "control_label": control_label,
        "prompt_guard_tokens": list(prompt_guard_tokens),
        "allowed_options": list(allowed_options),
        "chosen_option": chosen_option,
    }
    unredacted = find_unredacted(payload)
    if unredacted:
        reasons = ", ".join(r.reason for r in unredacted[:3])
        raise ZeroPhiViolationError(
            f"Refused GuardedBranch: unredacted credential material detected ({reasons})."
        )


def splice_prepare_branch(
    table: LivingBranchTable,
    *,
    card: Mapping[str, Any],
    resolution_payload: Mapping[str, Any],
    modal_ocr_words: Sequence[OcrWord],
    remember_rule: bool = True,
) -> GuardedBranch:
    """Compile a signed PrepareResolution into a guarded conditional branch.

    Verifies the resolution SHA-256 digest via
    :func:`openadapt_flow.cloud_prepare_bridge.verify_resolution_digest`,
    verifies zero-PHI constraints on both the card and extracted branch guard,
    derives invariant structural prompt tokens plus the exact option set, and
    inserts the resulting :class:`GuardedBranch` into ``table`` when
    ``remember_rule`` is ``True``.
    """
    assert_prepare_card_zero_phi(card)

    verified_digest = verify_resolution_digest(resolution_payload)
    if verified_digest is None:
        raise ValueError(
            "Refused to splice unsigned PrepareResolution: "
            "payload_sha256 or digest_sha256 is required."
        )

    halt = pixel_prepare_card_to_form_halt(card)
    card_id = halt.halt_id

    nested_raw = resolution_payload.get("resolution")
    nested_resolution: Mapping[str, Any] = (
        nested_raw if isinstance(nested_raw, Mapping) else {}
    )
    resolution_card_id = str(
        resolution_payload.get("card_id") or nested_resolution.get("card_id") or ""
    ).strip()
    if resolution_card_id and resolution_card_id != card_id:
        raise ValueError(
            f"PrepareResolution card_id {resolution_card_id!r} does not match "
            f"PrepareCard card_id {card_id!r}."
        )

    decision = prepare_resolution_to_human_input(
        halt,
        resolution_payload,
        remember_as_candidate_rule=remember_rule,
    )
    if (
        decision.action != HumanDecisionAction.PROVIDE_VALUE
        or not decision.provided_value
    ):
        raise ValueError(
            "Cannot splice a rejected or empty PrepareResolution into LivingBranchTable."
        )

    slots = card.get("unresolved_slots")
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], Mapping):
        raise ValueError("PrepareCard must contain at least one unresolved slot.")
    slot = slots[0]

    raw_allowed = slot.get("allowed_enum_options")
    if not isinstance(raw_allowed, list) or not raw_allowed:
        raise ValueError(
            "Cannot splice branch: PrepareCard slot must declare non-empty allowed_enum_options."
        )
    allowed_options = tuple(
        " ".join(str(opt).split()).strip() for opt in raw_allowed if str(opt).strip()
    )
    if not allowed_options:
        raise ValueError(
            "Cannot splice branch: PrepareCard allowed_enum_options are empty."
        )

    norm_to_canonical: dict[str, str] = {}
    for opt in allowed_options:
        norm = _normalize_option(opt)
        if norm in norm_to_canonical:
            raise ValueError(
                f"Cannot splice branch with ambiguous duplicate option label {opt!r}."
            )
        norm_to_canonical[norm] = opt
    option_set = tuple(sorted(norm_to_canonical.keys()))

    chosen_norm = _normalize_option(decision.provided_value)
    if chosen_norm not in norm_to_canonical:
        raise ValueError(
            f"Approved choice {decision.provided_value!r} is not in "
            f"PrepareCard allowed_enum_options {list(allowed_options)!r}."
        )
    chosen_option = norm_to_canonical[chosen_norm]

    card_reason = str(slot.get("reason") or "").strip()
    allows_redacted_slots = REDACTED.casefold() in card_reason.casefold()

    card_prompt_tokens = _tokenize_structural_words(card_reason)
    modal_prompt_text = _extract_prompt_text_from_ocr(modal_ocr_words, allowed_options)
    modal_prompt_tokens = _tokenize_structural_words(modal_prompt_text)

    shared_tokens = _intersect_ordered_tokens(card_prompt_tokens, modal_prompt_tokens)
    if allows_redacted_slots:
        content_tokens = tuple(
            tok for tok in shared_tokens if tok not in _GLUE_STOPWORDS
        )
        guard_tokens = content_tokens if len(content_tokens) >= 2 else shared_tokens
    else:
        guard_tokens = shared_tokens

    if not guard_tokens:
        raise ValueError(
            "Cannot splice branch: no invariant structural prompt guard tokens matched "
            "between PrepareCard reason and modal_ocr_words."
        )

    portal_origin = str(card.get("portal_origin") or "").strip()
    if not portal_origin:
        raise ValueError("PrepareCard must contain a non-empty portal_origin.")
    control_label = str(slot.get("control_label") or halt.field_label or "").strip()

    _assert_branch_zero_phi(
        portal_origin=portal_origin,
        control_label=control_label,
        prompt_guard_tokens=guard_tokens,
        allowed_options=allowed_options,
        chosen_option=chosen_option,
    )

    extra_in_modal = max(0, len(modal_prompt_tokens) - len(guard_tokens))
    max_variable_tokens = max(8, extra_in_modal + 4) if allows_redacted_slots else 0

    branch_seed = (
        f"{portal_origin.casefold()}|{' '.join(guard_tokens)}|"
        f"{'|'.join(option_set)}|{int(allows_redacted_slots)}"
    )
    branch_id = f"branch_{hashlib.sha256(branch_seed.encode('utf-8')).hexdigest()[:12]}"

    branch = GuardedBranch(
        branch_id=branch_id,
        portal_origin=portal_origin,
        control_label=control_label,
        prompt_guard_tokens=guard_tokens,
        option_set=option_set,
        allowed_options=allowed_options,
        chosen_option=chosen_option,
        allows_redacted_slots=allows_redacted_slots,
        max_variable_tokens=max_variable_tokens,
        source_card_id=card_id,
        resolution_digest_sha256=verified_digest,
        operator_id=decision.actor_id,
        remembered=remember_rule,
    )
    if remember_rule:
        table.add(branch)
    return branch


def _match_subsequence_and_extras(
    guard_tokens: Sequence[str],
    observed_tokens: Sequence[str],
) -> Optional[list[str]]:
    """Return non-guard tokens when ``guard_tokens`` is an ordered subsequence."""
    guard_idx = 0
    guard_len = len(guard_tokens)
    extras: list[str] = []
    for tok in observed_tokens:
        if guard_idx < guard_len and tok == guard_tokens[guard_idx]:
            guard_idx += 1
        else:
            extras.append(tok)
    if guard_idx != guard_len:
        return None
    return extras


def _prompt_guard_matches(
    branch: GuardedBranch,
    observed_prompt_tokens: tuple[str, ...],
) -> bool:
    """Check whether observed modal prompt tokens satisfy ``branch``'s guard."""
    if not branch.allows_redacted_slots:
        return observed_prompt_tokens == branch.prompt_guard_tokens

    extras = _match_subsequence_and_extras(
        branch.prompt_guard_tokens, observed_prompt_tokens
    )
    if extras is None:
        return False

    non_glue_extras = [tok for tok in extras if tok not in _GLUE_STOPWORDS]
    if len(non_glue_extras) > branch.max_variable_tokens:
        return False

    guard_set = set(branch.prompt_guard_tokens)
    for tok in non_glue_extras:
        if tok in _HAZARD_CONTRADICTION_TOKENS and tok not in guard_set:
            return False
    return True


def match_branch(
    table: LivingBranchTable,
    *,
    modal_ocr_words: Sequence[OcrWord],
    observed_options: Sequence[str],
    portal_origin: Optional[str] = None,
) -> Optional[GuardedBranch]:
    """Match a live modal strictly by structural prompt guard and exact option set.

    Returns the single matching :class:`GuardedBranch` only when BOTH the
    structural prompt guard tokens match AND the exact normalized option set
    matches with zero ambiguity. Returns ``None`` (safe halt to ``PrepareCard``)
    when no branch matches, options differ, prompt wording changes, or more than
    one branch matches.
    """
    if not table.branches:
        return None

    clean_options = [
        " ".join(str(opt).split()).strip()
        for opt in observed_options
        if str(opt).strip()
    ]
    if not clean_options:
        return None

    norm_options = [_normalize_option(opt) for opt in clean_options]
    if len(set(norm_options)) != len(norm_options):
        return None
    observed_option_set = tuple(sorted(set(norm_options)))

    prompt_text = _extract_prompt_text_from_ocr(modal_ocr_words, clean_options)
    observed_prompt_tokens = _tokenize_structural_words(prompt_text)
    if not observed_prompt_tokens:
        return None

    origin_cf = portal_origin.strip().casefold() if portal_origin else None

    matches: list[GuardedBranch] = []
    for branch in table.branches:
        if (
            origin_cf is not None
            and branch.portal_origin.strip().casefold() != origin_cf
        ):
            continue
        if branch.option_set != observed_option_set:
            continue
        if _prompt_guard_matches(branch, observed_prompt_tokens):
            matches.append(branch)

    if len(matches) != 1:
        return None
    return matches[0]


__all__ = [
    "DEFAULT_PRUNE_MIN_MATCH_SCORE",
    "GuardedBranch",
    "LivingBranchTable",
    "PruningReport",
    "match_branch",
    "prune_coincidences_from_two_runs",
    "splice_prepare_branch",
]
