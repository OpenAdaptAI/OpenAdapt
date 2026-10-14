# Episode Timeline Integration - Part 2

**Continued from EPISODE_TIMELINE_DESIGN.md**

## User Interaction Patterns

### Flow 1: Browse Episodes in Capture Viewer

```
┌─────────────────────────────────────────────────────────┐
│ 1. User opens capture viewer                            │
│    URL: viewer.html?recording=turn-off-nightshift       │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 2. Viewer loads and displays:                           │
│    - Playback controls                                  │
│    - Timeline with 2 episode labels:                    │
│      [Navigate to Settings] [Disable Night Shift]       │
│    - Current step: 1 of 5                               │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 3. User clicks "Disable Night Shift" label              │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 4. Viewer smoothly seeks to 3.5s (episode start)        │
│    - Animation: Progress bar slides to new position     │
│    - Screenshot updates to step at 3.5s                 │
│    - "Disable Night Shift" label glows (current)        │
│    - Episode indicator updates: "Episode 2 of 2"        │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 5. User clicks Play                                      │
│    - Playback starts from episode 2                     │
│    - Steps advance: scroll, click Night Shift, toggle   │
└─────────────────────────────────────────────────────────┘
```

**Expected Behavior**:
- Seek animation: 300ms ease-in-out
- Episode label highlight appears immediately
- Screenshot updates after seek completes
- No page reload or flash

### Flow 2: Episode Auto-Advance

```
┌─────────────────────────────────────────────────────────┐
│ 1. User watching episode 1 (Navigate to Settings)       │
│    - Current position: 3.2s                             │
│    - Approaching episode boundary at 3.5s               │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 2. Playback crosses 3.5s boundary                       │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 3. Toast notification appears (top-right):              │
│    ┌─────────────────────────────────────────────┐     │
│    │ 🔹 Entering Episode 2                        │     │
│    │ "Disable Night Shift"                        │     │
│    │ [View Details] [×]                           │     │
│    └─────────────────────────────────────────────┘     │
│    - Fades in over 200ms                                │
│    - Auto-dismisses after 4 seconds                     │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 4. Timeline updates:                                     │
│    - Episode 1 label dims (opacity: 0.5)                │
│    - Episode 2 label brightens (opacity: 1.0)           │
│    - Episode 2 label glows (box-shadow)                 │
│    - Episode indicator: "Episode 2 of 2"                │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 5. Playback continues in episode 2                      │
└─────────────────────────────────────────────────────────┘
```

**Optional**: Auto-pause at episode boundary with prompt:
```
┌─────────────────────────────────────────────────────────┐
│ Episode Complete: "Navigate to Settings" ✓               │
│                                                          │
│ Next: "Disable Night Shift"                             │
│                                                          │
│ [Continue] [Replay Episode] [Jump to Episode...]        │
└─────────────────────────────────────────────────────────┘
```

### Flow 3: Navigate from Segmentation Viewer

```
┌─────────────────────────────────────────────────────────┐
│ 1. User in segmentation viewer                          │
│    - Viewing episode grid (5 episodes)                  │
│    - Clicks "Disable Night Shift" card                  │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 2. Episode detail view expands                          │
│    - Shows description, steps, key frames               │
│    - Link: "View Full Recording >" button visible       │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 3. User clicks "View Full Recording"                    │
│    - Link has URL parameters:                           │
│      ?highlight_start=3.5&highlight_end=6.7             │
│      &episode_name=Disable+Night+Shift                  │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 4. Capture viewer opens in new tab                      │
│    - Episode context banner shown (yellow highlight):   │
│      "Viewing Episode: Disable Night Shift (3.5s-6.7s)" │
│    - Timeline highlights episode segment                │
│    - Playback positioned at episode start (3.5s)        │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ 5. User reviews episode in context of full recording    │
│    - Can see surrounding episodes                       │
│    - Can navigate before/after highlighted episode      │
└─────────────────────────────────────────────────────────┘
```

