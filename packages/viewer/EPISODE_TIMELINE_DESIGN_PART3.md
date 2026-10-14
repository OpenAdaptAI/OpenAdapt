# Episode Timeline Integration - Part 3

**Continued from EPISODE_TIMELINE_DESIGN_PART2.md**

## Testing Strategy

### Test Categories

#### 1. Unit Tests (Component Logic)

**File**: `tests/test_episode_timeline.py`

```python
import pytest
from openadapt_viewer.components.episode_timeline import EpisodeTimelineGenerator

def test_calculate_episode_position():
    """Test position calculation for episode labels"""
    generator = EpisodeTimelineGenerator()

    episode = {
        'start_time': 0.0,
        'end_time': 3.5,
        'duration': 3.5
    }

    total_duration = 10.0

    position = generator.calculate_position(episode, total_duration)

    assert position['left'] == '0.0%'
    assert position['width'] == '35.0%'


def test_get_episode_at_time():
    """Test finding which episode contains a timestamp"""
    episodes = [
        {'episode_id': 'ep1', 'start_time': 0.0, 'end_time': 3.5},
        {'episode_id': 'ep2', 'start_time': 3.5, 'end_time': 6.7},
    ]

    generator = EpisodeTimelineGenerator(episodes=episodes)

    assert generator.get_episode_at_time(2.0)['episode_id'] == 'ep1'
    assert generator.get_episode_at_time(5.0)['episode_id'] == 'ep2'
    assert generator.get_episode_at_time(10.0) is None


def test_format_duration():
    """Test duration formatting"""
    generator = EpisodeTimelineGenerator()

    assert generator.format_duration(3.5) == '3.5s'
    assert generator.format_duration(65.0) == '1m 5s'
    assert generator.format_duration(125.3) == '2m 5s'


def test_truncate_episode_name():
    """Test label truncation"""
    generator = EpisodeTimelineGenerator(config={'label_truncate': 20})

    short_name = "Short Name"
    assert generator.truncate_text(short_name, 20) == "Short Name"

    long_name = "This is a very long episode name"
    truncated = generator.truncate_text(long_name, 20)
    assert len(truncated) == 20
    assert truncated.endswith('...')


def test_episode_color_rotation():
    """Test that episode colors cycle through palette"""
    generator = EpisodeTimelineGenerator()

    assert 'episode-1-bg' in generator.get_episode_color(0)
    assert 'episode-2-bg' in generator.get_episode_color(1)
    assert 'episode-1-bg' in generator.get_episode_color(5)  # Cycles back


def test_graceful_degradation_no_episodes():
    """Test that component handles empty episode list"""
    generator = EpisodeTimelineGenerator(episodes=[])

    html = generator.render()

    assert 'No episodes available' in html or html == ''


def test_episode_boundary_confidence():
    """Test rendering confidence indicators"""
    episodes = [
        {
            'episode_id': 'ep1',
            'name': 'Test Episode',
            'start_time': 0.0,
            'end_time': 3.5,
            'boundary_confidence': 0.95
        }
    ]

    generator = EpisodeTimelineGenerator(episodes=episodes)
    html = generator.render()

    assert '95%' in html or 'confidence' in html.lower()
```

#### 2. Integration Tests (Browser Automation)

**File**: `tests/test_episode_timeline_integration.py`

