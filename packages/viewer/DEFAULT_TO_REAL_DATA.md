# DEFAULT_TO_REAL_DATA Policy

**Effective Date**: January 17, 2026
**Status**: MANDATORY
**Scope**: All OpenAdapt viewer components

## Policy Statement

**CRITICAL**: All OpenAdapt viewers MUST default to using REAL data from actual recordings.

Fake/sample/synthetic data is ONLY permitted for:
1. Unit tests (clearly marked)
2. Explicit demo mode (with clear warnings)
3. Documentation examples (clearly labeled as synthetic)

## Problem Solved

**Before this policy**:
- `test_benchmark_refactored.html` contained fake sample data
- Users couldn't see real system behavior
- Demos were unconvincing and misleading
- No way to verify actual ML segmentation quality

**After this policy**:
- All viewers default to real nightshift recording
- Screenshots show actual macOS System Settings
- Actions reflect real user behavior
- Episode boundaries from ML segmentation
- Duration matches actual recording (6.7 seconds)

## Implementation

### Default Capture Location

```
/path/to/openadapt-capture/turn-off-nightshift/
├── capture.db          # 1,561 real events
├── episodes.json       # 2 ML-segmented episodes
├── screenshots/        # 22 real PNG files
├── video.mp4          # Real screen recording
└── audio.flac         # Real audio
```

### Code Changes

#### 1. Real Data Loader (`real_data_loader.py`)

**NEW**: Loads real capture data from SQLite + episodes.json

```python
from openadapt_viewer.viewers.benchmark.real_data_loader import load_real_capture_data

# Default to nightshift recording
run = load_real_capture_data()

# Or specify different recording
run = load_real_capture_data("/path/to/other/recording")
```

**Features**:
- Reads from `capture.db` (SQLite)
- Loads episodes from `episodes.json`
- Converts to BenchmarkRun format
- Uses real screenshots from recordings
- Preserves actual timestamps and durations

#### 2. Generator Updates (`generator.py`)

```python
def generate_benchmark_html(
    data_path: Optional[Path | str] = None,
    output_path: Path | str = "benchmark_viewer.html",
    standalone: bool = False,
    run_data: Optional[BenchmarkRun] = None,
    use_real_data: bool = True,  # DEFAULT: True
) -> str:
    """Generate benchmark viewer.

    POLICY: ALWAYS defaults to real data from nightshift recording.
    Set use_real_data=False ONLY for unit tests with sample data.
    """
    if run_data is not None:
        run = run_data
    elif data_path is not None:
        # Try to load as capture directory first
        try:
            run = load_real_capture_data(data_path)
        except (FileNotFoundError, ValueError, KeyError):
            # Fall back to benchmark data format
            run = load_benchmark_data(data_path)
    else:
        # DEFAULT: Use real data
        if use_real_data:
            run = load_real_capture_data()
        else:
            # ONLY for unit tests
            run = create_sample_data()
```

#### 3. CLI Updates (`cli.py`)

```bash
# Default: nightshift recording
uv run openadapt-viewer benchmark --output viewer.html

# Specific recording
uv run openadapt-viewer benchmark --data /path/to/recording --output viewer.html
```

**Output**:
```
Generating benchmark viewer with REAL nightshift recording data...
Generated: viewer.html
```

#### 4. Sample Data Warnings (`data.py`)

```python
def create_sample_data(num_tasks: int = 10) -> BenchmarkRun:
    """Create sample benchmark data for testing/demo purposes.

    WARNING: This generates FAKE/SYNTHETIC data.
    POLICY: ONLY use this for unit tests, clearly marked.
    For all other purposes, use load_real_capture_data() from real_data_loader.

    ...
    """
```

## Verification

### Automated Verification

Run verification to ensure real data is being used:

```bash
python3 -c "
import re

with open('test_benchmark_refactored.html') as f:
    html = f.read()

checks = {
    'Title contains Real Capture': 'Real Capture: Turn Off Night Shift Demo' in html,
    'Model is human_demonstration': 'human_demonstration' in html,
    'Has episode_001': 'episode_001' in html,
    'Has episode_002': 'episode_002' in html,
    'Has Navigate to System Settings': 'Navigate to System Settings' in html,
    'Has Disable Night Shift': 'Disable Night Shift' in html,
    'Has real screenshot paths': 'capture_31807990_step_' in html,
    'Has turn-off-nightshift': 'turn-off-nightshift' in html,
    'Total tasks is 2': 'Total Tasks' in html and '>2</div>' in html,
    'No sample data': 'sample_run' not in html,
    'No synthetic data': 'synthetic' not in html.lower(),
}

for check, result in checks.items():
    status = '✓ PASS' if result else '✗ FAIL'
    print(f'{status}: {check}')

all_passed = all(checks.values())
print('Overall:', 'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED')
"
```

### Manual Verification

1. **Check Title**: "Real Capture: Turn Off Night Shift Demo"
2. **Check Model**: "human_demonstration"
3. **Check Tasks**: 2 episodes (not 10 fake tasks)
4. **Check Episodes**:
   - episode_001: "Navigate to System Settings"
   - episode_002: "Disable Night Shift"
5. **Check Screenshots**: Real paths like `capture_31807990_step_0.png`
6. **Check Duration**: ~6.7 seconds (not random)
7. **Check Metadata**:
   - `recording_id`: "turn-off-nightshift"
   - `source`: "real_capture"
   - `platform`: "darwin"
   - `llm_model`: "gpt-4o"
   - `episode_count`: 2

## Real Data Contents

### Episode 1: Navigate to System Settings

**Duration**: 3.5 seconds
**Steps**:
1. Click System Settings icon in dock
2. Wait for Settings window to open
3. Click on Displays in sidebar

**Screenshots**: 3 key frames
**Boundary Confidence**: 0.92
**Coherence Score**: 0.88

### Episode 2: Disable Night Shift

**Duration**: 3.2 seconds
**Steps**:
1. Scroll down in Displays settings
2. Click on Night Shift option
3. Toggle Night Shift switch to off position

**Screenshots**: 3 key frames
**Boundary Confidence**: 0.95
**Coherence Score**: 0.91

### Recording Metadata

- **Recording ID**: turn-off-nightshift
- **Platform**: macOS (darwin)
- **Screen Size**: 1920x1080
- **Total Duration**: 6.7 seconds
- **Total Events**: 1,561
  - screen.frame: 457
  - mouse.move: 1,046
  - mouse.down: 13
  - mouse.up: 13
  - key.down: 16
  - key.up: 16
- **Screenshots**: 22 PNG files
- **ML Segmentation**: gpt-4o
- **Processing**: 2026-01-17T12:00:00
- **Coverage**: 100%
- **Average Confidence**: 93.5%

## Benefits

### 1. Authenticity
- Viewers show real user behavior
- Screenshots are actual macOS UI
- Actions match real interactions
- Timestamps are accurate

### 2. ML Validation
- Episode boundaries from ML segmentation
- Confidence scores visible
- Coherence scores tracked
- Can verify segmentation quality

### 3. Convincing Demos
- Real "Turn Off Night Shift" task
- Actual macOS System Settings
- Genuine user workflow
- Professional presentation

### 4. Testing
- Verify ML pipeline works end-to-end
- Test viewer with real data shapes
- Validate screenshot paths
- Check timestamp handling

### 5. Documentation
- Examples use real recordings
- Screenshots show actual UI
- Behavior matches reality
- Credible use cases

## Migration Guide

### For New Viewers

```python
from openadapt_viewer.viewers.benchmark import generate_benchmark_html

# ✓ CORRECT: Use real data by default
generate_benchmark_html(output_path="viewer.html")

# ✓ CORRECT: Specify recording
generate_benchmark_html(
    data_path="/path/to/recording",
    output_path="viewer.html"
)

# ✗ INCORRECT: Don't use sample data for production
generate_benchmark_html(
    output_path="viewer.html",
    use_real_data=False  # Only for unit tests!
)
```

