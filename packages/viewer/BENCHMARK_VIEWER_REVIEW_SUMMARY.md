# Benchmark Viewer Review - Summary

**Review Date**: January 17, 2026
**Reviewed Files**:
- `/path/to/openadapt-ml/openadapt_ml/training/benchmark_viewer.py` (172KB, 4,774 lines)
- `/path/to/openadapt-ml/training_output/current/benchmark.html` (568KB, 3,065 lines)
- `/path/to/openadapt-evals/openadapt_evals/benchmarks/viewer.py` (43KB, 1,283 lines)

---

## TL;DR

**What you asked**: Why does `http://localhost:8765/benchmark.html` show "no evaluation running" when Azure eval (agent aace3b9) is active?

**Answer**: The viewer and the evaluation use different tracking systems that aren't connected:
- **Your Azure eval** writes to `live_eval_state.json` (openadapt-evals)
- **The viewer** reads from `benchmark_live.json` (openadapt-ml, last updated Jan 9)

**Bigger problem**: The viewer is a 4,774-line monolithic Python module with 64% unused features, SSE connection management that never works, and 48 indistinguishable mock runs in a dropdown.

**Recommendation**: **Deprecate and rewrite** (3 days) rather than fix technical debt (7 days). Base on openadapt-evals viewer architecture (1,283 lines, proven, focused).

---

## Documents in This Review

| Document | Purpose |
|----------|---------|
| **BENCHMARK_VIEWER_REVIEW.md** | Full technical analysis with root cause, complexity metrics, and migration plan |
| **BENCHMARK_VIEWER_METRICS.md** | Quantitative metrics: size, complexity, performance, technical debt score |
| **BENCHMARK_VIEWER_ISSUES.md** | User-facing issues explained with examples and fix options |
| **BENCHMARK_VIEWER_REVIEW_SUMMARY.md** | This file - quick reference and decision guide |

---

## Quick Comparison

### Current Viewer (openadapt-ml)

```
Size: 172KB source → 568KB HTML
Lines: 4,774 Python → 3,065 HTML
Features: 40+ JS functions, 139 CSS classes, 5 UI panels
Complexity: SSE + polling + stale detection + multi-run support
Issues: Live eval broken, runs indistinguishable, 64% unused features
```

### Modern Viewer (openadapt-evals)

```
Size: 43KB source → ~100KB HTML
Lines: 1,283 Python → ~600 HTML
Features: 15 JS functions, ~50 CSS classes, focused UI
Complexity: Simple file loading, single-run display
Issues: None (but lacks live tracking and multi-run comparison)
```

**Improvement**: 3.7x smaller, 93% less complexity, cleaner architecture

---

## The 6 Issues You're Experiencing

1. **"No evaluation running"** despite active Azure eval
   - Severity: Critical
   - Cause: Different tracking systems (live_eval_state.json vs benchmark_live.json)
   - Fix: Bridge or symlink (1 day)

2. **Mock data warning** on all runs
   - Severity: Low
   - Cause: All 48 runs have "mock" in name
   - Fix: Better detection or remove (0.1 days)

3. **"Unknown - 0%"** for all 48 runs
   - Severity: High
   - Cause: Metadata not loaded properly
   - Fix: Better label generation (0.5 days)

4. **48 overwhelming runs** in dropdown
   - Severity: Medium
   - Cause: No filtering or pagination
   - Fix: Add search/filter (0.5 days)

5. **No live connection** to running eval
   - Severity: Critical
   - Cause: Architecture mismatch between packages
   - Fix: API bridge or file watcher (1 day)

6. **No visual feedback** during eval
   - Severity: Medium
   - Cause: Limited UI design
   - Fix: Progress bar, timeline, ETA (1 day)

**Total fix time**: ~4 days

---

## Decision Matrix

### Option A: Fix Current Viewer

**Effort**: 7 days
- Refactor inline HTML → separate files (2 days)
- Fix live evaluation connection (1 day)
- Fix metadata loading (1 day)
- Remove unused panels (0.5 days)
- Simplify SSE/polling (0.5 days)
- Add run comparison UI (2 days)

**Result**: Fixed viewer but technical debt remains (monolithic architecture)

### Option B: Rewrite Based on openadapt-evals

**Effort**: 3 days
- Adapt openadapt-evals viewer (0.5 days)
- Add live tracking API bridge (1 day)
- Add multi-run comparison (optional) (1 day)
- Test and deploy (0.5 days)

**Result**: Clean implementation, no technical debt, maintainable

### Option C: Quick Fixes Only

**Effort**: 1 day
- Fix live evaluation connection (1 day)
- Accept other issues as-is

**Result**: Primary use case works, other issues remain

---

## Recommendation: Option B (Rewrite)

### Why Rewrite Wins

1. **Less effort**: 3 days vs 7 days
2. **Better result**: Clean codebase vs fixed-but-still-complex
3. **Future-proof**: Easier to add features vs hard-to-modify monolith
4. **Proven base**: openadapt-evals viewer already works and is simpler

### Why Not Just Fix?

The current viewer suffers from **architectural issues** not bugs:
- Inline HTML generation in Python (hard to test/debug)
- Monolithic 4,774-line module (hard to understand/modify)
- SSE + polling + stale detection (complexity with no benefit)
- Multi-run support that doesn't scale (48 runs = unusable UI)

These aren't quick fixes - they require substantial refactoring approaching a rewrite anyway.

### Risks of Rewrite