**URL Parameters**:
- `highlight_start`: Start time of episode (seconds)
- `highlight_end`: End time of episode (seconds)
- `episode_name`: Episode name for display
- `autoplay`: Start playing immediately (optional)

### Flow 4: Keyboard Navigation

```
User presses keys (viewer has focus):

[←] → Go to previous episode
      - Jumps to start of previous episode
      - If at start of episode 1, do nothing

[→] → Go to next episode
      - Jumps to start of next episode
      - If at last episode, do nothing

[Home] → Go to first episode
         - Jumps to 0.0s (start of recording)

[End] → Go to last episode
        - Jumps to start of last episode

[1-9] → Jump to episode by number
        - Press "2" → Jump to episode 2
        - If episode doesn't exist, show message

[Space] → Play/Pause (existing behavior)
```

**Visual Feedback**:
- Highlight focused episode label with outline
- Show tooltip with keyboard shortcuts on first load
- Animate transition when jumping between episodes

### Flow 5: Hover Interactions

```
┌─────────────────────────────────────────────────────────┐
│ User hovers over episode label                          │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Tooltip appears (200ms delay):                          │
│ ┌───────────────────────────────────────────────────┐  │
│ │ Disable Night Shift                                │  │
│ │ ─────────────────────────────────────────────────  │  │
│ │ User scrolls down to find Night Shift settings     │  │
│ │ and toggles it off.                                │  │
│ │                                                     │  │
│ │ Duration: 3.2s • 3 steps                           │  │
│ │ Confidence: 95%                                    │  │
│ │                                                     │  │
│ │ Click to jump to this episode                      │  │
│ └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘

User hovers over timeline:
┌─────────────────────────────────────────────────────────┐
│ Preview marker appears at hover position                │
│ - Vertical line showing where click would seek          │
│ - Timestamp tooltip: "4.2s - Episode 2"                 │
│ - Episode color hint (matches episode segment)          │
└─────────────────────────────────────────────────────────┘
```

### Flow 6: Mobile Touch Interactions

```
On mobile/tablet:

Swipe left on timeline → Next episode
Swipe right on timeline → Previous episode

Long-press episode label → Show details (tooltip)
Tap episode label → Jump to episode (same as desktop)

Pinch-zoom timeline → Zoom into episode details
  - Shows step-level markers
  - Can scrub within episode more precisely

Two-finger drag → Scroll horizontally (if timeline wider than screen)
```

---

## Technical Implementation

### File Structure

```
openadapt-viewer/
├── src/
│   └── openadapt_viewer/
│       ├── components/
│       │   ├── episode_timeline.py      # Python component generator
│       │   └── episode_timeline.js      # Standalone JS component
│       │
│       └── styles/
│           └── episode_timeline.css     # Component styles
│
├── capture_viewer.html                  # Enhanced with episodes
├── segmentation_viewer.html             # Enhanced with mini timelines
└── test_episode_timeline.html           # Interactive demo
```

### Core JavaScript Component

**File**: `src/openadapt_viewer/components/episode_timeline.js`

