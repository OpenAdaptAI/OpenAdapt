# Screenshot Pipeline Audit & Enhancement Plan

**Date**: 2026-01-17
**Status**: Current State Analysis & Proposed Enhancements

## Executive Summary

The current screenshot generation pipeline successfully captures basic viewer functionality but has significant gaps:

1. **Missing Dependencies**: Episodes are not being generated from actual recordings
2. **Limited Coverage**: Only 3 scenarios per capture (full, controls, events)
3. **No Segmentation Demo**: segmentation_viewer.html uses sample data, not real episodes
4. **Static Captures**: Screenshots show one moment in time, not feature interactions
5. **Missing Viewers**: Synthetic demo viewer and benchmark viewer not documented

## Current State Analysis

### 1. Existing Screenshot Generation (`scripts/generate_readme_screenshots.py`)

**What it does well:**
- ✅ Loads real captures from openadapt-capture
- ✅ Generates HTML viewers using openadapt_viewer components
- ✅ Uses Playwright for automated screenshots
- ✅ Creates consistent output structure (docs/images/)
- ✅ Has comprehensive error handling
- ✅ Integrated with GitHub Actions CI

**Current screenshots:**
```
docs/images/
├── turn-off-nightshift_full.png       (159 KB) - Complete interface
├── turn-off-nightshift_controls.png   (107 KB) - Playback controls
├── turn-off-nightshift_events.png     (79 KB)  - Event list
├── demo_new_full.png                  (103 KB)
├── demo_new_controls.png              (63 KB)
└── demo_new_events.png                (44 KB)
```

