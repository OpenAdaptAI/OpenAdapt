# Grounding Cascade Design for DemoExecutor

## Problem

DemoExecutor replays mouse-based demonstrations. Click actions require grounding — finding where to click on the current screen given a description from the demo. The current grounder (gpt-4.1-mini) can't reliably click "Clear data" in Chrome's settings, capping clear-browsing at 0.25.

Users record mouse-based demos. That's the product. We must make mouse demos work, not work around them with keyboard shortcuts.

## Empirical evidence

| Grounder | Task | Score | Source |
|----------|------|-------|--------|
| gpt-4.1-mini | notepad-hello (1 click) | 1.00 | Our flywheel validation |
| gpt-4.1-mini | clear-browsing (1 critical click) | 0.25 | Our flywheel validation |
| UI-Venus-1.5-8B | client's tasks | 1.00 | Client engagement |
| GPT-5.4 + UI-Venus | client's tasks | 1.00 | Client engagement |

UI-Venus is proven on our use case. gpt-4.1-mini is not.

## Cascade architecture

```
Step receives action from demo
    │
    ├─ keyboard/type → Tier 1: Execute directly (deterministic)
    │                  Cost: $0, Latency: 0ms
    │
    └─ click/double_click → Tier 1.5 → Tier 2 → Tier 3
```

### Tier 1: Deterministic (keyboard, type)
- Execute directly via pyautogui
- No model, no cost, no latency
- Already implemented in DemoExecutor

### Tier 1.5: Visual template matching (NEW)
- **1.5a — OCR text match**: Run lightweight OCR on current screenshot. Match demo step's `target_description` against detected text labels. Return center of matched text bounding box.
- **1.5b — pHash/CLIP crop match**: Compare a crop of the target element from the demo screenshot against regions in the current screenshot. pHash for identical elements, CLIP for visually similar ones.
- **Confidence threshold**: Accept if confidence > 0.8, else escalate to Tier 2.
- **Cost**: $0 (CPU-only). **Latency**: <500ms.
- **When it works**: UI looks similar to demo (same resolution, same theme, same layout). Handles window position changes.
- **When it fails**: UI changed between demo and replay (different theme, different resolution, new layout).

### Tier 2: Specialized UI grounder (UI-Venus, primary)
- **2a — UI-Venus** (default): Purpose-built for UI element grounding. Native bbox output `[x1, y1, x2, y2]`. Proven 1.0 on client tasks. Requires GPU serving (~$0.50/hr via sglang/vLLM).
- **2b — GPT-5.4** (fallback): Native computer use capabilities. 75% on OSWorld. Higher cost but no GPU needed. Use when UI-Venus endpoint is unavailable.
- **2c — Other specialized models** (future): UI-TARS-1.5, MAI-UI, OmniParser+VLM. Test empirically on our tasks before adopting.
- **Cost**: ~$0.002/call (UI-Venus local) or ~$0.01/call (GPT-5.4 API). **Latency**: 1-5s.

### Tier 3: Planner recovery
- Full VLM reasons about what went wrong and how to recover
- Only triggered when the screen state doesn't match expectations
- Already implemented in DemoExecutor (unused in practice — Tier 1+2 handle everything so far)
- **Cost**: ~$0.05/call. **Latency**: 5-15s.

## Confidence-based escalation

```python
def ground_click(step, current_screenshot, demo_screenshot):
    # Tier 1.5a: OCR text match
    if step.target_description:
        result = ocr_match(current_screenshot, step.target_description)
        if result.confidence > 0.8:
            return result.center

    # Tier 1.5b: Visual template match
    if step.target_crop:  # crop from demo screenshot
        result = template_match(current_screenshot, step.target_crop)
        if result.confidence > 0.8:
            return result.center

    # Tier 2a: UI-Venus (primary grounder)
    if ui_venus_available():
        result = ui_venus_ground(current_screenshot, step.target_description)
        if result.bbox:
            return result.bbox_center

    # Tier 2b: GPT-5.4 (fallback)
    result = gpt54_ground(current_screenshot, step.target_description)
    return result.coordinates
```

## What exists vs what to build

| Component | Status | Location |
|-----------|--------|----------|
| Tier 1 (keyboard/type) | Done | `demo_executor.py` |
| Tier 1.5a (OCR match) | Partially done | `openadapt-grounding/locator.py` (ElementLocator) |
| Tier 1.5b (pHash/CLIP) | Partially done | `training/planner_cache.py` (pHash), CLIP in deps |
| Tier 2a (UI-Venus HTTP) | Done | `demo_executor.py` lines 254-313, `serve_ui_venus.sh` |
| Tier 2b (GPT-5.4 API) | Done | `demo_executor.py` (grounder_model param) |
| Tier 3 (planner recovery) | Done | `demo_executor.py` |
| Cascade routing logic | **Not done** | Need to implement |
| Demo screenshot crops | **Not done** | Need to store target element crops in demo |

## Implementation plan

### Phase 1: GPT-5.4 as grounder (1 hour, unblocks clear-browsing)
Change `grounder_model="gpt-4.1-mini"` to `grounder_model="gpt-5.4"` in DemoExecutor defaults or test scripts. Re-run clear-browsing. If it hits 1.0, the product demo works TODAY.

### Phase 2: UI-Venus as primary grounder (2 hours, cost optimization)
Boot GPU, serve UI-Venus, point DemoExecutor at `grounder_endpoint`. Re-run clear-browsing. Verify 1.0. This replaces $0.01/click (GPT-5.4) with $0.002/click (UI-Venus local).

### Phase 3: Tier 1.5 template matching (1-2 days)
- Store target element crops in demo JSON (crop from demo screenshot at the click location)
- Implement `template_match()` using pHash + CLIP
- Implement `ocr_match()` using the existing ElementLocator from openadapt-grounding
- Add confidence-based routing in DemoExecutor before the VLM grounder call
- This eliminates most VLM calls for demos where the UI matches

### Phase 4: Test-time zoom (RegionFocus) (1 day)
- If Tier 2 grounder confidence is low, crop to predicted region at 2x resolution
- Re-run grounder on the cropped image
- Literature shows +28% accuracy improvement on ScreenSpot-Pro

### Phase 5: Empirical evaluation of alternative grounders (1-2 days)
- Test UI-TARS-1.5-7B, MAI-UI-8B on our tasks
- Compare against UI-Venus empirically
- Adopt whichever performs best on OUR tasks, not benchmarks

## Cost analysis

For a 10-step demo with 4 click actions:

| Architecture | VLM calls | Cost | Latency |
|-------------|-----------|------|---------|
| Current (gpt-4.1-mini, every click) | 4 | $0.006 | 12s |
| Phase 1 (GPT-5.4, every click) | 4 | $0.04 | 12s |
| Phase 2 (UI-Venus, every click) | 4 | $0.008 | 8s |
| Phase 3 (template + UI-Venus fallback) | 0-2 | $0-0.004 | 1-5s |

Phase 3 is 10x cheaper than Phase 1 and faster, because template matching handles most clicks without a VLM call.

## Design principles

1. **Empirical over benchmarks.** Use what works on OUR tasks. UI-Venus 1.0 > UI-TARS 94.2% on someone else's test.
2. **Cheapest first.** Template matching ($0) before VLM ($0.002) before API ($0.01).
3. **Confidence-driven.** Don't escalate when you're already right. Each tier reports confidence.
4. **Demo screenshots are gold.** The demo literally shows what the button looks like. Use that information (template matching) before asking a model.
5. **Fallback gracefully.** Every tier has a fallback. No single point of failure.
