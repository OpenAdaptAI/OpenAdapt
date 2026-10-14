# Grounding Cascade Design v2

## The actual problem

Not "find where to click given a description." Instead:

**Find the best-supported candidate for the demonstrated target on the current screen using pixels, text, context, and expected transition. Then verify.**

The system should generate candidates, rank them, act, and verify — not just ground and click.

## GroundingTarget: first-class artifact

The demo stores a `GroundingTarget` per click step, not just a description string:

```python
@dataclass
class GroundingTarget:
    # What the target is
    description: str              # "Clear browsing data button"
    target_type: str              # "button" | "icon" | "checkbox" | "tab" | "text_field"

    # Visual evidence (from demo screenshot at this step)
    crop: bytes                   # PNG crop of the target element
    crop_bbox: tuple[int,int,int,int]  # [x1,y1,x2,y2] in demo frame
    click_offset: tuple[int,int]  # click point relative to crop top-left

    # Text context
    nearby_text: list[str]        # OCR text within ~100px of target
    window_title: str             # app/window title at demo time
    surrounding_labels: list[str] # sibling labels/landmarks

    # Transition evidence
    screenshot_before: bytes      # full screenshot before this step
    screenshot_after: bytes       # full screenshot after this step
    expected_change: str          # "dialog closes" | "new page loads" | "text appears"
```

This is the key unlock. Every downstream tier has richer signal to work with.

## Architecture: generate candidates, then verify

```
click step with GroundingTarget
    │
    ├─ 1. State check: are preconditions met?
    │     - Window title matches?
    │     - Expected page/dialog visible? (OCR landmarks)
    │     - If not → state recovery before grounding
    │
    ├─ 2. Candidate generation (cheapest first, collect ALL candidates)
    │     ├─ Tier 1.5a: Text anchoring (OCR + a11y if available)
    │     ├─ Tier 1.5b: Visual retrieval (CLIP crop match)
    │     └─ Tier 2: VLM grounding (UI-Venus or GPT-5.4)
    │
    ├─ 3. Candidate selection
    │     - Rank by tier-specific confidence (calibrated per method)
    │     - Spatial consistency check (is candidate in plausible region?)
    │     - Verify crop around candidate resembles GroundingTarget.crop
    │
    ├─ 4. Act (click the selected candidate)
    │
    └─ 5. Post-click transition verification
          - Did screen state change?
          - Does new state resemble GroundingTarget.screenshot_after?
          - If not → retry with next-best candidate or escalate to Tier 3
```

## Tier details

### Pre-step: State narrowing

Before grounding, check preconditions:
- **Window title**: OCR the title bar, match against `GroundingTarget.window_title`
- **Landmark text**: Check `nearby_text` / `surrounding_labels` are present on screen
- **Viewport**: Is the target region visible (scroll position similar to demo)?

If preconditions fail → state recovery (open the right app, navigate to the right page, scroll). This is cheaper than grounding the wrong element on the wrong screen.

### Tier 1.5a: Text anchoring