```javascript
/**
 * EpisodeTimeline - Reusable episode timeline component
 *
 * Usage:
 *   const timeline = new EpisodeTimeline({
 *     container: document.getElementById('timeline-container'),
 *     episodes: [...],
 *     currentTime: 0,
 *     totalDuration: 6.7,
 *     onSeek: (time) => { player.seek(time); },
 *     onEpisodeChange: (episode) => { console.log('Now in:', episode.name); }
 *   });
 *
 *   // Update current time (e.g., from playback loop)
 *   timeline.update({ currentTime: 4.2 });
 */

class EpisodeTimeline {
  constructor(options) {
    this.container = options.container;
    this.episodes = options.episodes || [];
    this.currentTime = options.currentTime || 0;
    this.totalDuration = options.totalDuration || this.calculateTotalDuration();
    this.onSeek = options.onSeek || (() => {});
    this.onEpisodeChange = options.onEpisodeChange || (() => {});

    this.config = {
      showLabels: true,
      showBoundaries: true,
      enableClickNavigation: true,
      enableAutoAdvance: false,
      colorScheme: 'auto',
      labelTruncate: 30,
      ...options.config
    };

    this.state = {
      currentEpisodeIndex: -1,
      hoveredEpisodeId: null,
      isDragging: false,
      previewTime: null
    };

    this.init();
  }

  init() {
    if (!this.container) {
      console.error('EpisodeTimeline: Container element not found');
      return;
    }

    if (!this.episodes.length) {
      console.warn('EpisodeTimeline: No episodes provided');
      this.renderEmpty();
      return;
    }

    this.render();
    this.attachEventListeners();
    this.updateCurrentEpisode();
  }

  calculateTotalDuration() {
    if (!this.episodes.length) return 0;
    const lastEpisode = this.episodes[this.episodes.length - 1];
    return lastEpisode.end_time;
  }

  render() {
    const html = `
      <div class="oa-episode-timeline">
        ${this.renderCurrentIndicator()}
        ${this.renderLabels()}
        ${this.renderTrack()}
        ${this.renderControls()}
        ${this.renderTooltip()}
      </div>
    `;

    this.container.innerHTML = html;
  }

  renderCurrentIndicator() {
    if (this.state.currentEpisodeIndex < 0) return '';

    const episode = this.episodes[this.state.currentEpisodeIndex];
    const index = this.state.currentEpisodeIndex;

    return `
      <div class="oa-episode-current-indicator">
        <span class="oa-episode-current-label">Episode</span>
        <strong>${index + 1}</strong>
        <span>of</span>
        <strong>${this.episodes.length}</strong>
        <span class="oa-episode-divider">—</span>
        <span class="oa-episode-current-name">${episode.name}</span>
      </div>
    `;
  }

  renderLabels() {
    if (!this.config.showLabels) return '';

    const labels = this.episodes.map((episode, index) => {
      const left = (episode.start_time / this.totalDuration) * 100;
      const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;
      const color = this.getEpisodeColor(index);
      const isCurrent = index === this.state.currentEpisodeIndex;
      const isPast = index < this.state.currentEpisodeIndex;
      const isFuture = index > this.state.currentEpisodeIndex;

      const classes = [
        'oa-episode-label',
        isCurrent && 'oa-episode-current',
        isPast && 'oa-episode-past',
        isFuture && 'oa-episode-future'
      ].filter(Boolean).join(' ');

      const truncatedName = this.truncateText(episode.name, this.config.labelTruncate);

      return `
        <div class="${classes}"
             data-episode-id="${episode.episode_id}"
             data-episode-index="${index}"
             style="left: ${left}%; width: ${width}%; background: ${color};"
             role="button"
             tabindex="0"
             aria-label="Jump to episode ${index + 1}: ${episode.name}">
          <span class="oa-episode-label-text">${truncatedName}</span>
          <span class="oa-episode-label-duration">${this.formatDuration(episode.duration)}</span>
        </div>
      `;
    }).join('');

    return `
      <div class="oa-episode-labels" role="group" aria-label="Episode labels">
        ${labels}
      </div>
    `;
  }

  renderTrack() {
    const segments = this.episodes.map((episode, index) => {
      const left = (episode.start_time / this.totalDuration) * 100;
      const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;
      const color = this.getEpisodeColor(index);
      const isCurrent = index === this.state.currentEpisodeIndex;

      return `
        <div class="oa-episode-segment ${isCurrent ? 'oa-episode-current' : ''}"
             data-episode-index="${index}"
             style="left: ${left}%; width: ${width}%; background: ${color};">
        </div>
      `;
    }).join('');

    const boundaries = this.config.showBoundaries ?
      this.episodes.slice(0, -1).map((episode) => {
        const left = (episode.end_time / this.totalDuration) * 100;
        return `
          <div class="oa-episode-boundary"
               style="left: ${left}%;"
               role="separator">
          </div>
        `;
      }).join('') : '';

    const markerLeft = (this.currentTime / this.totalDuration) * 100;
    const currentMarker = `
      <div class="oa-current-marker"
           style="left: ${markerLeft}%;"
           role="slider"
           aria-label="Current playback position"
           aria-valuenow="${this.currentTime.toFixed(1)}"
           aria-valuemin="0"
           aria-valuemax="${this.totalDuration.toFixed(1)}">
      </div>
    `;

    return `
      <div class="oa-timeline-track"
           role="slider"
           aria-label="Playback timeline"
           tabindex="0">
        ${segments}
        ${boundaries}
        ${currentMarker}
      </div>
    `;
  }

  renderControls() {
    const hasPrev = this.state.currentEpisodeIndex > 0;
    const hasNext = this.state.currentEpisodeIndex < this.episodes.length - 1;

    return `
      <div class="oa-episode-controls">
        <button class="oa-episode-nav-btn"
                data-action="prev"
                ${!hasPrev ? 'disabled' : ''}
                aria-label="Go to previous episode">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
          </svg>
          <span>Previous Episode</span>
        </button>

        <button class="oa-episode-nav-btn"
                data-action="next"
                ${!hasNext ? 'disabled' : ''}
                aria-label="Go to next episode">
          <span>Next Episode</span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
          </svg>
        </button>
      </div>
    `;
  }

  renderTooltip() {
    return `
      <div class="oa-episode-tooltip"
           role="tooltip"
           style="display: none;">
        <div class="oa-episode-tooltip-header">
          <strong class="oa-episode-tooltip-name"></strong>
          <span class="oa-episode-tooltip-meta"></span>
        </div>
        <div class="oa-episode-tooltip-description"></div>
        <div class="oa-episode-tooltip-footer"></div>
      </div>
    `;
  }

  renderEmpty() {
    this.container.innerHTML = `
      <div class="oa-episode-timeline-empty">
        <p>No episodes available for this recording.</p>
      </div>
    `;
  }

  attachEventListeners() {
    // Episode label clicks
    this.container.querySelectorAll('.oa-episode-label').forEach(label => {
      label.addEventListener('click', (e) => this.handleLabelClick(e));
      label.addEventListener('mouseenter', (e) => this.handleLabelHover(e));
      label.addEventListener('mouseleave', () => this.hideTooltip());
      label.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          this.handleLabelClick(e);
        }
      });
    });

    // Timeline track clicks
    const track = this.container.querySelector('.oa-timeline-track');
    if (track) {
      track.addEventListener('click', (e) => this.handleTrackClick(e));
      track.addEventListener('mousemove', (e) => this.handleTrackHover(e));
      track.addEventListener('mouseleave', () => this.hidePreview());
    }

    // Navigation buttons
    this.container.querySelectorAll('.oa-episode-nav-btn').forEach(btn => {
      btn.addEventListener('click', (e) => this.handleNavClick(e));
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => this.handleKeydown(e));
  }

  handleLabelClick(e) {
    if (!this.config.enableClickNavigation) return;

    const episodeId = e.currentTarget.dataset.episodeId;
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);

    if (episode) {
      this.seekToEpisode(episode);
    }
  }

  handleLabelHover(e) {
    const episodeId = e.currentTarget.dataset.episodeId;
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);

    if (episode) {
      this.showTooltip(episode, e);
    }
  }

  handleTrackClick(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percent = clickX / rect.width;
    const time = percent * this.totalDuration;

    this.onSeek(time);
  }

  handleTrackHover(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const hoverX = e.clientX - rect.left;
    const percent = hoverX / rect.width;
    const time = percent * this.totalDuration;

    this.state.previewTime = time;
    // Could show preview marker here
  }

  handleNavClick(e) {
    const action = e.currentTarget.dataset.action;

    if (action === 'prev') {
      this.prevEpisode();
    } else if (action === 'next') {
      this.nextEpisode();
    }
  }

  handleKeydown(e) {
    // Only handle if timeline has focus or no other input is focused
    if (document.activeElement.tagName === 'INPUT') return;

    switch(e.key) {
      case 'ArrowLeft':
        e.preventDefault();
        this.prevEpisode();
        break;
      case 'ArrowRight':
        e.preventDefault();
        this.nextEpisode();
        break;
      case 'Home':
        e.preventDefault();
        this.seekToEpisode(this.episodes[0]);
        break;
      case 'End':
        e.preventDefault();
        this.seekToEpisode(this.episodes[this.episodes.length - 1]);
        break;
      default:
        // Number keys 1-9 for direct episode access
        if (e.key >= '1' && e.key <= '9') {
          const index = parseInt(e.key) - 1;
          if (index < this.episodes.length) {
            e.preventDefault();
            this.seekToEpisode(this.episodes[index]);
          }
        }
    }
  }

  showTooltip(episode, event) {
    const tooltip = this.container.querySelector('.oa-episode-tooltip');
    if (!tooltip) return;

    const nameEl = tooltip.querySelector('.oa-episode-tooltip-name');
    const metaEl = tooltip.querySelector('.oa-episode-tooltip-meta');
    const descEl = tooltip.querySelector('.oa-episode-tooltip-description');
    const footerEl = tooltip.querySelector('.oa-episode-tooltip-footer');

    nameEl.textContent = episode.name;
    metaEl.textContent = `${this.formatDuration(episode.duration)} • ${episode.steps?.length || 0} steps`;
    descEl.textContent = episode.description;

    if (episode.boundary_confidence) {
      footerEl.textContent = `Confidence: ${(episode.boundary_confidence * 100).toFixed(0)}%`;
    }

    // Position tooltip
    const labelRect = event.currentTarget.getBoundingClientRect();
    const containerRect = this.container.getBoundingClientRect();

    tooltip.style.left = `${labelRect.left - containerRect.left}px`;
    tooltip.style.top = `${labelRect.top - containerRect.top - tooltip.offsetHeight - 8}px`;
    tooltip.style.display = 'block';

    this.state.hoveredEpisodeId = episode.episode_id;
  }

  hideTooltip() {
    const tooltip = this.container.querySelector('.oa-episode-tooltip');
    if (tooltip) {
      tooltip.style.display = 'none';
    }
    this.state.hoveredEpisodeId = null;
  }

  hidePreview() {
    this.state.previewTime = null;
  }

  seekToEpisode(episode) {
    this.onSeek(episode.start_time);
  }

  prevEpisode() {
    if (this.state.currentEpisodeIndex > 0) {
      const prevEp = this.episodes[this.state.currentEpisodeIndex - 1];
      this.seekToEpisode(prevEp);
    }
  }

  nextEpisode() {
    if (this.state.currentEpisodeIndex < this.episodes.length - 1) {
      const nextEp = this.episodes[this.state.currentEpisodeIndex + 1];
      this.seekToEpisode(nextEp);
    }
  }

  update(updates) {
    let needsRender = false;

    if (updates.currentTime !== undefined && updates.currentTime !== this.currentTime) {
      this.currentTime = updates.currentTime;
      this.updateCurrentEpisode();
      needsRender = true;
    }

    if (updates.episodes !== undefined) {
      this.episodes = updates.episodes;
      needsRender = true;
    }

    if (needsRender) {
      this.render();
      this.attachEventListeners();
    } else {
      // Just update marker position (more efficient)
      this.updateMarkerPosition();
    }
  }

  updateCurrentEpisode() {
    const previousIndex = this.state.currentEpisodeIndex;

    // Find which episode we're in
    for (let i = 0; i < this.episodes.length; i++) {
      const ep = this.episodes[i];
      if (this.currentTime >= ep.start_time && this.currentTime < ep.end_time) {
        this.state.currentEpisodeIndex = i;
        break;
      }
    }

    // If we've crossed a boundary, fire callback
    if (previousIndex !== this.state.currentEpisodeIndex &&
        this.state.currentEpisodeIndex >= 0) {
      const episode = this.episodes[this.state.currentEpisodeIndex];
      this.onEpisodeChange(episode);
    }
  }

  updateMarkerPosition() {
    const marker = this.container.querySelector('.oa-current-marker');
    if (marker) {
      const left = (this.currentTime / this.totalDuration) * 100;
      marker.style.left = `${left}%`;
      marker.setAttribute('aria-valuenow', this.currentTime.toFixed(1));
    }
  }

  getEpisodeColor(index) {
    const colorIndex = (index % 5) + 1;
    return `var(--episode-${colorIndex}-bg)`;
  }

  formatDuration(seconds) {
    if (seconds < 60) {
      return `${seconds.toFixed(1)}s`;
    }
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}m ${secs}s`;
  }

  truncateText(text, maxLength) {
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength - 3) + '...';
  }

  destroy() {
    // Clean up event listeners
    document.removeEventListener('keydown', this.handleKeydown);
    this.container.innerHTML = '';
  }
}

