# Grounding Cascade Design v3

## Problem

Find the best-supported candidate for the demonstrated target on the current screen using pixels, text, context, and expected transition. Then verify the click produced the expected state change. Retry or escalate on failure.

The current DemoExecutor (demo_executor.py) routes keyboard/type to Tier 1 (deterministic) and clicks to Tier 2 (single VLM call). There is no candidate generation, no pre-click verification, no post-click verification, and no structured transition checking. This design replaces the single-shot VLM grounding call with a multi-source candidate pipeline.

## Empirical context

| Grounder | Task | Score | Source |
|----------|------|-------|--------|
| gpt-4.1-mini | notepad-hello (1 click) | 1.00 | Flywheel validation (2026-03-28) |
| gpt-4.1-mini | clear-browsing (1 critical click) | 0.25 | Flywheel validation (2026-03-28) |
| UI-Venus-1.5-8B | client tasks | 1.00 | Client engagement |
| GPT-5.4 + UI-Venus | client tasks | 1.00 | Client engagement |

The bottleneck is click grounding accuracy on complex UIs (Chrome settings). Keyboard actions already work perfectly (4/5 notepad steps are Tier 1). This design targets the click path only.

---

## Data model

### GroundingTarget (stored per click step in demo)

```python
@dataclass
class GroundingTarget:
    # What the target is
    description: str                          # "Clear browsing data button"
    target_type: str                          # "button" | "icon" | "checkbox" | "tab" | "text_field"

    # Visual evidence (from demo screenshot)
    crop_path: str                            # relative path to PNG crop of target element
    crop_bbox: tuple[int, int, int, int]      # [x1, y1, x2, y2] in demo frame
    click_offset: tuple[int, int]             # click point relative to crop top-left

    # Text context
    nearby_text: list[str]                    # OCR text within ~100px of target
    window_title: str                         # app/window title at demo time
    surrounding_labels: list[str]             # sibling labels/landmarks

    # Transition evidence (file references, not base64)
    screenshot_before_path: str               # path to full screenshot before this step
    screenshot_after_path: str                # path to full screenshot after this step

    # Structured transition expectations (machine-checkable)
    disappearance_text: list[str]             # landmark text that should vanish after click
    appearance_text: list[str]                # landmark text that should appear after click
    window_title_change: str | None           # expected new window title (None = no change)
    region_changed: tuple[int, int, int, int] | None  # region that should differ
    modal_toggled: bool | None                # True = modal appears, False = modal disappears

    # Human-readable transition description (kept for logging/debugging)
    expected_change: str                      # "Confirmation dialog appears"
```

**Storage model**: All image data uses file references (relative paths to PNGs on disk). No base64 in the data model. Crops and screenshots are stored alongside the demo JSON in the demo directory. Loading into memory for CLIP/pHash/VLM is the caller's responsibility.

**VLM enrichment caveat**: For imported openadapt-capture recordings, a VLM generates GroundingTarget fields (description, nearby_text, surrounding_labels, structured transition expectations). These fields need human review for ambiguous elements -- a mislabeled button description or wrong disappearance_text silently poisons all future replays of that demo. The enrichment pipeline should flag low-confidence fields for review rather than silently accepting them.

### GroundingCandidate (produced by each tier during grounding)

```python
@dataclass
class GroundingCandidate:
    source: str                               # "a11y" | "ocr" | "clip" | "ui_venus" | "gpt54"
    point: tuple[int, int]                    # (x, y) pixel coordinates to click
    bbox: tuple[int, int, int, int] | None    # [x1, y1, x2, y2] if available
    local_score: float                        # tier-local confidence (not comparable across tiers)
    matched_text: str | None                  # text that matched (for text-based tiers)
    reasoning: str | None                     # VLM reasoning (for VLM-based tiers)
    spatial_score: float | None               # consistency with demo position (set by selection)
    visual_verify_score: float | None         # crop resemblance to target (set by verification)
    accepted: bool = False                    # set True by selection logic
```

All tiers normalize their output into `GroundingCandidate`. Selection logic operates on a list of candidates, not on tier-specific return types.

---

## Full cascade flow

