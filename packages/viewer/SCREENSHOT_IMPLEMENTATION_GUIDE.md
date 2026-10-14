# Screenshot Pipeline Implementation Guide

**Date**: 2026-01-17
**Status**: Ready for Production Implementation

## Overview

This guide provides step-by-step instructions for implementing the enhanced screenshot pipeline that systematically displays ALL openadapt-viewer functionality.

## Files Created

### 1. Core Documentation
- ✅ `SCREENSHOT_PIPELINE_AUDIT.md` - Comprehensive gap analysis and enhancement plan
- ✅ `SCREENSHOT_IMPLEMENTATION_GUIDE.md` - This file
- ✅ `scripts/generate_comprehensive_screenshots.py` - Enhanced screenshot generation script

### 2. Data Preparation
- ✅ `/path/to/openadapt-capture/turn-off-nightshift/episodes.json` - Episode data for segmentation viewer

## Implementation Steps

### Phase 1: Environment Setup (5-10 minutes)

**Goal**: Install dependencies and verify tools work

```bash
cd /path/to/openadapt-viewer

# Install playwright for screenshots
uv pip install ".[screenshots]"

# Install chromium browser
uv run playwright install chromium

# Verify installation
python scripts/generate_comprehensive_screenshots.py --check-deps

# Expected output:
# Dependency Status:
#   playwright: ✓ Available
```

### Phase 2: Generate Capture Viewer Screenshots (30 minutes)

**Goal**: Regenerate existing screenshots plus episode-aware ones

#### 2.1 First ensure capture viewer HTML files exist

```bash
# Generate HTML viewers from captures
python scripts/generate_readme_screenshots.py \
  --capture-dir ../openadapt-capture \
  --output-dir docs/images \
  --skip-screenshots

# This creates:
# - temp/turn-off-nightshift_viewer.html
# - temp/demo_new_viewer.html
```

#### 2.2 Generate screenshots

```bash
# Generate all capture viewer screenshots
python scripts/generate_comprehensive_screenshots.py \
  --viewers capture \
  --output-dir docs/images

# Expected output:
# CAPTURE VIEWER
# HTML: temp/turn-off-nightshift_viewer.html
# Scenarios: 3
#   [full] Complete viewer interface with all panels
#     Viewport: 1400x900
#     Saved: turn-off-nightshift_full.png (159 KB)
#   [controls] Playback controls and timeline focus
#     ...
#   [events] Event list and details panel
#     ...
```

**Result**: 6 screenshots (3 per capture)

### Phase 3: Generate Segmentation Viewer Screenshots (30 minutes)

**Goal**: Demonstrate episode browsing and selection

#### 3.1 Verify episode data exists

```bash
# Check that episodes.json was copied
ls -lh ../openadapt-capture/turn-off-nightshift/episodes.json

# Should show ~3KB file
```

#### 3.2 Open segmentation viewer manually to verify

```bash
# Open in browser to test
open segmentation_viewer.html

# Test:
# 1. Load episode JSON file via file input
# 2. Verify episodes appear
# 3. Click an episode to see details
# 4. Try search functionality
# 5. Try recording filter
```

#### 3.3 Generate screenshots

```bash
python scripts/generate_comprehensive_screenshots.py \
  --viewers segmentation \
  --output-dir docs/images

# Expected: 5 screenshots showing:
# - Overview with episode list
# - Selected episode with key frames
# - Search filtering episodes
# - Recording filter dropdown
# - Key frames gallery
```

**Result**: 5 new segmentation viewer screenshots

### Phase 4: Generate Synthetic Demo Viewer Screenshots (30 minutes)

**Goal**: Showcase synthetic WAA demo library

#### 4.1 Verify synthetic demo viewer works

```bash
# Open in browser
open synthetic_demo_viewer.html

# Test:
# 1. Verify 82 demos load
# 2. Filter by domain (notepad, paint, etc.)
# 3. Select a demo to view steps
# 4. Check prompt panel shows API usage
# 5. Scroll to impact comparison section
```

#### 4.2 Generate screenshots

```bash
python scripts/generate_comprehensive_screenshots.py \
  --viewers synthetic \
  --output-dir docs/images

# Expected: 5 screenshots showing:
# - Demo library overview
# - Domain filter applied
# - Selected demo with syntax highlighting
# - API prompt panel
# - Impact comparison (33% → 100%)
```