// Export for use in modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = EpisodeTimeline;
}
```

### CSS Styles

**File**: `src/openadapt_viewer/styles/episode_timeline.css`

```css
/* Episode Timeline Component Styles */

.oa-episode-timeline {
  padding: var(--oa-space-md);
  background: var(--oa-bg-secondary);
  border-radius: var(--oa-border-radius-lg);
  margin: var(--oa-space-md) 0;
}

/* Current Episode Indicator */
.oa-episode-current-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: var(--oa-bg-tertiary);
  border-radius: var(--oa-border-radius);
  margin-bottom: 16px;
  font-size: 14px;
  color: var(--oa-text-secondary);
}

.oa-episode-current-indicator strong {
  color: var(--oa-accent);
  font-weight: 700;
  font-size: 16px;
}

.oa-episode-current-name {
  color: var(--oa-text-primary);
  font-weight: 600;
}

.oa-episode-divider {
  color: var(--oa-text-muted);
  margin: 0 4px;
}

/* Episode Labels */
.oa-episode-labels {
  position: relative;
  height: 40px;
  margin-bottom: 12px;
}

.oa-episode-label {
  position: absolute;
  top: 0;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
  border: 2px solid transparent;
  opacity: 0.7;
  overflow: hidden;
}

.oa-episode-label:hover {
  opacity: 1;
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  z-index: 10;
}