```
click step with GroundingTarget
    |
    1. STATE CHECK: are preconditions met?
    |   - Window title matches GroundingTarget.window_title?
    |   - Landmark text (surrounding_labels) visible via OCR?
    |   - If not -> state recovery before grounding
    |
    2. CANDIDATE GENERATION (cheapest first, collect all)
    |   +-- Tier 1.5a: Text anchoring (OCR + a11y)
    |   +-- Tier 1.5b: Visual retrieval (CLIP with region proposals)
    |   +-- Tier 2: VLM grounding (UI-Venus or GPT-5.4)
    |
    3. NORMALIZE into list[GroundingCandidate]
    |
    4. DETERMINISTIC ACCEPTANCE RULES
    |   - a11y exact match on element name+role -> accept immediately
    |   - OCR exact match + layout consistent with demo -> accept immediately
    |   - (These are HIGH-PRECISION signals; early stopping is correct here)
    |
    5. VERIFICATION (for non-deterministic candidates)
    |   - Crop region around candidate point from current screenshot
    |   - Compare crop to GroundingTarget.crop_path via CLIP similarity
    |   - Set visual_verify_score on each candidate
    |   - Reject candidates below resemblance threshold
    |   - Select best remaining candidate
    |
    6. CLICK the selected candidate
    |
    7. POST-CLICK TRANSITION VERIFICATION
        - Take new screenshot
        - Check structured expectations:
          - disappearance_text items no longer present (OCR)
          - appearance_text items now present (OCR)
          - window_title_change matches (if specified)
          - region_changed shows pixel difference (if specified)
          - modal_toggled matches (if specified)
        - If ALL checked expectations pass -> success, continue
        - If ANY expectation fails -> retry with next-best candidate
        - If no candidates remain -> escalate to Tier 3 (planner recovery)
```

### Early stopping rule

The principle is not "never stop early" and not "always stop early." It is:

- **Stop early on HIGH-PRECISION signals.** An a11y exact match (name + role) is deterministic -- accept it, skip verification, click. An OCR exact text match where the matched element's position is spatially consistent with the demo layout is high-precision -- accept it.
- **Continue gathering evidence on UNCERTAIN signals.** A fuzzy OCR match (0.6-0.8 confidence) or a CLIP match without a large gap to the second-best candidate are uncertain. Gather more candidates from other tiers before deciding.
- **Always verify on MODERATE-CONFIDENCE signals.** VLM grounding results, CLIP matches with moderate gaps, OCR matches on common strings ("OK", "Cancel") -- crop-verify before clicking.

---

## Tier details

### Pre-step: State narrowing

Runs before candidate generation. Cheaper to detect "wrong screen" than to ground on it.

1. OCR the current screenshot for text content.
2. Check `GroundingTarget.window_title` against detected title bar text.
3. Check `GroundingTarget.surrounding_labels` -- are landmark texts present?
4. If preconditions fail: state recovery (open app, navigate, scroll) via Tier 3 planner. Do not attempt grounding on the wrong screen.

### Tier 1.5a: Text anchoring

- Run OCR on current screenshot.
- If a11y tree is available (Windows UIAutomation via WAA): query elements by name and role.
- Match `GroundingTarget.description` and `nearby_text` against detected text.
- **a11y exact match**: `GroundingCandidate(source="a11y", local_score=1.0)`. Deterministic accept.
- **OCR exact match + layout consistent**: `GroundingCandidate(source="ocr", local_score=0.95)`. Accept if position is in the expected quadrant relative to demo.
- **OCR fuzzy match**: `GroundingCandidate(source="ocr", local_score=0.6-0.8)`. Add to candidates, do not accept alone.

### Tier 1.5b: Visual retrieval (CLIP)

Compare `GroundingTarget.crop_path` (loaded as image) against candidate regions in the current screenshot.

**Region proposals** (where candidates come from -- NOT sliding windows):

1. **OCR bounding boxes**: Every detected text region is a proposal. Covers buttons with text labels, menu items, links.
2. **A11y tree element bounds**: When available, every a11y element with a bounding rect is a proposal. Covers unlabeled buttons, icons with a11y names.
3. **Detected UI components**: OmniParser-style icon/widget detection for elements that have no text and no a11y. Covers toolbar icons, graphical buttons.
4. **Saliency/attention maps**: If a VLM is already being called (Tier 2), extract attention weights over the image to identify salient regions.
5. **Grid sampling at multiple scales**: Last resort only. 3x3 grid at full resolution, 5x5 at half, 7x7 at quarter. Expensive and noisy -- only when proposals 1-4 yield fewer than 3 candidates.