- Run OCR on current screenshot
- Match `description` and `nearby_text` against OCR results
- **Exact match**: confidence 0.95 (accept immediately)
- **Fuzzy substring match**: confidence 0.6-0.8 (add to candidates, don't accept alone)
- **Layout consistency**: if matched text is in a plausible region relative to demo, boost confidence
- **Separate from visual matching** because text grounding is semantically stable across themes/resolutions

When a11y tree is available (Windows UIAutomation):
- Match by element name/role
- Deterministic — confidence 1.0 if exact match
- Not available in VNC/remote desktop scenarios

### Tier 1.5b: Visual retrieval

- **CLIP crop match** (primary): Embed `GroundingTarget.crop` and candidate regions with CLIP. Rank by cosine similarity. Use relative gap to next-best candidate, not raw similarity.
- **pHash** (cheap pre-filter only): Use to shortlist regions quickly. Never as final decider — brittle to scaling, DPI, hover states, antialiasing.
- Return top-K candidates with similarity scores

### Tier 2a: UI-Venus (primary VLM grounder)

- Send screenshot + `description` to UI-Venus HTTP endpoint
- Returns bbox `[x1, y1, x2, y2]`
- Proven 1.0 on client tasks
- Availability check with timeout + retry (not hard fallback on first failure)
- Requires GPU serving (~$0.50/hr)

### Tier 2b: GPT-5.4 (API fallback)

- Send screenshot + `description` to GPT-5.4 API
- Native computer use capabilities (75% OSWorld)
- Higher cost (~$0.01/call) but no GPU needed
- Use when UI-Venus endpoint is unavailable after retries

### Tier 3: Planner recovery

- Full VLM reasons about unexpected state
- Triggered when:
  - State check fails and recovery is needed
  - Post-click verification fails on all candidates
  - No candidate meets minimum confidence
- NOT just a grounding fallback — also handles state recovery

## Candidate selection and verification

### Selection policy

Each tier produces candidates with tier-local scores. Do NOT compare raw scores across tiers. Instead:

```python
def select_candidate(candidates):
    # 1. If a11y exact match exists → accept (deterministic)
    # 2. If OCR exact match + layout consistent → accept
    # 3. If CLIP top candidate has large gap to second-best → accept
    # 4. If UI-Venus returned bbox → verify crop resemblance
    # 5. If multiple candidates, prefer text-anchored over visual-only
    # 6. Final tiebreak: spatial consistency with demo position
```

### Pre-click verification

Before clicking, verify the candidate:
- Crop the region around the candidate point from current screenshot
- Compare to `GroundingTarget.crop` (CLIP similarity or pHash)
- If crop doesn't resemble the target → reject candidate, try next best

### Post-click transition verification

After clicking, verify the transition:
- Take new screenshot
- Compare to `GroundingTarget.screenshot_after`
- Check expected change occurred:
  - "dialog closes" → previous dialog elements no longer present
  - "new page loads" → new landmark text appears
  - "text appears" → specific text now visible
- If verification fails → retry with next-best candidate, then escalate to Tier 3

## What the demo must store

Updated demo JSON schema per step:

```json
{
  "step_index": 2,
  "action_type": "click",
  "target": {
    "description": "Clear browsing data button",
    "target_type": "button",
    "crop": "base64...",
    "crop_bbox": [450, 380, 620, 410],
    "click_offset": [85, 15],
    "nearby_text": ["Clear browsing data", "Privacy and security", "All time"],
    "window_title": "Settings - Google Chrome",
    "surrounding_labels": ["Privacy and security", "Clear browsing data", "Cookies and other site data"]
  },
  "screenshot_before": "step_002_before.png",
  "screenshot_after": "step_002_after.png",
  "expected_change": "Confirmation dialog appears"
}
```

The demo recording pipeline must capture this. For imported openadapt-capture recordings, a VLM enrichment step generates the missing fields (crop extraction, nearby text via OCR, window title detection).

## Phase ordering

### Phase 1: GPT-5.4 as grounder (1 hour)
Unblock clear-browsing NOW. Change grounder model, re-run. No architecture changes.

### Phase 2: UI-Venus as primary grounder (2 hours)
Serve UI-Venus, point DemoExecutor at it. Cost optimization over Phase 1.

### Phase 3: GroundingTarget data model (1 day)
Implement `GroundingTarget` dataclass. Update demo recording to capture rich target data. VLM enrichment for imported recordings.

### Phase 4: Text anchoring — Tier 1.5a (1 day)
OCR-based candidate generation. a11y tree integration where available. This handles the majority of text-labeled buttons/links without any VLM call.

### Phase 5: Post-click verification (1 day)
Transition verification using before/after screenshots. Retry logic with next-best candidate. This catches bad clicks before they propagate.

### Phase 6: Visual retrieval — Tier 1.5b (2 days)
CLIP-based crop matching. pHash as pre-filter. Candidate ranking with relative gap scoring.

### Phase 7: State narrowing (1 day)
Pre-step precondition checks. Window title matching. Landmark text verification. State recovery before grounding.

### Deferred: RegionFocus zoom, app-specific plugins
Only if empirical evidence shows Tier 2 failing on cases zoom would fix. App-specific plugins (e.g., Chrome settings navigator) only if general cascade can't handle common flows.

## Design principles

1. **Target representation is the foundation.** Rich `GroundingTarget` enables every tier. Without it, each tier infers from a weak description string.
2. **Generate candidates, don't return answers.** Each tier proposes candidates. Selection is separate from generation.
3. **Verify transitions, not just coordinates.** Ground → act → verify. Bad clicks must not propagate.
4. **Text and visual are separate tiers.** Different failure modes, different confidence semantics.
5. **Confidence is tier-local.** Don't compare OCR confidence with CLIP similarity. Use tier-specific acceptance criteria.
6. **Cheapest first, but don't stop early.** If a cheap tier produces a candidate, verify it before accepting. Cheap ≠ reliable.
7. **Empirical over benchmarks.** UI-Venus 1.0 on our tasks > UI-TARS 94.2% on ScreenSpot.
8. **GPT-5.4 is the unblocker, not the solution.** Phase 1 gets us to 1.0. Phases 3-7 get us to production.
9. **State narrowing before grounding.** "Are we on the right screen?" before "Where is the button?"
10. **Demo screenshots are gold.** The demo literally shows what to click and what happens after. Use both.