**Result**: 5 new synthetic demo viewer screenshots

### Phase 5: Generate Benchmark Viewer Screenshots (30 minutes)

**Goal**: Visualize benchmark evaluation results

#### 5.1 Check if benchmark results exist

```bash
# Look for benchmark data
ls -lh benchmark_results/

# If empty, generate mock data:
cd ../openadapt-evals
uv run python -m openadapt_evals.benchmarks.cli test-collection --tasks 5
cd ../openadapt-viewer
```

#### 5.2 Generate benchmark viewer HTML

```bash
# Generate viewer from mock data
# (This command may need to be adapted based on actual benchmark structure)
python -c "
from openadapt_viewer.viewers.benchmark import generate_benchmark_html
generate_benchmark_html(
    data_path='../openadapt-evals/benchmark_results/run_001',
    output_path='benchmark_viewer_generated.html'
)
"
```

#### 5.3 Generate screenshots

```bash
python scripts/generate_comprehensive_screenshots.py \
  --viewers benchmark \
  --output-dir docs/images

# Expected: 4 screenshots showing:
# - Summary metrics
# - Task list with filters
# - Task detail replay
# - Domain breakdown
```

**Result**: 4 new benchmark viewer screenshots

### Phase 6: Update README (45 minutes)

**Goal**: Comprehensive visual documentation

#### 6.1 Backup current README

```bash
cp README.md README.md.backup
```

#### 6.2 Add new screenshot sections

The README should be updated with sections for:

1. **Capture Playback Viewer** (expand existing)
   - Add episode timeline screenshot
   - Add episode context screenshot

2. **Segmentation Viewer** (NEW)
   - Episode library overview
   - Episode detail view
   - Search in action
   - Filter dropdown
   - Integration with capture viewer

3. **Synthetic Demo Viewer** (expand existing)
   - Add detailed screenshots
   - Show domain filtering
   - Show prompt usage
   - Show impact comparison

4. **Benchmark Viewer** (NEW)
   - Summary dashboard
   - Task list
   - Task replay
   - Domain breakdown

See `SCREENSHOT_PIPELINE_AUDIT.md` § Phase 6 for complete markdown template.

#### 6.3 Test README locally

```bash
# Install markdown viewer (if not already installed)
# pip install grip

# Or just open in GitHub-compatible viewer
open README.md
```

### Phase 7: CI/CD Integration (30 minutes)

**Goal**: Automated regeneration on changes

#### 7.1 Update GitHub workflow

Edit `.github/workflows/screenshots.yml`:

```yaml
# Add after existing screenshot generation step

- name: Generate comprehensive screenshots
  working-directory: openadapt-viewer
  run: |
    source ../.venv/bin/activate

    # Generate all viewer screenshots
    python scripts/generate_comprehensive_screenshots.py \
      --viewers all \
      --output-dir docs/images

    echo "📸 All screenshots generated:"
    ls -lh docs/images/*.png
```

#### 7.2 Test workflow locally (if act is installed)

```bash
# Test GitHub Action locally with act
act push -j generate-screenshots

# Or just commit and push to trigger CI
```

#### 7.3 Commit and push

```bash
git add -A
git commit -m "feat: comprehensive screenshot pipeline

- Add enhanced screenshot generation script
- Generate screenshots for all viewers (capture, segmentation, synthetic, benchmark)
- Update README with comprehensive visual documentation
- Add episode data integration for segmentation viewer

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>"

git push
```

## Verification Checklist

Before considering implementation complete, verify:

### Screenshots Generated
- [ ] `turn-off-nightshift_full.png` (capture viewer)
- [ ] `turn-off-nightshift_controls.png`
- [ ] `turn-off-nightshift_events.png`
- [ ] `demo_new_full.png`
- [ ] `demo_new_controls.png`
- [ ] `demo_new_events.png`
- [ ] `segmentation_overview.png` (segmentation viewer)
- [ ] `segmentation_episode_detail.png`
- [ ] `segmentation_search_active.png`
- [ ] `segmentation_recording_filter.png`
- [ ] `segmentation_key_frames.png`
- [ ] `synthetic_overview.png` (synthetic demo viewer)
- [ ] `synthetic_domain_filter.png`
- [ ] `synthetic_demo_detail.png`
- [ ] `synthetic_prompt_panel.png`
- [ ] `synthetic_impact_section.png`
- [ ] `benchmark_summary.png` (benchmark viewer)
- [ ] `benchmark_task_list.png`
- [ ] `benchmark_task_detail.png`
- [ ] `benchmark_domain_breakdown.png`