For each proposal region:
- Embed with CLIP (ViT-L/14 or SigLIP).
- Compute cosine similarity to the GroundingTarget crop embedding.
- Rank by similarity. Use **relative gap** to second-best, not raw score.
- Return top-K as `GroundingCandidate(source="clip", local_score=similarity)`.

**pHash** is a pre-filter only: use to quickly discard proposals with very different structure before running CLIP. Never use pHash as a final decision signal -- it is brittle to scaling, DPI changes, hover states, and antialiasing.

### Tier 2a: UI-Venus (primary VLM grounder)

- Send screenshot + `GroundingTarget.description` to UI-Venus HTTP endpoint.
- Parse `[x1, y1, x2, y2]` bbox response.
- Return `GroundingCandidate(source="ui_venus", point=bbox_center, bbox=bbox, local_score=1.0)`.
- Availability check with 3 retries + exponential backoff (not hard fallback on first failure).
- Requires GPU serving (~$0.50/hr via sglang/vLLM).

### Tier 2b: GPT-5.4 (API fallback)

- Send screenshot + `GroundingTarget.description` to GPT-5.4 API.
- Parse normalized (x, y) coordinates from response.
- Return `GroundingCandidate(source="gpt54", point=pixel_coords, local_score=1.0)`.
- Used when UI-Venus endpoint is unavailable after retries.
- Higher cost (~$0.01/call) but no GPU infrastructure needed.

### Tier 3: Planner recovery

Not a grounding tier. Triggered when:
- State check fails and recovery is needed.
- Post-click verification fails on all candidates.
- No candidate meets minimum acceptance criteria.

The planner VLM reasons about unexpected state and produces recovery actions (navigate, scroll, open app). After recovery, re-enter the cascade from the state check.

---

## Candidate selection logic

```python
def select_and_act(candidates: list[GroundingCandidate], target: GroundingTarget) -> GroundingCandidate | None:
    """Select best candidate, verify, return accepted candidate or None."""

    # Step 1: Deterministic acceptance (high-precision, skip verification)
    for c in candidates:
        if c.source == "a11y" and c.local_score == 1.0:
            c.accepted = True
            return c
        if c.source == "ocr" and c.local_score >= 0.95:
            c.spatial_score = compute_spatial_consistency(c.point, target.crop_bbox)
            if c.spatial_score > 0.7:
                c.accepted = True
                return c

    # Step 2: Score spatial consistency for remaining candidates
    for c in candidates:
        if c.spatial_score is None:
            c.spatial_score = compute_spatial_consistency(c.point, target.crop_bbox)

    # Step 3: Visual verification (crop resemblance check)
    for c in sorted(candidates, key=lambda c: c.local_score, reverse=True):
        crop = extract_crop(current_screenshot, c.point, target.crop_bbox)
        c.visual_verify_score = clip_similarity(crop, load_image(target.crop_path))
        if c.visual_verify_score > 0.75:
            c.accepted = True
            return c

    # Step 4: If no candidate passes verification, return best VLM candidate
    vlm_candidates = [c for c in candidates if c.source in ("ui_venus", "gpt54")]
    if vlm_candidates:
        best = max(vlm_candidates, key=lambda c: c.spatial_score or 0)
        best.accepted = True
        return best

    return None  # escalate to Tier 3
```

### Post-click transition verification

```python
def verify_transition(target: GroundingTarget, screenshot_after: bytes) -> bool:
    """Check structured transition expectations against post-click screenshot."""
    ocr_texts = run_ocr(screenshot_after)

    if target.disappearance_text:
        for text in target.disappearance_text:
            if text_present(text, ocr_texts):
                return False  # text should have vanished but didn't

    if target.appearance_text:
        for text in target.appearance_text:
            if not text_present(text, ocr_texts):
                return False  # text should have appeared but didn't

    if target.window_title_change is not None:
        current_title = detect_window_title(screenshot_after)
        if target.window_title_change not in current_title:
            return False

    if target.region_changed is not None:
        if not region_differs(target.screenshot_before_path, screenshot_after, target.region_changed):
            return False

    if target.modal_toggled is not None:
        modal_visible = detect_modal(screenshot_after)
        if target.modal_toggled != modal_visible:
            return False

    return True
```

---

## Phase ordering