### For Existing Viewers

1. **Replace sample data calls**:
   ```python
   # OLD
   from openadapt_viewer.viewers.benchmark.data import create_sample_data
   run = create_sample_data()

   # NEW
   from openadapt_viewer.viewers.benchmark.real_data_loader import load_real_capture_data
   run = load_real_capture_data()
   ```

2. **Update CLI commands**:
   ```bash
   # OLD
   uv run openadapt-viewer benchmark --output viewer.html
   # (generated fake data)

   # NEW
   uv run openadapt-viewer benchmark --output viewer.html
   # (generates real nightshift data)
   ```

3. **Update tests**:
   ```python
   # Unit tests can still use sample data
   def test_viewer_with_sample_data():
       run = create_sample_data(num_tasks=5)  # OK for tests
       html = generate_benchmark_html(run_data=run, use_real_data=False)
       assert "task_001" in html
   ```

### For Documentation

1. **Label synthetic examples**:
   ```markdown
   # Example with Synthetic Data (for illustration only)

   Note: This example uses synthetic data for simplicity.
   In production, always use real capture data.
   ```

2. **Prefer real examples**:
   ```markdown
   # Example with Real Data

   This example uses the nightshift recording from openadapt-capture.
   ```

## Enforcement

### Code Reviews

All PRs must verify:
- [ ] No sample data in production code paths
- [ ] Real data used by default
- [ ] Sample data clearly marked for tests
- [ ] Documentation uses real examples

### CI/CD

Automated checks:
```bash
# Check no sample_run in production HTML
grep -q "sample_run" viewer.html && exit 1

# Check real data markers present
grep -q "real_capture" viewer.html || exit 1
grep -q "human_demonstration" viewer.html || exit 1
```

### Testing

Unit tests MUST specify `use_real_data=False` explicitly:
```python
def test_with_sample_data():
    # Explicit opt-in to fake data
    run = create_sample_data(num_tasks=3)
    html = generate_benchmark_html(run_data=run, use_real_data=False)
    assert len(run.tasks) == 3
```

## Future Work

### Additional Real Recordings

Add more real recordings for different tasks:
- Browser automation tasks
- File management operations
- System configuration changes
- Application workflows

### Catalog Integration

```python
from openadapt_viewer.catalog import get_catalog

# Discover all available recordings
catalog = get_catalog()
recordings = catalog.get_all_recordings()

# Load specific recording by name
run = load_real_capture_data(catalog.get_recording("turn-off-nightshift").path)
```

### Recording Selection UI

Add dropdown in viewer to switch between recordings:
```javascript
<select id="recording-selector">
  <option value="turn-off-nightshift" selected>Turn Off Night Shift</option>
  <option value="other-recording">Other Recording</option>
</select>
```

## References

- **Real Data Loader**: `src/openadapt_viewer/viewers/benchmark/real_data_loader.py`
- **Generator**: `src/openadapt_viewer/viewers/benchmark/generator.py`
- **Sample Data** (deprecated for production): `src/openadapt_viewer/viewers/benchmark/data.py`
- **CLI**: `src/openadapt_viewer/cli.py`
- **Nightshift Recording**: `/path/to/openadapt-capture/turn-off-nightshift/`
- **Episodes**: `/path/to/openadapt-capture/turn-off-nightshift/episodes.json`

## Questions & Support

For questions about this policy:
1. Check this document first
2. Review the real_data_loader.py code
3. Examine the nightshift recording structure
4. Test with `uv run openadapt-viewer benchmark`

## Summary

**OLD BEHAVIOR** (UNACCEPTABLE):
```bash
uv run openadapt-viewer benchmark --output viewer.html
# Generated fake sample data with random tasks
```

**NEW BEHAVIOR** (CORRECT):
```bash
uv run openadapt-viewer benchmark --output viewer.html
# Generates real nightshift recording with 2 episodes, 6.7s duration, 22 screenshots
```

**POLICY**: ALWAYS use real data by default. Sample data ONLY for unit tests, clearly marked.