```python
import pytest
from playwright.sync_api import Page, expect

@pytest.fixture
def episode_viewer_page(page: Page) -> Page:
    """Load capture viewer with test episode data"""
    page.goto('file:///path/to/openadapt-viewer/capture_viewer.html?episodes=test_episodes.json')
    page.wait_for_selector('.oa-episode-timeline')
    return page


def test_episode_labels_rendered(episode_viewer_page: Page):
    """Test that episode labels appear on timeline"""
    labels = episode_viewer_page.locator('.oa-episode-label')

    expect(labels).to_have_count(2)

    # Check label text
    expect(labels.nth(0)).to_contain_text('Navigate to System Settings')
    expect(labels.nth(1)).to_contain_text('Disable Night Shift')


def test_click_episode_label_seeks(episode_viewer_page: Page):
    """Test that clicking episode label jumps to that episode"""
    # Get initial position
    initial_position = episode_viewer_page.locator('.oa-current-marker')
    initial_left = initial_position.get_attribute('style')

    # Click second episode label
    episode_viewer_page.locator('.oa-episode-label').nth(1).click()

    # Wait for seek animation
    episode_viewer_page.wait_for_timeout(500)

    # Check position changed
    new_left = initial_position.get_attribute('style')
    assert new_left != initial_left

    # Check current episode indicator updated
    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 2 of 2')


def test_episode_tooltip_on_hover(episode_viewer_page: Page):
    """Test that hovering episode label shows tooltip"""
    label = episode_viewer_page.locator('.oa-episode-label').nth(0)

    # Hover over label
    label.hover()

    # Wait for tooltip
    episode_viewer_page.wait_for_selector('.oa-episode-tooltip', state='visible')

    tooltip = episode_viewer_page.locator('.oa-episode-tooltip')

    # Check tooltip content
    expect(tooltip).to_contain_text('Navigate to System Settings')
    expect(tooltip).to_contain_text('3.5s')
    expect(tooltip).to_contain_text('3 steps')


def test_prev_next_episode_buttons(episode_viewer_page: Page):
    """Test episode navigation buttons"""
    prev_btn = episode_viewer_page.get_by_label('Go to previous episode')
    next_btn = episode_viewer_page.get_by_label('Go to next episode')

    # Initially at episode 1, prev should be disabled
    expect(prev_btn).to_be_disabled()
    expect(next_btn).to_be_enabled()

    # Click next
    next_btn.click()
    episode_viewer_page.wait_for_timeout(300)

    # Now at episode 2
    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 2')
    expect(prev_btn).to_be_enabled()
    expect(next_btn).to_be_disabled()

    # Click prev
    prev_btn.click()
    episode_viewer_page.wait_for_timeout(300)

    # Back to episode 1
    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 1')


def test_keyboard_navigation(episode_viewer_page: Page):
    """Test keyboard shortcuts for episode navigation"""
    # Press right arrow to go to next episode
    episode_viewer_page.keyboard.press('ArrowRight')
    episode_viewer_page.wait_for_timeout(300)

    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 2')

    # Press left arrow to go back
    episode_viewer_page.keyboard.press('ArrowLeft')
    episode_viewer_page.wait_for_timeout(300)

    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 1')

    # Press 2 to jump to episode 2
    episode_viewer_page.keyboard.press('2')
    episode_viewer_page.wait_for_timeout(300)

    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 2')

    # Press Home to go to first episode
    episode_viewer_page.keyboard.press('Home')
    episode_viewer_page.wait_for_timeout(300)

    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 1')


def test_episode_boundary_markers(episode_viewer_page: Page):
    """Test that episode boundaries are visible"""
    boundaries = episode_viewer_page.locator('.oa-episode-boundary')

    # Should have N-1 boundaries for N episodes (2 episodes = 1 boundary)
    expect(boundaries).to_have_count(1)

    # Check boundary is positioned correctly (at 52.2% for 3.5s / 6.7s)
    boundary_style = boundaries.nth(0).get_attribute('style')
    assert 'left: 52' in boundary_style  # Approximately 52%


def test_current_episode_highlight(episode_viewer_page: Page):
    """Test that current episode label is highlighted"""
    # Start at episode 1
    episode_1_label = episode_viewer_page.locator('.oa-episode-label').nth(0)
    expect(episode_1_label).to_have_class(/oa-episode-current/)

    # Navigate to episode 2
    episode_viewer_page.locator('.oa-episode-label').nth(1).click()
    episode_viewer_page.wait_for_timeout(300)

    # Episode 2 should now be highlighted
    episode_2_label = episode_viewer_page.locator('.oa-episode-label').nth(1)
    expect(episode_2_label).to_have_class(/oa-episode-current/)

    # Episode 1 should NOT be highlighted
    expect(episode_1_label).not_to_have_class(/oa-episode-current/)


def test_timeline_click_seeks(episode_viewer_page: Page):
    """Test that clicking timeline seeks to that position"""
    track = episode_viewer_page.locator('.oa-timeline-track')

    # Get track dimensions
    track_box = track.bounding_box()

    # Click at 75% position
    click_x = track_box['x'] + (track_box['width'] * 0.75)
    click_y = track_box['y'] + (track_box['height'] / 2)

    episode_viewer_page.mouse.click(click_x, click_y)
    episode_viewer_page.wait_for_timeout(300)

    # Should now be in episode 2 (starts at 52%)
    expect(episode_viewer_page.locator('.oa-episode-current-indicator')).to_contain_text('Episode 2')


def test_episode_color_coding(episode_viewer_page: Page):
    """Test that episodes have different colors"""
    label_1 = episode_viewer_page.locator('.oa-episode-label').nth(0)
    label_2 = episode_viewer_page.locator('.oa-episode-label').nth(1)

    style_1 = label_1.get_attribute('style')
    style_2 = label_2.get_attribute('style')

    # Check that backgrounds are different
    assert 'episode-1-bg' in style_1 or 'gradient' in style_1
    assert 'episode-2-bg' in style_2 or 'gradient' in style_2
    assert style_1 != style_2
```