| Phase | Work | Time | What it unblocks |
|-------|------|------|------------------|
| 1 | GPT-5.4 as grounder | 1 hr | clear-browsing hits 1.0 today |
| 2 | UI-Venus as primary grounder | 2 hrs | cost optimization (10x cheaper per click) |
| 3 | GroundingTarget data model | 1 day | rich target representation for all downstream tiers |
| 4 | State narrowing | 1 day | catch wrong-screen errors before grounding (moved up from v2 Phase 7) |
| 5 | Text anchoring -- Tier 1.5a | 1 day | zero-cost grounding for text-labeled elements |
| 6 | Post-click verification | 1 day | catch bad clicks before they propagate |
| 7 | Visual retrieval -- Tier 1.5b with CLIP | 2 days | handle icon-only and visually-distinct elements |

**Why state narrowing moved up (Phase 4 instead of Phase 7)**: Grounding the wrong element on the wrong screen is the most expensive failure mode -- it wastes a VLM call, clicks the wrong thing, corrupts state, and triggers recovery. Detecting "wrong screen" via OCR landmark check is cheap (<100ms) and prevents the entire cascade from running on garbage input. It should be in place before adding more grounding tiers.

### Phase 1: GPT-5.4 as grounder (1 hour)

Change `grounder_model="gpt-4.1-mini"` to `grounder_model="gpt-5.4"` in DemoExecutor defaults. Re-run clear-browsing. If it hits 1.0, the product demo works today. No architecture changes.

### Phase 2: UI-Venus as primary grounder (2 hours)

Boot GPU, serve UI-Venus via sglang, point DemoExecutor at `grounder_endpoint`. Verify 1.0 on clear-browsing. Replaces $0.01/click (GPT-5.4) with ~$0.002/click (UI-Venus local).

### Phase 3: GroundingTarget data model (1 day)

- Implement `GroundingTarget` and `GroundingCandidate` dataclasses.
- Update demo recording to capture crops, nearby text, surrounding labels, window title.
- Add structured transition fields (disappearance_text, appearance_text, etc.).
- VLM enrichment step for imported openadapt-capture recordings with confidence flags.
- Update demo JSON schema. All image data stored as file references.

### Phase 4: State narrowing (1 day)

- OCR-based window title check before grounding.
- Landmark text verification (surrounding_labels present on screen).
- State recovery routing to Tier 3 planner when preconditions fail.

### Phase 5: Text anchoring -- Tier 1.5a (1 day)

- OCR-based candidate generation against GroundingTarget.description and nearby_text.
- A11y tree integration where available (Windows UIAutomation via WAA).
- Deterministic acceptance for a11y exact match and OCR exact + layout consistent.
- Fuzzy matching for partial candidates.

### Phase 6: Post-click verification (1 day)

- Structured transition verification using disappearance_text, appearance_text, window_title_change, region_changed, modal_toggled.
- Retry logic: try next-best candidate on verification failure.
- Escalate to Tier 3 when all candidates exhausted.

### Phase 7: Visual retrieval -- Tier 1.5b with CLIP (2 days)

- Region proposal generation (OCR boxes, a11y bounds, UI component detection, grid fallback).
- CLIP embedding and cosine similarity ranking.
- pHash as pre-filter only.
- Relative gap scoring for acceptance decisions.

### Deferred

- **RegionFocus zoom**: Crop predicted region at 2x, re-run grounder. Only if Tier 2 empirically fails on small targets.
- **App-specific plugins**: Chrome settings navigator, Windows common dialogs, Office ribbon navigation. Generality is overrated if one plugin gives 10x reliability on a common flow. When a specific app accounts for >30% of grounding failures, write a plugin with hardcoded navigation paths rather than trying to make the general cascade handle it. These plugins bypass the cascade entirely for known UI patterns and fall back to the cascade for unknown elements. This is a future extension, not part of the initial build.

---

## Eval plan

### Failure taxonomy

| Failure type | Description | Detected by |
|-------------|-------------|-------------|
| **Wrong screen** | Grounding attempted on wrong app/page/dialog | State check (Phase 4) |
| **Correct screen, wrong element** | Right screen but clicked the wrong button | Post-click verification (Phase 6) |
| **Correct screen, element not found** | Right screen but no candidate generated | Candidate recall metric |
| **Correct element, wrong point** | Right element identified but click coordinates off | Pre-click crop verification |
| **Correct click, no state change** | Clicked correctly but UI didn't respond (timing, focus) | Post-click verification |
| **False accept** | Verification incorrectly accepted a bad click | Manual audit of verification results |
| **False reject** | Verification incorrectly rejected a good click | Manual audit of verification results |

### Per-tier metrics