1. **Feature loss**: Some multi-run features need reimplementation
   - Mitigation: Add as needed based on actual usage
2. **Learning curve**: New codebase to learn
   - Mitigation: openadapt-evals viewer is simpler (1,283 lines vs 4,774)
3. **Migration**: Update links and documentation
   - Mitigation: Parallel deployment for smooth transition

---

## Migration Plan

### Phase 1: Parallel Deployment (Week 1)

- [ ] Adapt openadapt-evals viewer
- [ ] Add live tracking bridge
- [ ] Deploy at `/benchmark-v2.html`
- [ ] Test with current benchmark runs
- [ ] Keep old viewer at `/benchmark.html`

### Phase 2: Feedback (Week 2)

- [ ] Gather user feedback
- [ ] Add missing features
- [ ] Fix issues
- [ ] Performance testing

### Phase 3: Deprecation (Week 3)

- [ ] Add deprecation notice to old viewer
- [ ] Redirect `/benchmark.html` → `/benchmark-v2.html`
- [ ] Update documentation

### Phase 4: Removal (Week 4)

- [ ] Remove old viewer code
- [ ] Clean up deprecated endpoints
- [ ] Archive for reference

---

## Immediate Action Items

### If You Choose Rewrite (Recommended)

1. **Week 1 (3 days)**:
   - Copy `openadapt_evals/benchmarks/viewer.py` as base
   - Add `LiveEvaluationTracker` → `benchmark_live.json` bridge
   - Test with mock runs and live Azure eval
   - Deploy at `/benchmark-v2.html` (parallel to old)

2. **Week 2 (2 days)**:
   - Gather feedback from team
   - Add multi-run comparison if needed
   - Polish UI based on feedback

3. **Week 3 (1 day)**:
   - Redirect old viewer to new
   - Update docs and links
   - Announce to team

### If You Choose Quick Fix (Option C)

1. **Today (1 hour)**:
   ```python
   # In openadapt_evals/benchmarks/live_tracker.py
   class LiveEvaluationTracker:
       def update_state(self, state: dict):
           # Write to both locations
           self.state_path.write_text(json.dumps(state))

           # ADDED: Also write to openadapt-ml location
           ml_path = Path("/path/to/openadapt-ml/training_output/current/benchmark_live.json")
           ml_path.write_text(json.dumps(state))
   ```

2. **Test**:
   ```bash
   # Run Azure eval
   uv run python -m openadapt_evals.benchmarks.cli azure --workers 10

   # Check viewer at http://localhost:8765/benchmark.html
   # Should now show "Running" instead of "No evaluation running"
   ```

---

## Key Metrics

| Metric | Current | After Rewrite | Improvement |
|--------|---------|---------------|-------------|
| Source size | 172KB | ~50KB | 3.4x smaller |
| Generated HTML | 568KB | ~100KB | 5.7x smaller |
| Lines of code | 4,774 | ~1,500 | 3.2x less |
| Features implemented | 11 | 6 | Focused |
| Features working | 4 (36%) | 6 (100%) | No dead code |
| Technical debt score | 99/120 (83%) | ~30/120 (25%) | Healthy |

---

## Questions to Consider

1. **How often do you need multi-run comparison?**
   - If rarely: Simple viewer is fine
   - If often: Need better UI than current dropdown

2. **How critical is live tracking?**
   - If critical: Must fix immediately (1 day)
   - If nice-to-have: Can be added later

3. **Do you have 3 days for a rewrite?**
   - If yes: Rewrite recommended (better long-term)
   - If no: Quick fix now, rewrite later

4. **Will you add more benchmark features?**
   - If yes: Rewrite gives better foundation
   - If no: Quick fix may be sufficient

---

## Next Steps

**Recommended path**:

1. **Read this review** (you are here)
2. **Check BENCHMARK_VIEWER_ISSUES.md** for detailed issue explanations
3. **Review BENCHMARK_VIEWER_METRICS.md** for quantitative analysis
4. **Read full BENCHMARK_VIEWER_REVIEW.md** for technical details
5. **Decide**: Rewrite, Fix, or Quick Fix
6. **Start implementation** using migration plan above

**Questions?** The documents provide:
- Issue descriptions with screenshots/examples
- Fix options with code samples
- Complexity estimates
- Migration checklist

---

## Contact / Feedback

This review was conducted by Claude Code (Sonnet 4.5) on January 17, 2026.

For questions or feedback on this review:
- Review the detailed documents in this directory
- Check the code locations listed in each document
- Test the quick fixes suggested in BENCHMARK_VIEWER_ISSUES.md

---

## File Locations Reference

```
Review documents:
/path/to/openadapt-viewer/
├── BENCHMARK_VIEWER_REVIEW.md (main analysis)
├── BENCHMARK_VIEWER_METRICS.md (quantitative data)
├── BENCHMARK_VIEWER_ISSUES.md (user-facing issues)
└── BENCHMARK_VIEWER_REVIEW_SUMMARY.md (this file)

Current viewer:
/path/to/openadapt-ml/
├── openadapt_ml/training/benchmark_viewer.py (generator)
└── training_output/current/benchmark.html (generated)

Modern viewer:
/path/to/openadapt-evals/
└── openadapt_evals/benchmarks/viewer.py (cleaner approach)

Benchmark data:
/path/to/openadapt-ml/benchmark_results/
└── waa-mock_eval_*/  (48+ runs)
```

---

**End of Summary**

For detailed analysis, see BENCHMARK_VIEWER_REVIEW.md