#### 3. Visual Regression Tests

**File**: `tests/test_episode_timeline_visual.py`

```python
import pytest
from playwright.sync_api import Page

@pytest.fixture
def screenshots_dir():
    return 'tests/visual_regression/screenshots'


def test_episode_timeline_default_state(episode_viewer_page: Page, screenshots_dir):
    """Capture screenshot of timeline in default state"""
    timeline = episode_viewer_page.locator('.oa-episode-timeline')

    timeline.screenshot(path=f'{screenshots_dir}/timeline_default.png')


def test_episode_timeline_hover_state(episode_viewer_page: Page, screenshots_dir):
    """Capture screenshot of timeline with hover state"""
    label = episode_viewer_page.locator('.oa-episode-label').nth(0)
    label.hover()

    episode_viewer_page.wait_for_timeout(300)  # Wait for hover animation

    timeline = episode_viewer_page.locator('.oa-episode-timeline')
    timeline.screenshot(path=f'{screenshots_dir}/timeline_hover.png')


def test_episode_timeline_current_state(episode_viewer_page: Page, screenshots_dir):
    """Capture screenshot with episode 2 active"""
    episode_viewer_page.locator('.oa-episode-label').nth(1).click()
    episode_viewer_page.wait_for_timeout(500)

    timeline = episode_viewer_page.locator('.oa-episode-timeline')
    timeline.screenshot(path=f'{screenshots_dir}/timeline_episode_2.png')


def test_episode_timeline_mobile(episode_viewer_page: Page, screenshots_dir):
    """Capture screenshot of mobile layout"""
    episode_viewer_page.set_viewport_size({'width': 375, 'height': 667})
    episode_viewer_page.wait_for_timeout(300)

    timeline = episode_viewer_page.locator('.oa-episode-timeline')
    timeline.screenshot(path=f'{screenshots_dir}/timeline_mobile.png')


def test_episode_timeline_many_episodes(page: Page, screenshots_dir):
    """Test timeline with many episodes (10+)"""
    # Load fixture with 10 episodes
    page.goto('file:///path/to/openadapt-viewer/test_many_episodes.html')
    page.wait_for_selector('.oa-episode-timeline')

    timeline = page.locator('.oa-episode-timeline')
    timeline.screenshot(path=f'{screenshots_dir}/timeline_many_episodes.png')
```

#### 4. Performance Tests

```python
import pytest
import time
from playwright.sync_api import Page


def test_timeline_render_performance(episode_viewer_page: Page):
    """Test that timeline renders quickly with typical episode count"""
    start = time.time()

    episode_viewer_page.goto('file:///path/to/openadapt-viewer/capture_viewer.html?episodes=test_episodes.json')
    episode_viewer_page.wait_for_selector('.oa-episode-timeline')

    end = time.time()
    render_time = end - start

    # Should render in < 2 seconds
    assert render_time < 2.0


def test_seek_animation_performance(episode_viewer_page: Page):
    """Test that seek animations are smooth (no jank)"""
    # Enable performance profiling
    episode_viewer_page.evaluate('performance.mark("seek-start")')

    # Click to seek
    episode_viewer_page.locator('.oa-episode-label').nth(1).click()
    episode_viewer_page.wait_for_timeout(300)  # Wait for animation

    episode_viewer_page.evaluate('performance.mark("seek-end")')

    # Measure performance
    duration = episode_viewer_page.evaluate('''
        performance.measure("seek-duration", "seek-start", "seek-end");
        const measure = performance.getEntriesByName("seek-duration")[0];
        return measure.duration;
    ''')

    # Animation should complete in < 500ms
    assert duration < 500


def test_many_episodes_performance(page: Page):
    """Test performance with 20 episodes"""
    start = time.time()

    page.goto('file:///path/to/openadapt-viewer/test_many_episodes.html')
    page.wait_for_selector('.oa-episode-timeline')

    # Interact with timeline
    page.locator('.oa-episode-label').nth(10).click()
    page.wait_for_timeout(300)

    end = time.time()
    total_time = end - start

    # Should still be responsive with many episodes
    assert total_time < 3.0
```