**Total expected: 20 screenshots** (6 capture + 5 segmentation + 5 synthetic + 4 benchmark)

### README Updated
- [ ] All screenshots referenced in README exist
- [ ] Captions are descriptive and accurate
- [ ] Image paths are correct (docs/images/*.png)
- [ ] New viewer sections added (Segmentation, Benchmark)
- [ ] Existing sections enhanced (Synthetic Demo)

### Documentation Complete
- [ ] SCREENSHOT_PIPELINE_AUDIT.md exists
- [ ] SCREENSHOT_IMPLEMENTATION_GUIDE.md exists (this file)
- [ ] scripts/generate_comprehensive_screenshots.py documented with --help
- [ ] CI workflow updated to generate all screenshots

### Functionality Tested
- [ ] segmentation_viewer.html loads episode data
- [ ] Search works in segmentation viewer
- [ ] Filter dropdown works
- [ ] Episode selection shows details
- [ ] Synthetic demo viewer loads 82 demos
- [ ] Domain filters work
- [ ] Benchmark viewer shows task data (if available)

## Troubleshooting

### Playwright Issues

**Problem**: `playwright not available`

**Solution**:
```bash
uv pip install playwright
uv run playwright install chromium
```

**Problem**: Screenshots are blank/white

**Solution**: Increase wait times in script:
```python
# In ScreenshotConfig
wait_after_load=2000,  # Increase from 1000ms
wait_after_interact=1000,  # Increase from 500ms
```

### Episode Data Issues

**Problem**: segmentation_viewer.html shows "No episodes loaded"

**Solution**: Verify episode JSON exists:
```bash
cat ../openadapt-capture/turn-off-nightshift/episodes.json
```

Should contain:
```json
{
  "recording_id": "turn-off-nightshift",
  "episodes": [
    { "episode_id": "episode_001", ... },
    { "episode_id": "episode_002", ... }
  ]
}
```

If empty, copy test data:
```bash
cp test_episodes.json ../openadapt-capture/turn-off-nightshift/episodes.json
```

### Screenshot Size Issues

**Problem**: Screenshots are too large (> 300 KB each)

**Solution**: Reduce viewport or enable compression:
```python
# In ScreenshotConfig
viewport_width=1200,  # Reduce from 1400
viewport_height=800,  # Reduce from 900

# Or in page.screenshot() call:
page.screenshot(path=str(output_path), quality=80, type='jpeg')
```

### Interaction Failures

**Problem**: `interact` function fails with element not found

**Solution**: Add null checks:
```python
interact=lambda page: (
    page.click('.episode-item')
    if page.query_selector('.episode-item')
    else print("Warning: .episode-item not found")
)
```

## Performance Optimization

### Parallel Screenshot Generation

For faster generation, use multiprocessing:

```python
from multiprocessing import Pool

def generate_screenshot_worker(args):
    html_path, output_path, config = args
    return generate_screenshot(html_path, output_path, config)

# In main():
with Pool(processes=4) as pool:
    tasks = [(html, output, config) for config in scenarios]
    screenshots = pool.map(generate_screenshot_worker, tasks)
```

### Headless Browser Optimization

```python
# In generate_screenshot():
browser = p.chromium.launch(
    headless=True,  # Ensure headless mode
    args=[
        '--disable-gpu',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-extensions',
    ]
)
```

### Caching Strategy

Cache generated HTML to avoid regeneration:

```bash
# Check if HTML exists and is newer than source
if [ temp/viewer.html -nt source.py ]; then
    echo "Using cached HTML"
else
    echo "Regenerating HTML..."
    python generate_viewer.py
fi
```

## Maintenance

### Adding New Viewers

1. Add HTML file to repo root (e.g., `new_viewer.html`)

2. Add scenarios to script:
```python
SCREENSHOT_SCENARIOS["new_viewer"] = [
    ScreenshotConfig(name="overview", description="Main view"),
    ScreenshotConfig(name="detail", description="Detail panel"),
]
```

3. Update README with new section

4. Regenerate screenshots:
```bash
python scripts/generate_comprehensive_screenshots.py --viewers all
```

### Updating Existing Screenshots

After modifying viewer HTML/CSS:

```bash
# Regenerate specific viewer
python scripts/generate_comprehensive_screenshots.py --viewers segmentation

# Or all viewers
python scripts/generate_comprehensive_screenshots.py --viewers all

# Commit updated images
git add docs/images/*.png
git commit -m "docs: update screenshots for <feature>"
```

### Screenshot Standards

**Viewport Sizes**:
- Full page: 1400x900 (default)
- Detail panel: 1400x600
- Sidebar focus: 800x900
- Chart/graph: 1400x400

**File Sizes**:
- Target: < 150 KB per screenshot
- Maximum: < 300 KB per screenshot
- Use PNG for UI (sharp text)
- Consider JPEG for photo-heavy content

**Naming Convention**:
- Format: `{viewer}_{scenario}.png`
- Examples: `segmentation_overview.png`, `benchmark_task_list.png`
- Use underscores, not hyphens
- Lowercase only

## Success Metrics

**Coverage**:
- ✅ 4+ viewers documented with screenshots
- ✅ 20+ total screenshots generated
- ✅ All major features visible (search, filters, playback, selection)

**Quality**:
- ✅ Screenshots < 200 KB average
- ✅ Text is crisp and readable
- ✅ Colors consistent with viewer themes
- ✅ No loading indicators or blank states in screenshots

**Automation**:
- ✅ One command regenerates all screenshots
- ✅ CI runs on every PR
- ✅ Script exits with error code on failure
- ✅ Clear error messages for debugging

**Documentation**:
- ✅ README has comprehensive screenshot gallery
- ✅ Each screenshot has descriptive caption
- ✅ Pipeline documented for maintainers
- ✅ Troubleshooting guide available

## Timeline

| Phase | Duration | Cumulative |
|-------|----------|------------|
| 1. Environment Setup | 10 min | 10 min |
| 2. Capture Viewer | 30 min | 40 min |
| 3. Segmentation Viewer | 30 min | 1h 10m |
| 4. Synthetic Demo Viewer | 30 min | 1h 40m |
| 5. Benchmark Viewer | 30 min | 2h 10m |
| 6. README Update | 45 min | 2h 55m |
| 7. CI Integration | 30 min | 3h 25m |
| **Total** | **3h 25m** | |

**Note**: Times assume:
- Dependencies are installable without issues
- Viewers load correctly
- No major debugging needed
- Familiarity with tools

Add 1-2 hours buffer for troubleshooting.

## Next Steps

1. **Install Playwright** (if not already done)
   ```bash
   cd /path/to/openadapt-viewer
   uv pip install ".[screenshots]"
   uv run playwright install chromium
   ```

2. **Generate Capture Viewer Screenshots**
   ```bash
   python scripts/generate_readme_screenshots.py \
     --capture-dir ../openadapt-capture \
     --output-dir docs/images
   ```

3. **Generate All Other Viewer Screenshots**
   ```bash
   python scripts/generate_comprehensive_screenshots.py \
     --viewers all \
     --output-dir docs/images
   ```

4. **Review Generated Screenshots**
   ```bash
   open docs/images/
   ```

5. **Update README**
   - Add new viewer sections
   - Update image references
   - Add captions

6. **Test and Commit**
   ```bash
   git add -A
   git commit -m "feat: comprehensive screenshot pipeline"
   git push
   ```

## Support

For issues or questions:
1. Check troubleshooting section above
2. Review SCREENSHOT_PIPELINE_AUDIT.md for design rationale
3. Check script help: `python scripts/generate_comprehensive_screenshots.py --help`
4. Open issue on GitHub with `documentation` label

## Conclusion

This implementation provides:
- **Comprehensive coverage** of all viewer functionality
- **Systematic approach** to screenshot generation
- **Automated pipeline** for maintainability
- **Clear documentation** for future updates

The enhanced pipeline transforms the README from showing 2 viewers to showcasing **4+ complete viewers** with **20+ screenshots** demonstrating **every major feature**.

**Status**: Ready for implementation - all files created, plan documented, next steps clear.