**State check (Phase 4)**
- Recall: Of steps where the agent is on the wrong screen, what fraction did the state check catch?
- Precision: Of steps where the state check flagged "wrong screen," what fraction were actually wrong?
- Target: >95% recall, >90% precision.

**Candidate recall@k (Phases 5, 7)**
- For each click step, is the correct element among the top-k candidates from all tiers combined?
- Measure at k=1, k=3, k=5.
- Target: recall@3 > 90% on our task set.

**Selection accuracy (Phase 5, 7)**
- Of steps where the correct element is in the candidate set, did the selection logic pick it?
- Target: >95% when correct element is present.

**Pre-click verification (Phase 7)**
- False accept rate: fraction of wrong candidates that passed crop resemblance check.
- Target: <5% false accept rate.

**Post-click verification (Phase 6)**
- False accept rate: fraction of bad clicks where verification said "transition OK."
- False reject rate: fraction of good clicks where verification said "transition failed."
- Target: <5% false accept, <10% false reject.

**End-to-end**
- Task completion rate as a function of number of click steps.
- 1-click tasks (notepad-hello): target 1.0.
- 2-3 click tasks (clear-browsing): target 0.9+.
- 5+ click tasks: target 0.8+.
- Measured per grounder configuration (gpt-4.1-mini, GPT-5.4, UI-Venus, full cascade).

### Eval procedure

1. Curate a set of 20+ demo steps covering: text buttons, icon-only buttons, checkboxes, dropdowns, tabs, text fields, and ambiguous elements (multiple "OK" buttons).
2. For each step, manually annotate the ground-truth click point and expected transition.
3. Run each tier independently and measure candidate recall.
4. Run the full cascade and measure end-to-end selection accuracy.
5. Run with post-click verification and measure false accept/reject rates.
6. Report per-tier and end-to-end metrics after each phase lands.

---

## Cost analysis

For a 10-step demo with 4 click actions:

| Architecture | VLM calls | Cost/episode | Latency |
|-------------|-----------|-------------|---------|
| Current (gpt-4.1-mini, every click) | 4 | $0.006 | 12s |
| Phase 1 (GPT-5.4, every click) | 4 | $0.04 | 12s |
| Phase 2 (UI-Venus, every click) | 4 | $0.008 | 8s |
| Full cascade (Tier 1.5 + UI-Venus fallback) | 0-2 | $0-0.004 | 1-5s |

Full cascade is 10-20x cheaper than GPT-5.4 because text anchoring and CLIP matching handle most clicks without a VLM call.

---

## Design principles

1. **Target representation is the foundation.** Rich `GroundingTarget` with crops, text context, and structured transitions enables every tier. Without it, each tier infers from a weak description string.
2. **Generate candidates, then select.** Each tier proposes `GroundingCandidate` objects. Selection is a separate, explicit step that operates on a normalized list.
3. **Verify transitions, not just coordinates.** Ground, act, verify. Bad clicks must not propagate through multi-step demos.
4. **Confidence is tier-local.** Do not compare OCR confidence with CLIP similarity or VLM confidence. Use tier-specific acceptance criteria and deterministic rules.
5. **Early stop on certainty, gather evidence on uncertainty.** A11y exact match = stop. Fuzzy OCR match = keep looking. This is not "don't stop early" -- it is "stop early only when you are right."
6. **State narrowing before grounding.** "Are we on the right screen?" is cheaper and more important than "Where is the button?" Check preconditions first.
7. **Cheapest first.** OCR/a11y ($0, <100ms) before CLIP ($0, <500ms) before UI-Venus ($0.002, 2s) before GPT-5.4 ($0.01, 3s).
8. **Demo screenshots are gold.** The demo shows what the button looks like and what happens after clicking it. Use both (crop matching + transition verification).
9. **Empirical over benchmarks.** UI-Venus 1.0 on our tasks beats UI-TARS 94.2% on ScreenSpot. Evaluate on OUR task set, not published benchmarks.
10. **GPT-5.4 is the unblocker, not the architecture.** Phase 1 gets us to 1.0 today. Phases 3-7 get us to production reliability and cost efficiency.
11. **File references for all image data.** Screenshots and crops stored as PNGs on disk, referenced by path. No base64 in the data model. Consistent, inspectable, git-friendly.
12. **Plugins for reliability, not generality.** When a specific app causes >30% of failures, write a targeted plugin rather than making the general cascade more complex. One Chrome settings plugin is worth more than a 5% improvement to CLIP matching.