### Test Coverage Goals

- **Unit Tests**: 90% code coverage
- **Integration Tests**: All user flows covered
- **Visual Tests**: Key states documented
- **Performance Tests**: Render time < 2s, seek time < 500ms

### Running Tests

```bash
# All tests
uv run pytest tests/test_episode_timeline*.py -v

# Unit tests only (fast)
uv run pytest tests/test_episode_timeline.py -v

# Integration tests (requires Playwright)
uv run pytest tests/test_episode_timeline_integration.py -v

# Visual regression tests
uv run pytest tests/test_episode_timeline_visual.py -v

# Performance tests
uv run pytest tests/test_episode_timeline_integration.py -k performance -v

# With coverage
uv run pytest tests/ --cov=openadapt_viewer.components.episode_timeline --cov-report=html
```

---

## Accessibility & Responsiveness

### WCAG 2.1 AA Compliance

#### 1. Keyboard Navigation

**Requirements**:
- ✅ All interactive elements focusable via Tab
- ✅ Focus indicators visible (2px outline)
- ✅ Keyboard shortcuts documented
- ✅ No keyboard traps

**Implementation**:
```html
<!-- Episode label is keyboard-accessible -->
<div class="oa-episode-label"
     role="button"
     tabindex="0"
     aria-label="Jump to episode 2: Disable Night Shift"
     @keydown="handleKeydown">
  Disable Night Shift
</div>
```

```css
.oa-episode-label:focus {
  outline: 2px solid var(--oa-accent);
  outline-offset: 2px;
}
```

#### 2. Screen Reader Support

**ARIA Labels**:
```html
<!-- Timeline track -->
<div class="oa-timeline-track"
     role="slider"
     aria-label="Playback timeline"
     aria-valuenow="4.2"
     aria-valuemin="0"
     aria-valuemax="6.7"
     aria-valuetext="Episode 2: Disable Night Shift, 4.2 seconds">
</div>

<!-- Episode boundary -->
<div class="oa-episode-boundary"
     role="separator"
     aria-label="Boundary between Navigate to Settings and Disable Night Shift">
</div>

<!-- Episode controls -->
<div class="oa-episode-controls" role="group" aria-label="Episode navigation">
  <button aria-label="Go to previous episode">...</button>
  <button aria-label="Go to next episode">...</button>
</div>
```

**Live Regions** (announce changes):
```html
<div role="status" aria-live="polite" aria-atomic="true" class="sr-only">
  <span id="episode-announcer"></span>
</div>

<script>
// Update announcer when episode changes
function announceEpisodeChange(episode) {
  const announcer = document.getElementById('episode-announcer');
  announcer.textContent = `Entering episode ${episode.name}. ${episode.steps.length} steps.`;
}
</script>
```

#### 3. Color Contrast

**Requirements**:
- Text on background: 4.5:1 minimum
- Large text (18pt+): 3:1 minimum

**Implementation**:
```css
/* Episode label text on gradient background */
.oa-episode-label {
  color: white;  /* Ensure sufficient contrast on all gradient backgrounds */
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);  /* Enhance readability */
}

/* Current episode indicator */
.oa-episode-current-indicator strong {
  color: var(--oa-accent);  /* #00d4aa on dark background = 7.2:1 ✓ */
}
```

#### 4. Focus Management

**On Episode Change**:
```javascript
function seekToEpisode(episode) {
  // Seek to episode
  this.onSeek(episode.start_time);

  // Announce change
  announceEpisodeChange(episode);

  // Optionally move focus to play button
  // document.querySelector('[aria-label="Play"]').focus();
}
```

#### 5. Alternative Text

**For visual elements**:
```html
<!-- Episode boundary marker -->
<div class="oa-episode-boundary"
     role="separator"
     aria-label="Episode 1 ends at 3.5 seconds">
</div>

<!-- Current position marker -->
<div class="oa-current-marker"
     role="slider"
     aria-label="Current playback position: 4.2 seconds, in episode Disable Night Shift">
</div>
```