**Limitations:**
- Only 3 views per capture (doesn't show feature interactions)
- No segmentation viewer screenshots
- No synthetic demo viewer screenshots
- No benchmark viewer screenshots
- Doesn't demonstrate search functionality
- Doesn't show filter dropdowns in action
- Doesn't show episode progression

### 2. Segmentation Viewer State

**File**: `segmentation_viewer.html`

**Current behavior:**
- Uses file input to load JSON
- Has catalog integration (via `segmentation_generator.py`)
- Uses sample data (`sample_episode_library.json`)
- **NOT loading real episodes from recordings**

**Missing integration:**
- No `episodes.json` in `/path/to/openadapt-capture/turn-off-nightshift/`
- Episodes need to be generated via segmentation pipeline in openadapt-ml
- test_episodes.json exists in viewer root (appears to be test data)

**Segmentation output exists in openadapt-ml:**
```
/path/to/openadapt-ml/segmentation_output/
├── turn-off-nightshift_episodes.json  (626 bytes)
├── turn-off-nightshift_transcript.json (15 KB)
├── demo_new_episodes.json
├── episode_library.json
└── test_results.json
```

**Required action**: Copy/symlink episodes from openadapt-ml to capture directories

### 3. README Screenshot Coverage

**Current README sections with screenshots:**
- ✅ Full Viewer Interface
- ✅ Playback Controls
- ✅ Event List, Details, and Transcript
- ✅ Demo Workflow
- ❌ Segmentation Viewer (mentioned in text but no screenshots)
- ❌ Synthetic Demo Viewer (mentioned but no screenshots)
- ❌ Benchmark Viewer (exists but not documented)

### 4. Other Viewers

**Synthetic Demo Viewer** (`synthetic_demo_viewer.html`):
- Interactive browser for 82 synthetic WAA demos
- 6 domains (notepad, paint, clock, browser, file_explorer, office)
- Filter by domain, task selector
- Shows demo content + API prompt usage
- Side-by-side impact comparison
- **No screenshots in README**

**Benchmark Viewer** (`benchmark_viewer.html`):
- Shows benchmark evaluation results
- Task list with pass/fail status
- Step-by-step replay
- Domain breakdown
- **No screenshots in README**

**Retrieval Viewer** (`retrieval_viewer.html`):
- Demo search result display
- **No screenshots in README**

**Capture Viewer** (`capture_viewer.html`):
- Alternative capture playback interface
- **No screenshots in README**

## Gap Analysis

### Critical Gaps

1. **No Real Episode Data**
   - Segmentation viewer uses sample JSON
   - Episodes exist in openadapt-ml but not accessible to viewer
   - Need to copy/symlink episode JSON files

2. **Limited Feature Coverage**
   - Search functionality not demonstrated
   - Filter dropdowns not shown in use
   - Episode progression not visualized
   - Recording selector not shown

3. **Missing Viewer Documentation**
   - 4+ viewers exist but only 2 are documented with screenshots
   - Synthetic demo viewer is powerful but invisible in README
   - Benchmark viewer is complete but undocumented

### Moderate Gaps

4. **Static Screenshots**
   - Current approach: 3 static views per capture
   - Missing: Interactive features (search, filters, selection states)
   - Missing: Episode boundaries and progression

5. **No Multi-Episode Demo**
   - Single recording doesn't show episode library concept
   - No demonstration of deduplication
   - No cross-recording episode discovery

### Minor Gaps

6. **No CI Automation for Segmentation**
   - CI generates capture viewer screenshots
   - But doesn't generate segmentation or other viewer screenshots

## Proposed Enhancements

### Phase 1: Integrate Real Episode Data (HIGH PRIORITY)

**Goal**: Make segmentation_viewer.html display real episodes from recordings

**Actions:**
1. Copy episode JSON from openadapt-ml to capture directories
   ```bash
   cp /path/to/openadapt-ml/segmentation_output/turn-off-nightshift_episodes.json \
      /path/to/openadapt-capture/turn-off-nightshift/episodes.json
   ```

2. Update segmentation_viewer.html to auto-load from known path

3. Test with catalog integration:
   ```bash
   uv run python -m openadapt_viewer.cli segmentation --auto-load turn-off-nightshift --open
   ```

4. Generate segmentation viewer screenshots

### Phase 2: Expand Screenshot Coverage (HIGH PRIORITY)

**Goal**: Show ALL viewer functionality systematically

**New screenshot scenarios:**

**Segmentation Viewer** (6 new screenshots):
- `segmentation_full.png` - Episode library overview
- `segmentation_episode_detail.png` - Selected episode with key frames
- `segmentation_search.png` - Search in action ("nightshift" → results)
- `segmentation_filter.png` - Recording dropdown filtering
- `segmentation_timeline.png` - Episode timeline/progression view
- `segmentation_integration.png` - "View Full Recording" button leading to capture viewer

**Synthetic Demo Viewer** (4 new screenshots):
- `synthetic_demo_overview.png` - Demo library with domain filters
- `synthetic_demo_detail.png` - Selected demo with steps
- `synthetic_demo_prompt.png` - API prompt usage panel
- `synthetic_demo_impact.png` - Side-by-side accuracy comparison

**Benchmark Viewer** (4 new screenshots):
- `benchmark_summary.png` - Task summary with metrics
- `benchmark_task_list.png` - Task list with filters
- `benchmark_replay.png` - Step-by-step task replay
- `benchmark_domain_breakdown.png` - Domain statistics

**Capture Viewer** (Enhanced - 3 new):
- `capture_multi_episode.png` - Timeline showing multiple episode boundaries
- `capture_episode_context.png` - Episode context banner at top
- `capture_episode_highlight.png` - Episode time range highlighted on timeline

### Phase 3: Interactive Feature Demonstration (MEDIUM PRIORITY)

**Goal**: Show features in use, not just static views

**Approach**: Playwright interactions before screenshot

**Examples:**

1. **Search Functionality**:
   ```python
   # Type search query
   page.fill('#search-input', 'nightshift')
   page.wait_for_timeout(500)  # Let results filter
   page.screenshot(path='segmentation_search_active.png')
   ```

2. **Episode Selection**:
   ```python
   # Click first episode
   page.click('.episode-item:first-child')
   page.wait_for_timeout(500)
   page.screenshot(path='segmentation_episode_selected.png')
   ```

3. **Filter Dropdown**:
   ```python
   # Open recording filter
   page.click('#recording-filter')
   page.wait_for_timeout(300)
   page.screenshot(path='segmentation_filter_open.png')
   ```

4. **Playback in Progress**:
   ```python
   # Start playback
   page.click('.play-button')
   page.wait_for_timeout(1000)  # Let animation start
   page.screenshot(path='capture_playback_active.png')
   ```

### Phase 4: Intelligent Frame Selection (MEDIUM PRIORITY)

**Goal**: Show episode progression, not just one frame

**Approach**: Generate episode-aware screenshots

**Implementation:**
```python
def generate_episode_screenshots(episodes, capture_path, output_dir):
    """Generate one screenshot per episode showing key frame."""
    for episode in episodes:
        # Get representative frame (usually first or middle frame)
        key_frame_index = episode['frame_indices'][0]

        # Load that frame
        screenshot_path = f"{capture_path}/screenshots/step_{key_frame_index}.png"

        # Create composite showing:
        # - Episode name
        # - Key frame
        # - Episode duration
        # - Timeline position

        composite = create_episode_composite(
            screenshot_path,
            episode['name'],
            episode['duration'],
            episode['start_time'],
        )

        output_path = output_dir / f"episode_{episode['episode_id']}.png"
        composite.save(output_path)
```

### Phase 5: Enhanced Autogeneration Script

**Goal**: One command generates comprehensive screenshot gallery

**New script structure:**
```python
# scripts/generate_comprehensive_screenshots.py

SCREENSHOT_SCENARIOS = {
    'capture_viewer': [
        {'name': 'full', 'viewport': (1400, 900), 'interact': None},
        {'name': 'controls', 'viewport': (1400, 600), 'interact': scroll_to_controls},
        {'name': 'events', 'viewport': (800, 900), 'interact': scroll_to_events},
        {'name': 'episode_timeline', 'viewport': (1400, 400), 'interact': show_episode_boundaries},
    ],

    'segmentation_viewer': [
        {'name': 'overview', 'viewport': (1400, 900), 'interact': None},
        {'name': 'episode_detail', 'viewport': (1400, 900), 'interact': lambda p: p.click('.episode-item:first-child')},
        {'name': 'search_active', 'viewport': (1400, 900), 'interact': lambda p: p.fill('#search', 'nightshift')},
        {'name': 'filter_by_recording', 'viewport': (1400, 900), 'interact': lambda p: p.select_option('#recording-filter', 'turn-off-nightshift')},
    ],

    'synthetic_demo_viewer': [
        {'name': 'overview', 'viewport': (1400, 900), 'interact': None},
        {'name': 'notepad_demo', 'viewport': (1400, 900), 'interact': lambda p: p.select_option('#domain-filter', 'notepad')},
        {'name': 'prompt_panel', 'viewport': (1400, 900), 'interact': lambda p: p.click('.demo-item:first-child')},
        {'name': 'impact_comparison', 'viewport': (1400, 600), 'interact': scroll_to_impact},
    ],

    'benchmark_viewer': [
        {'name': 'summary', 'viewport': (1400, 900), 'interact': None},
        {'name': 'task_list', 'viewport': (1400, 900), 'interact': scroll_to_tasks},
        {'name': 'task_detail', 'viewport': (1400, 900), 'interact': lambda p: p.click('.task-item:first-child')},
        {'name': 'domain_breakdown', 'viewport': (1400, 600), 'interact': scroll_to_domains},
    ],
}

def generate_all_screenshots():
    """Generate comprehensive screenshot gallery for README."""
    for viewer_type, scenarios in SCREENSHOT_SCENARIOS.items():
        viewer_html = generate_viewer(viewer_type)

        for scenario in scenarios:
            take_screenshot(
                viewer_html,
                f"{viewer_type}_{scenario['name']}.png",
                viewport=scenario['viewport'],
                interact=scenario['interact'],
            )
```

### Phase 6: README Update

**Goal**: Comprehensive visual documentation

**New README structure:**

```markdown
## Screenshots

### Capture Playback Viewer

Interactive viewer for exploring captured GUI interactions with playback controls, timeline navigation, event details, and real-time audio transcript.

![Full Viewer Interface](docs/images/turn-off-nightshift_full.png)
*Complete interface showing screenshot display (center), event list (right sidebar top), and audio transcript (right sidebar bottom)*

![Playback Controls](docs/images/turn-off-nightshift_controls.png)
*Timeline and playback controls with overlay toggle, event details and synchronized transcript panel*

![Episode Timeline](docs/images/capture_episode_timeline.png)
*Timeline showing multiple episode boundaries with highlighted current episode*

![Episode Context](docs/images/capture_episode_context.png)
*Episode context banner showing which episode is currently being viewed*

### Segmentation Viewer

Browse and explore episodes extracted from recordings. Search, filter, and view episodes with their key frames and steps.

![Episode Library](docs/images/segmentation_overview.png)
*Episode library showing all extracted workflows with thumbnails and descriptions*

![Episode Detail](docs/images/segmentation_episode_detail.png)
*Detailed view of a single episode with key frames, steps, and metadata*

![Search in Action](docs/images/segmentation_search_active.png)
*Search functionality finding episodes by keyword (e.g., "nightshift")*

![Filter by Recording](docs/images/segmentation_filter.png)
*Filter dropdown showing episodes from a specific recording*

![View Full Recording](docs/images/segmentation_integration.png)
*"View Full Recording" button links to capture viewer with episode context*

### Synthetic Demo Viewer

Interactive browser for 82 synthetic WAA demonstration trajectories across 6 Windows domains.

![Demo Library](docs/images/synthetic_demo_overview.png)
*Complete demo library with domain filters and task selector*

![Demo Detail](docs/images/synthetic_demo_detail.png)
*Selected demo showing step-by-step actions with syntax highlighting*

![API Prompt Usage](docs/images/synthetic_demo_prompt.png)
*How demos are included in actual API prompts for demo-conditioned prompting*

![Impact Comparison](docs/images/synthetic_demo_impact.png)
*Side-by-side comparison showing 33% → 100% accuracy improvement with demos*

### Benchmark Viewer

Visualize benchmark evaluation results with task-level metrics, step-by-step replay, and domain breakdown.

![Benchmark Summary](docs/images/benchmark_summary.png)
*Overall metrics: total tasks, pass/fail counts, success rate by domain*

![Task List](docs/images/benchmark_task_list.png)
*Filterable task list with status badges and difficulty rankings*

![Task Replay](docs/images/benchmark_replay.png)
*Step-by-step replay of a benchmark task execution with screenshots*

![Domain Breakdown](docs/images/benchmark_domain_breakdown.png)
*Success rate breakdown by domain (office, browser, file explorer, etc.)*
```

### Phase 7: CI/CD Integration

**Goal**: Automated regeneration on changes

**GitHub Action enhancements:**

```yaml
# .github/workflows/screenshots.yml (enhanced)

- name: Generate all viewer screenshots
  run: |
    # Capture viewers
    python scripts/generate_comprehensive_screenshots.py \
      --viewers capture segmentation synthetic benchmark \
      --output-dir docs/images

    # Check for segmentation data
    if [ ! -f ../openadapt-capture/turn-off-nightshift/episodes.json ]; then
      echo "Generating episodes from recording..."
      cd ../openadapt-ml
      python -m openadapt_ml.segmentation.cli segment \
        --recording ../openadapt-capture/turn-off-nightshift \
        --output episodes.json
      cp episodes.json ../openadapt-capture/turn-off-nightshift/
    fi

- name: Validate screenshot completeness
  run: |
    python scripts/validate_screenshots.py \
      --expected-count 25 \
      --check-size \
      --check-dimensions
```

## Implementation Priority

### P0 - Critical (Do First)
1. ✅ Audit complete (this document)
2. Generate real episodes for turn-off-nightshift
3. Integrate episodes with segmentation viewer
4. Generate segmentation viewer screenshots
5. Update README with segmentation viewer section

### P1 - High (Do Next)
6. Add synthetic demo viewer screenshots
7. Add benchmark viewer screenshots
8. Enhance autogeneration script with interaction support
9. Update README with all viewer sections

### P2 - Medium (Do After)
10. Implement intelligent frame selection (one per episode)
11. Add episode timeline view to capture viewer
12. Add episode progression screenshots
13. Enhance CI to generate all viewers

### P3 - Nice to Have
14. Animated GIFs for key interactions
15. Video clips of playback
16. Side-by-side comparison screenshots
17. Dark mode screenshots
18. Mobile viewport screenshots

## Success Metrics

**Comprehensive Coverage:**
- ✅ All 4+ viewers documented with screenshots
- ✅ All major features visible (search, filters, selection, playback)
- ✅ Episode concept clearly demonstrated
- ✅ Real data (not just samples) shown

**Quality:**
- ✅ Screenshots load quickly (< 200 KB each)
- ✅ Consistent viewport sizes
- ✅ Clear, readable text
- ✅ Visual hierarchy clear

**Maintainability:**
- ✅ One command regenerates all screenshots
- ✅ CI automatically updates on changes
- ✅ Script is well-documented
- ✅ Easy to add new scenarios

## Files to Create/Modify

### New Files
- [ ] `scripts/generate_comprehensive_screenshots.py` - Enhanced script
- [ ] `scripts/validate_screenshots.py` - Screenshot validation
- [ ] `docs/SCREENSHOT_GUIDELINES.md` - Screenshot standards
- [ ] `docs/images/segmentation_*.png` - Segmentation viewer screenshots (6)
- [ ] `docs/images/synthetic_demo_*.png` - Synthetic demo screenshots (4)
- [ ] `docs/images/benchmark_*.png` - Benchmark viewer screenshots (4)
- [ ] `docs/images/capture_episode_*.png` - Enhanced capture screenshots (3)

### Modified Files
- [ ] `scripts/generate_readme_screenshots.py` - Add more scenarios
- [ ] `README.md` - Comprehensive screenshot gallery
- [ ] `.github/workflows/screenshots.yml` - Enhanced CI
- [ ] `segmentation_viewer.html` - Auto-load real episodes
- [ ] `CONTRIBUTING.md` - Document screenshot pipeline

### Data Files Needed
- [ ] `/path/to/openadapt-capture/turn-off-nightshift/episodes.json` (from ML)
- [ ] `/path/to/openadapt-capture/demo_new/episodes.json` (from ML)

## Timeline Estimate

| Phase | Tasks | Estimated Time |
|-------|-------|----------------|
| Phase 1 | Episode integration | 1-2 hours |
| Phase 2 | Screenshot generation | 2-3 hours |
| Phase 3 | Interactive demos | 2-3 hours |
| Phase 4 | Intelligent frames | 3-4 hours |
| Phase 5 | Script enhancement | 2-3 hours |
| Phase 6 | README update | 1-2 hours |
| Phase 7 | CI integration | 2-3 hours |
| **Total** | **All phases** | **13-20 hours** |

## Immediate Next Steps

1. **Generate episodes** (30 min):
   ```bash
   cd /path/to/openadapt-ml
   # Check if segmentation CLI works
   python -m openadapt_ml.segmentation.cli --help
   # Or use existing episodes
   cp segmentation_output/turn-off-nightshift_episodes.json \
      ../openadapt-capture/turn-off-nightshift/episodes.json
   ```

2. **Test segmentation viewer** (15 min):
   ```bash
   cd /path/to/openadapt-viewer
   uv run python -m openadapt_viewer.cli segmentation \
     --auto-load turn-off-nightshift --open
   ```

3. **Generate segmentation screenshots** (30 min):
   - Update generate_readme_screenshots.py to handle segmentation viewer
   - Generate 6 screenshots per Phase 2 plan
   - Test with Playwright

4. **Update README** (30 min):
   - Add Segmentation Viewer section
   - Add screenshots with captions
   - Link to live viewers

**Total immediate work: ~2 hours to get segmentation viewer documented**

## Questions for User

1. Should we prioritize segmentation viewer first (most impactful) or do all viewers at once?
2. Are there specific features you want highlighted in screenshots?
3. Should screenshots show real recordings or can some use sample/synthetic data?
4. Is the 25-screenshot target (from all viewers) acceptable or should we aim for more/fewer?
5. Should we add animated GIFs for key interactions or stick with static PNGs?

## Conclusion

The current pipeline is solid but incomplete. Main gaps:
1. **Missing episode data integration** - easily fixed by copying JSON
2. **Limited viewer coverage** - 4 viewers exist but only 2 are documented
3. **No feature interaction demos** - screenshots are static, not interactive

Proposed enhancements will provide **comprehensive, automated, maintainable** screenshot generation covering **all viewer functionality** with **real data**.

**Recommended approach**: Start with P0 (segmentation viewer), then expand to all viewers (P1), then add polish (P2/P3).