.oa-episode-label:focus {
  outline: 2px solid var(--oa-accent);
  outline-offset: 2px;
}

.oa-episode-label.oa-episode-current {
  opacity: 1;
  border-color: var(--oa-accent);
  box-shadow: 0 0 12px rgba(0, 212, 170, 0.6);
  z-index: 5;
}

.oa-episode-label.oa-episode-past {
  opacity: 0.5;
}

.oa-episode-label.oa-episode-future {
  opacity: 0.3;
}

.oa-episode-label-text {
  font-size: 12px;
  font-weight: 600;
  color: white;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  margin-right: 8px;
}

.oa-episode-label-duration {
  font-size: 11px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.8);
  white-space: nowrap;
}

/* Timeline Track */
.oa-timeline-track {
  position: relative;
  height: 8px;
  background: var(--oa-bg-tertiary);
  border-radius: 4px;
  overflow: visible;
  cursor: pointer;
  margin-bottom: 8px;
}

.oa-timeline-track:focus {
  outline: 2px solid var(--oa-accent);
  outline-offset: 4px;
}

.oa-episode-segment {
  position: absolute;
  top: 0;
  height: 100%;
  border-radius: 4px;
  transition: filter 0.2s ease;
}

.oa-episode-segment.oa-episode-current {
  filter: brightness(1.2);
}