### Responsive Design

#### Breakpoints

```css
/* Desktop (default) */
@media (min-width: 1024px) {
  .oa-episode-labels {
    /* Labels side-by-side */
  }
}

/* Tablet */
@media (max-width: 1023px) and (min-width: 768px) {
  .oa-episode-label {
    font-size: 11px;
  }

  .oa-episode-label-duration {
    font-size: 10px;
  }
}

/* Mobile */
@media (max-width: 767px) {
  .oa-episode-timeline {
    padding: var(--oa-space-sm);
  }

  .oa-episode-labels {
    /* Stack labels if too many */
    flex-direction: column;
    height: auto;
  }

  .oa-episode-label {
    position: relative !important;
    width: 100% !important;
    left: 0 !important;
    margin-bottom: 4px;
  }

  .oa-episode-current-indicator {
    font-size: 12px;
    flex-wrap: wrap;
  }

  .oa-episode-controls {
    flex-direction: column;
  }

  .oa-episode-nav-btn span {
    /* Hide text, show only icons */
    display: none;
  }
}

/* Small Mobile */
@media (max-width: 480px) {
  .oa-episode-label-duration {
    display: none;
  }

  .oa-episode-tooltip {
    max-width: 250px;
    font-size: 11px;
  }
}
```

#### Touch Interactions

```javascript
class EpisodeTimeline {
  attachTouchListeners() {
    const track = this.container.querySelector('.oa-timeline-track');

    let touchStartX = 0;
    let touchStartTime = 0;

    track.addEventListener('touchstart', (e) => {
      touchStartX = e.touches[0].clientX;
      touchStartTime = Date.now();
    });

    track.addEventListener('touchend', (e) => {
      const touchEndX = e.changedTouches[0].clientX;
      const touchEndTime = Date.now();

      const deltaX = touchEndX - touchStartX;
      const deltaTime = touchEndTime - touchStartTime;

      // Swipe detection
      if (Math.abs(deltaX) > 50 && deltaTime < 300) {
        if (deltaX > 0) {
          // Swipe right = previous episode
          this.prevEpisode();
        } else {
          // Swipe left = next episode
          this.nextEpisode();
        }
      }
    });

    // Long-press for details
    this.container.querySelectorAll('.oa-episode-label').forEach(label => {
      let longPressTimer;

      label.addEventListener('touchstart', (e) => {
        longPressTimer = setTimeout(() => {
          // Show tooltip on long-press
          this.showTooltip(this.getEpisodeById(label.dataset.episodeId), e);
        }, 500);
      });

      label.addEventListener('touchend', () => {
        clearTimeout(longPressTimer);
      });
    });
  }
}
```

---

## Advanced Features (Phase 4)

### Feature 1: Episode Bookmarks

**User Story**: As a user, I want to bookmark specific moments within episodes so I can quickly return to important actions.

**UI Design**:
```
Timeline with bookmarks:
┌─────────────────────────────────────────────────────────┐
│  [Episode 1] ⭐ [Episode 2] ⭐⭐                          │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│        ↑                    ↑↑                          │
│     bookmark 1           bookmarks 2,3                  │
└─────────────────────────────────────────────────────────┘

Bookmark Panel:
┌─────────────────────────────────────────────────────────┐
│ My Bookmarks (3)                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ ⭐ Click System Settings icon                        │ │
│ │    Episode 1 • 0:01.2 • Added Jan 17                │ │
│ │    [Jump] [Edit] [Delete]                           │ │
│ ├─────────────────────────────────────────────────────┤ │
│ │ ⭐ Scroll down in settings                           │ │
│ │    Episode 2 • 0:03.8 • Added Jan 17                │ │
│ │    [Jump] [Edit] [Delete]                           │ │
│ └─────────────────────────────────────────────────────┘ │
│ [+ Add Bookmark at Current Position]                    │
└─────────────────────────────────────────────────────────┘
```