.oa-episode-boundary {
  position: absolute;
  top: -6px;
  bottom: -6px;
  width: 2px;
  background: rgba(255, 255, 255, 0.4);
  z-index: 10;
  pointer-events: none;
}

.oa-current-marker {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--oa-accent);
  border: 2px solid var(--oa-bg-secondary);
  box-shadow: 0 0 8px var(--oa-accent);
  z-index: 20;
  pointer-events: none;
  transition: left 0.1s linear;
}

/* Episode Controls */
.oa-episode-controls {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-top: 12px;
}

.oa-episode-nav-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  background: var(--oa-bg-tertiary);
  border: 1px solid var(--oa-border-color);
  border-radius: var(--oa-border-radius);
  color: var(--oa-text-primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.oa-episode-nav-btn:hover:not(:disabled) {
  background: var(--oa-accent-dim);
  color: var(--oa-accent);
  border-color: var(--oa-accent);
}

.oa-episode-nav-btn:focus {
  outline: 2px solid var(--oa-accent);
  outline-offset: 2px;
}

.oa-episode-nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.oa-episode-nav-btn svg {
  width: 16px;
  height: 16px;
  fill: currentColor;
}

/* Episode Tooltip */
.oa-episode-tooltip {
  position: absolute;
  z-index: 100;
  background: var(--oa-bg-primary);
  border: 1px solid var(--oa-border-color);
  border-radius: var(--oa-border-radius-lg);
  padding: 12px;
  box-shadow: var(--oa-shadow-lg);
  max-width: 300px;
  pointer-events: none;
}

.oa-episode-tooltip-header {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 12px;
  margin-bottom: 8px;
}

.oa-episode-tooltip-name {
  color: var(--oa-accent);
  font-size: 14px;
  font-weight: 600;
}

.oa-episode-tooltip-meta {
  color: var(--oa-text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.oa-episode-tooltip-description {
  color: var(--oa-text-secondary);
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 8px;
}

.oa-episode-tooltip-footer {
  color: var(--oa-text-muted);
  font-size: 11px;
  padding-top: 8px;
  border-top: 1px solid var(--oa-border-color);
}

/* Empty State */
.oa-episode-timeline-empty {
  padding: 40px;
  text-align: center;
  color: var(--oa-text-muted);
  font-size: 14px;
}

/* Episode Color Variables */
:root {
  --episode-1-bg: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
  --episode-2-bg: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
  --episode-3-bg: linear-gradient(135deg, #ec4899 0%, #db2777 100%);
  --episode-4-bg: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
  --episode-5-bg: linear-gradient(135deg, #10b981 0%, #059669 100%);
}

/* Responsive Design */
@media (max-width: 768px) {
  .oa-episode-timeline {
    padding: var(--oa-space-sm);
  }

  .oa-episode-labels {
    height: auto;
    min-height: 40px;
  }

  .oa-episode-label {
    /* Stack labels on mobile if too many */
    position: relative;
    width: 100% !important;
    left: 0 !important;
    margin-bottom: 4px;
  }

  .oa-episode-current-indicator {
    flex-wrap: wrap;
    font-size: 12px;
  }

  .oa-episode-nav-btn span {
    display: none; /* Show only icons on mobile */
  }

  .oa-episode-tooltip {
    max-width: 250px;
    font-size: 12px;
  }
}

@media (max-width: 480px) {
  .oa-episode-label-duration {
    display: none; /* Hide duration on very small screens */
  }

  .oa-episode-controls {
    width: 100%;
  }

  .oa-episode-nav-btn {
    flex: 1;
    justify-content: center;
  }
}
```

---

Continued in Part 3...

Would you like me to continue with the remaining sections (Testing Strategy, Accessibility, Advanced Features, Appendices)?