**Implementation**:
```javascript
class EpisodeBookmarks {
  constructor(storageKey = 'oa-episode-bookmarks') {
    this.storageKey = storageKey;
    this.bookmarks = this.loadBookmarks();
  }

  addBookmark(episodeId, timestamp, note = '') {
    const bookmark = {
      id: this.generateId(),
      episodeId,
      timestamp,
      note,
      createdAt: new Date().toISOString()
    };

    this.bookmarks.push(bookmark);
    this.saveBookmarks();

    return bookmark;
  }

  removeBookmark(bookmarkId) {
    this.bookmarks = this.bookmarks.filter(b => b.id !== bookmarkId);
    this.saveBookmarks();
  }

  getBookmarksForEpisode(episodeId) {
    return this.bookmarks.filter(b => b.episodeId === episodeId);
  }

  loadBookmarks() {
    try {
      const stored = localStorage.getItem(this.storageKey);
      return stored ? JSON.parse(stored) : [];
    } catch (error) {
      console.error('Failed to load bookmarks:', error);
      return [];
    }
  }

  saveBookmarks() {
    try {
      localStorage.setItem(this.storageKey, JSON.stringify(this.bookmarks));
    } catch (error) {
      console.error('Failed to save bookmarks:', error);
    }
  }

  generateId() {
    return `bookmark_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }
}
```

### Feature 2: Episode Comparison Mode

**User Story**: As a user, I want to compare two recordings side-by-side to see how episode structure differs.

**UI Design**:
```
┌──────────────────────────────────────────────────────────────┐
│ Recording Comparison                                          │
├───────────────────────────────┬───────────────────────────────┤
│ Recording A                   │ Recording B                   │
│ turn-off-nightshift           │ turn-off-nightshift-v2        │
│                               │                               │
│ [Ep1] [Ep2]                   │ [Ep1] [Ep2] [Ep3]            │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━ │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│                               │                               │
│ • 2 episodes                  │ • 3 episodes                  │
│ • 6.7s total                  │ • 8.2s total                  │
│ • 100% coverage               │ • 95% coverage                │
│                               │                               │
│ Differences:                                                  │
│ - Recording B has additional "Close window" episode           │
│ - Recording B 22% longer duration                             │
│ - Episode names 85% similar                                   │
└──────────────────────────────────────────────────────────────┘
```

### Feature 3: Episode Analytics

**User Story**: As a developer, I want to track which episodes users view most to understand usage patterns.

**Metrics to Track**:
- Episode view count (how many times each episode was viewed)
- Episode completion rate (% of users who viewed entire episode)
- Episode skip rate (% of users who skipped over episode)
- Average time spent per episode
- Most common navigation paths (Ep1 → Ep2 vs Ep1 → Ep3)

**Dashboard**:
```
┌──────────────────────────────────────────────────────────────┐
│ Episode Analytics - turn-off-nightshift                       │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  Episode Performance:                                         │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ 1. Navigate to Settings       Views: 145   ██████████ │ │
│  │    Completion: 92%  Skip Rate: 5%                      │ │
│  ├────────────────────────────────────────────────────────┤ │
│  │ 2. Disable Night Shift        Views: 134   █████████  │ │
│  │    Completion: 87%  Skip Rate: 8%                      │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                               │
│  Navigation Paths:                                            │
│  - Ep1 → Ep2: 85% (most common)                              │
│  - Ep1 only: 10%                                              │
│  - Ep2 only: 5%                                               │
│                                                               │
│  Engagement:                                                  │
│  - Avg session: 12.3s                                         │
│  - Replay rate: 23%                                           │
│  - Bookmark rate: 15%                                         │
└──────────────────────────────────────────────────────────────┘
```

### Feature 4: Episode Refinement

**User Story**: As a user, I want to adjust episode boundaries if the auto-segmentation is slightly off.

**UI Design**:
```
┌──────────────────────────────────────────────────────────────┐
│ Refine Episodes                                               │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  [Episode 1] [◄ Adjust ►] [Episode 2]                        │
│  ━━━━━━━━━━━━|━━━━━━━━━━━━━━━━━━━━━━━━━                    │
│              ↕                                                │
│         Drag to adjust                                        │
│                                                               │
│  Episode 1: 0.0s → 3.5s (3.5s)                               │
│  Episode 2: 3.5s → 6.7s (3.2s)                               │
│                                                               │
│  Suggested adjustment: Move boundary to 3.2s                  │
│  Reason: Better alignment with action completion              │
│                                                               │
│  [Apply Suggestion] [Reset] [Save Changes]                   │
└──────────────────────────────────────────────────────────────┘
```

**Implementation**:
```javascript
class EpisodeRefinement {
  constructor(episodes, onUpdate) {
    this.episodes = episodes;
    this.onUpdate = onUpdate;
    this.enableDragging();
  }

  enableDragging() {
    const boundaries = document.querySelectorAll('.oa-episode-boundary');

    boundaries.forEach((boundary, index) => {
      boundary.style.cursor = 'ew-resize';

      let isDragging = false;
      let startX = 0;
      let startTime = this.episodes[index].end_time;

      boundary.addEventListener('mousedown', (e) => {
        isDragging = true;
        startX = e.clientX;
        e.preventDefault();
      });

      document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;

        const deltaX = e.clientX - startX;
        const track = boundary.closest('.oa-timeline-track');
        const trackWidth = track.offsetWidth;
        const totalDuration = this.getTotalDuration();

        // Calculate new boundary time
        const deltaTime = (deltaX / trackWidth) * totalDuration;
        const newTime = Math.max(0, Math.min(totalDuration, startTime + deltaTime));

        // Update boundary position
        const newPercent = (newTime / totalDuration) * 100;
        boundary.style.left = `${newPercent}%`;

        // Update episode times
        this.episodes[index].end_time = newTime;
        this.episodes[index + 1].start_time = newTime;
      });

      document.addEventListener('mouseup', () => {
        if (isDragging) {
          isDragging = false;
          this.onUpdate(this.episodes);
        }
      });
    });
  }

  suggestAdjustments() {
    // Analyze episodes and suggest boundary adjustments
    const suggestions = [];

    for (let i = 0; i < this.episodes.length - 1; i++) {
      const ep1 = this.episodes[i];
      const ep2 = this.episodes[i + 1];

      // Check if boundary falls mid-action
      // (Implementation depends on step/action data)
      const suggestion = this.analyzeBoundary(ep1, ep2);

      if (suggestion) {
        suggestions.push(suggestion);
      }
    }

    return suggestions;
  }
}
```

---

## Appendices

### Appendix A: File Checklist

**New Files to Create**:
- [ ] `src/openadapt_viewer/components/episode_timeline.js`
- [ ] `src/openadapt_viewer/components/episode_timeline.py`
- [ ] `src/openadapt_viewer/styles/episode_timeline.css`
- [ ] `test_episode_timeline.html` (interactive demo)
- [ ] `tests/test_episode_timeline.py` (unit tests)
- [ ] `tests/test_episode_timeline_integration.py` (integration tests)
- [ ] `tests/test_episode_timeline_visual.py` (visual regression)

**Files to Modify**:
- [ ] `capture_viewer.html` (add episode timeline)
- [ ] `segmentation_viewer.html` (add mini timeline previews)
- [ ] `synthetic_demo_viewer.html` (optional: add episode view)
- [ ] `benchmark_viewer.html` (optional: add episode timeline for tasks)

### Appendix B: API Reference

**EpisodeTimeline Class**:

```typescript
class EpisodeTimeline {
  constructor(options: EpisodeTimelineOptions);

  // Core methods
  render(): void;
  update(updates: Partial<EpisodeTimelineOptions>): void;
  destroy(): void;

  // Episode navigation
  seekToEpisode(episodeId: string): void;
  prevEpisode(): void;
  nextEpisode(): void;
  getCurrentEpisode(): Episode | null;
  getEpisodeAtTime(time: number): Episode | null;

  // Utility methods
  getEpisodeColor(index: number): string;
  formatDuration(seconds: number): string;
  calculatePosition(episode: Episode, totalDuration: number): Position;

  // Event handlers
  on(event: string, callback: Function): void;
  off(event: string, callback: Function): void;
}

interface EpisodeTimelineOptions {
  container: HTMLElement;
  episodes: Episode[];
  currentTime: number;
  totalDuration: number;
  onSeek: (time: number) => void;
  onEpisodeChange?: (episode: Episode) => void;
  config?: EpisodeTimelineConfig;
}

interface Episode {
  episode_id: string;
  name: string;
  description: string;
  start_time: number;
  end_time: number;
  duration: number;
  steps: string[];
  boundary_confidence?: number;
  coherence_score?: number;
  screenshots?: {
    thumbnail?: string;
    key_frames?: KeyFrame[];
  };
}

interface EpisodeTimelineConfig {
  showLabels?: boolean;
  showBoundaries?: boolean;
  enableClickNavigation?: boolean;
  enableAutoAdvance?: boolean;
  colorScheme?: 'auto' | 'blue' | 'purple' | 'custom';
  labelTruncate?: number;
}
```

### Appendix C: CSS Class Reference

**Episode Timeline Classes**:

| Class | Purpose |
|-------|---------|
| `.oa-episode-timeline` | Container for entire component |
| `.oa-episode-current-indicator` | Shows current episode info |
| `.oa-episode-labels` | Container for episode labels |
| `.oa-episode-label` | Individual episode label |
| `.oa-episode-current` | Current episode modifier |
| `.oa-episode-past` | Past episode modifier |
| `.oa-episode-future` | Future episode modifier |
| `.oa-timeline-track` | Timeline track container |
| `.oa-episode-segment` | Episode segment on timeline |
| `.oa-episode-boundary` | Episode boundary marker |
| `.oa-current-marker` | Current playback position |
| `.oa-episode-controls` | Episode navigation controls |
| `.oa-episode-nav-btn` | Prev/Next buttons |
| `.oa-episode-tooltip` | Hover tooltip |

### Appendix D: Browser Compatibility

**Minimum Requirements**:
- Chrome/Edge: 90+
- Firefox: 88+
- Safari: 14+
- Mobile Safari: iOS 14+
- Chrome Android: 90+

**Fallbacks**:
- CSS Grid → Flexbox (IE11)
- CSS Variables → Hardcoded colors (IE11)
- IntersectionObserver → Polyfill
- ResizeObserver → Polyfill

### Appendix E: Performance Benchmarks

**Target Metrics**:
- Initial render: < 500ms (10 episodes)
- Seek animation: < 300ms
- Episode transition: < 200ms
- Memory footprint: < 10MB
- Frame rate: 60fps during animations

**Optimization Techniques**:
- Virtual scrolling for 20+ episodes
- Lazy render episode labels (only visible ones)
- Debounce timeline hover events
- Use CSS transforms (not left/top) for animations
- RequestAnimationframe for smooth updates

### Appendix F: Migration Guide

**For Existing Viewers**:

1. **Add Episode Data**: Ensure episodes are available (JSON file or catalog)

2. **Include CSS**: Link episode timeline styles
   ```html
   <link rel="stylesheet" href="styles/episode_timeline.css">
   ```

3. **Include JS**: Add component script
   ```html
   <script src="components/episode_timeline.js"></script>
   ```

4. **Initialize Timeline**: Add to viewer
   ```javascript
   const timeline = new EpisodeTimeline({
     container: document.getElementById('timeline-container'),
     episodes: loadedEpisodes,
     currentTime: 0,
     totalDuration: recordingDuration,
     onSeek: (time) => { seekToTime(time); }
   });
   ```

5. **Update on Playback**: Sync current time
   ```javascript
   // In playback loop
   timeline.update({ currentTime: player.currentTime });
   ```

---

## Summary

This comprehensive design document covers:

✅ **Visual Design**: Colors, typography, spacing, states
✅ **Component Architecture**: Modular, reusable, extensible
✅ **Data Flow**: Three options with fallbacks
✅ **Implementation Phases**: 4 phases from MVP to advanced
✅ **Integration Guide**: Step-by-step for capture/segmentation viewers
✅ **User Interactions**: 6 detailed user flows with expected behavior
✅ **Technical Implementation**: Complete JS component + CSS
✅ **Testing Strategy**: Unit, integration, visual, performance tests
✅ **Accessibility**: WCAG 2.1 AA compliant with ARIA support
✅ **Responsiveness**: Desktop, tablet, mobile layouts
✅ **Advanced Features**: Bookmarks, comparison, analytics, refinement

**Next Steps**:
1. Review design with team
2. Create prototype/mockup
3. Implement Phase 1 (MVP)
4. Write tests
5. Integrate into capture viewer
6. Gather user feedback
7. Iterate and improve

**Estimated Timeline**:
- Phase 1 (MVP): 1 week
- Phase 2 (Enhanced UX): 1 week
- Phase 3 (Polish): 1 week
- Phase 4 (Advanced): 2-3 weeks

Total: 5-6 weeks for full implementation

---

**Document Version**: 1.0
**Last Updated**: 2026-01-17
**Authors**: OpenAdapt Viewer Team
**Status**: Ready for Review
