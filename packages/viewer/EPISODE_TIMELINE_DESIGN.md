# Episode Timeline Integration - Comprehensive Design Document

**Date:** 2026-01-17
**Status:** Design Phase
**Owner:** OpenAdapt Viewer Team

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current State Analysis](#current-state-analysis)
3. [Design Goals](#design-goals)
4. [Visual Design Specifications](#visual-design-specifications)
5. [Component Architecture](#component-architecture)
6. [Data Flow Design](#data-flow-design)
7. [Implementation Phases](#implementation-phases)
8. [Integration Guide](#integration-guide)
9. [User Interaction Patterns](#user-interaction-patterns)
10. [Technical Implementation](#technical-implementation)
11. [Testing Strategy](#testing-strategy)
12. [Accessibility & Responsiveness](#accessibility--responsiveness)

---

## Executive Summary

### Vision

Transform the capture viewer and other viewers into episode-aware applications with an intelligent, interactive timeline that displays episode boundaries, enables quick navigation, and provides rich context about the user's workflow.

### Key Features

- **Episode Labels on Timeline**: Visual markers showing episode names above the timeline
- **Episode Boundaries**: Clear vertical dividers showing where one episode ends and another begins
- **Click-to-Navigate**: Jump to any episode by clicking its label
- **Current Episode Context**: Always show which episode the user is viewing
- **Automatic Episode Loading**: Fetch episodes from catalog or JSON files
- **Episode-Aware Playback**: Auto-advance between episodes, skip episodes, view episode-specific details
- **Reusable Component**: Apply timeline to capture, segmentation, synthetic demo, and benchmark viewers

### Success Metrics

- Users can identify which episode they're viewing in < 2 seconds
- Navigation between episodes takes < 1 click
- Timeline renders smoothly with 1-20 episodes
- Component reusable across 4+ viewer types

---

## Current State Analysis

### Existing Infrastructure

**Episode Data Structure** (from `test_episodes.json`):
```json
{
  "episode_id": "episode_001",
  "name": "Navigate to System Settings",
  "description": "User opens System Settings...",
  "start_time": 0.0,
  "end_time": 3.5,
  "duration": 3.5,
  "steps": ["Click System Settings icon", "Wait for window", "Click Displays"],
  "boundary_confidence": 0.92,
  "coherence_score": 0.88,
  "screenshots": {
    "thumbnail": "../path/to/thumbnail.png",
    "key_frames": [...]
  }
}
```

**Current Capture Viewer** (`/path/to/openadapt-viewer/capture_viewer.html`):
- ✅ Playback controls (play/pause, prev/next step)
- ✅ Timeline scrubber (clickable progress bar)
- ✅ Speed controls (0.5x, 1x, 2x, 4x)
- ✅ URL parameters for highlighting (`highlight_start`, `highlight_end`, `episode_name`)
- ❌ No episode labels on timeline
- ❌ No episode boundaries visualization
- ❌ No episode navigation buttons
- ❌ Timeline is just a gradient progress bar

**Current Segmentation Viewer** (`/path/to/openadapt-viewer/segmentation_viewer.html`):
- ✅ Episode cards in grid layout
- ✅ Episode detail view with steps
- ✅ Links to capture viewer with URL parameters
- ✅ Search and filter episodes
- ❌ No timeline visualization
- ❌ No preview of episode timeline

**Gap Analysis**:
```
┌─────────────────────────────────────────────────────────────┐
│ CURRENT:                                                     │
│  Timeline:  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│             (just a gradient bar, no episode info)           │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ DESIRED:                                                     │
│  Episodes:  [Navigate to Settings] [Disable Night Shift]    │
│             ├─────────────┤├──────────────┤                 │
│  Timeline:  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│             ↑             ↑               ↑                  │
│             0s            3.5s            6.7s               │
│                           ●                                  │
│                    (current position)                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Design Goals

### Primary Goals

1. **Visibility**: Users should instantly see episode structure
2. **Navigation**: One-click jump to any episode
3. **Context**: Always know which episode you're viewing
4. **Performance**: Smooth with 1-20 episodes
5. **Reusability**: Works across all viewer types

### Secondary Goals

6. **Progressive Enhancement**: Works without episodes (graceful degradation)
7. **Accessibility**: Keyboard navigation, screen reader support
8. **Mobile-Friendly**: Responsive design for tablet/phone
9. **Customizable**: Episode colors, labels, animations configurable
10. **Extensible**: Easy to add features (bookmarks, comparison, analytics)

### Non-Goals

- ❌ Real-time episode generation (pre-computed only)
- ❌ Episode editing/refinement in viewer (read-only)
- ❌ Multi-viewer synchronization (future feature)
- ❌ Episode analytics dashboard (separate viewer)

---

## Visual Design Specifications

### Layout Concept

```
┌──────────────────────────────────────────────────────────────────┐
│ EPISODE TIMELINE COMPONENT                                        │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Current: Episode 2 of 2 - "Disable Night Shift"                │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Episode Labels (hover to see description)                   │ │
│  │                                                              │ │
│  │  [Navigate to System Settings]  [Disable Night Shift]      │ │
│  │  ├──────────────────────┤├─────────────────────┤           │ │
│  ├──────────────────────────────────────────────────────────────┤ │
│  │ Timeline Track                                               │ │
│  │                                                              │ │
│  │  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │ │
│  │  │            │                        │                    │ │
│  │  0.0s         3.5s                     6.7s                 │ │
│  │               ●                                             │ │
│  │          (current position: 4.2s)                          │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  [◄ Prev Episode]  [Play/Pause]  [Next Episode ►]               │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### Color Palette

**Episode Colors** (rotating palette):
```css
/* Episode 1 */
--episode-1-bg: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); /* Blue */
--episode-1-text: #ffffff;
--episode-1-border: #3b82f6;

/* Episode 2 */
--episode-2-bg: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); /* Purple */
--episode-2-text: #ffffff;
--episode-2-border: #8b5cf6;

/* Episode 3 */
--episode-3-bg: linear-gradient(135deg, #ec4899 0%, #db2777 100%); /* Pink */
--episode-3-text: #ffffff;
--episode-3-border: #ec4899;

/* Episode 4 */
--episode-4-bg: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); /* Orange */
--episode-4-text: #ffffff;
--episode-4-border: #f59e0b;

/* Episode 5+ (cycle back to blue with slight variation) */
--episode-5-bg: linear-gradient(135deg, #10b981 0%, #059669 100%); /* Green */
--episode-5-text: #ffffff;
--episode-5-border: #10b981;
```

**State Colors**:
```css
/* Current episode (active) */
--episode-current: brightness(1.2) drop-shadow(0 0 8px currentColor);

/* Past episodes (dimmed) */
--episode-past: opacity(0.6);

/* Future episodes (more dimmed) */
--episode-future: opacity(0.4);

/* Hover state */
--episode-hover: brightness(1.1) scale(1.02);
```

### Typography

```css
/* Episode label */
.oa-episode-label {
  font-family: var(--oa-font-sans);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.3px;
  text-transform: none; /* Keep original casing */
}

/* Current episode display */
.oa-episode-current {
  font-size: 16px;
  font-weight: 700;
  color: var(--oa-text-primary);
}

/* Episode description (tooltip) */
.oa-episode-description {
  font-size: 14px;
  font-weight: 400;
  line-height: 1.5;
  color: var(--oa-text-secondary);
}

/* Episode metadata */
.oa-episode-meta {
  font-size: 11px;
  font-weight: 500;
  color: var(--oa-text-muted);
}
```

### Spacing & Dimensions

```css
:root {
  /* Timeline dimensions */
  --oa-timeline-height: 8px;
  --oa-timeline-border-radius: 4px;

  /* Episode label dimensions */
  --oa-episode-label-height: 32px;
  --oa-episode-label-padding: 6px 12px;
  --oa-episode-label-gap: 8px; /* Gap between labels and timeline */

  /* Episode boundary marker */
  --oa-episode-boundary-width: 2px;
  --oa-episode-boundary-height: 40px; /* Extends above and below timeline */

  /* Current position marker */
  --oa-current-marker-size: 12px;
  --oa-current-marker-color: var(--oa-accent);

  /* Container spacing */
  --oa-episode-timeline-padding: 16px;
  --oa-episode-timeline-margin: 16px 0;
}
```

### Visual States

**Episode Label States**:

1. **Default**: Semi-transparent, subtle gradient
2. **Current**: Bright, glowing border, full opacity
3. **Hover**: Scale up slightly, show tooltip
4. **Past**: Dimmed opacity (50%)
5. **Future**: More dimmed opacity (30%)
6. **Clicked**: Brief pulse animation

**Timeline States**:

1. **Default**: Dark gray background
2. **Episode Segment**: Colored based on episode
3. **Current Position**: Circular marker with accent color
4. **Hover**: Show timestamp tooltip
5. **Dragging**: Change cursor, show preview time

---

## Component Architecture

### Core Components

#### 1. `EpisodeTimeline` (Main Component)

**Purpose**: Orchestrates all sub-components and manages state

**Props**:
```typescript
interface EpisodeTimelineProps {
  episodes: Episode[];           // Array of episode objects
  currentTime: number;            // Current playback position (seconds)
  totalDuration: number;          // Total recording duration
  currentStep?: number;           // Optional: current step index
  onSeek: (time: number) => void; // Callback when user seeks
  onEpisodeClick: (episodeId: string, startTime: number) => void;
  onEpisodeChange?: (episode: Episode) => void; // Fired when crossing boundary

  // Optional configuration
  config?: {
    showLabels?: boolean;        // Default: true
    showBoundaries?: boolean;    // Default: true
    enableClickNavigation?: boolean; // Default: true
    enableAutoAdvance?: boolean; // Default: false
    colorScheme?: 'auto' | 'blue' | 'purple' | 'custom';
    labelTruncate?: number;      // Max chars, default: 30
  }
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
```

**State**:
```typescript
interface EpisodeTimelineState {
  currentEpisodeIndex: number;    // Which episode we're in (0-indexed)
  hoveredEpisodeId: string | null; // For tooltip display
  isDragging: boolean;             // User is dragging timeline
  previewTime: number | null;      // Time shown when hovering
}
```

**Methods**:
```typescript
class EpisodeTimeline {
  // Core methods
  getCurrentEpisode(): Episode | null;
  getEpisodeAtTime(time: number): Episode | null;
  seekToEpisode(episodeId: string): void;
  nextEpisode(): void;
  prevEpisode(): void;

  // Utility methods
  getEpisodeColor(index: number): string;
  formatEpisodeDuration(episode: Episode): string;
  calculateEpisodePosition(episode: Episode): { left: string, width: string };

  // Event handlers
  handleLabelClick(episodeId: string): void;
  handleTimelineClick(event: MouseEvent): void;
  handleLabelHover(episodeId: string): void;
  handleTimelineHover(event: MouseEvent): void;
}
```

#### 2. `EpisodeLabels` (Sub-component)

**Purpose**: Renders episode labels above timeline

```html
<div class="oa-episode-labels">
  <div class="oa-episode-label"
       data-episode-id="episode_001"
       style="left: 0%; width: 52.2%"
       @click="handleLabelClick"
       @mouseenter="handleLabelHover">
    <span class="oa-episode-label-text">Navigate to System Settings</span>
    <span class="oa-episode-label-duration">3.5s</span>
  </div>

  <div class="oa-episode-label oa-episode-current"
       data-episode-id="episode_002"
       style="left: 52.2%; width: 47.8%">
    <span class="oa-episode-label-text">Disable Night Shift</span>
    <span class="oa-episode-label-duration">3.2s</span>
  </div>
</div>
```

#### 3. `EpisodeTrack` (Sub-component)

**Purpose**: Renders the timeline track with episode segments

```html
<div class="oa-timeline-track" @click="handleTimelineClick">
  <!-- Episode segments -->
  <div class="oa-episode-segment"
       data-episode-id="episode_001"
       style="left: 0%; width: 52.2%; background: var(--episode-1-bg)">
  </div>

  <div class="oa-episode-segment oa-episode-current"
       data-episode-id="episode_002"
       style="left: 52.2%; width: 47.8%; background: var(--episode-2-bg)">
  </div>

  <!-- Episode boundaries -->
  <div class="oa-episode-boundary" style="left: 52.2%"></div>

  <!-- Current position marker -->
  <div class="oa-current-marker" style="left: 62.7%"></div>
</div>
```

#### 4. `EpisodeMarkers` (Sub-component)

**Purpose**: Time markers below timeline

```html
<div class="oa-timeline-markers">
  <span class="oa-timeline-marker">0.0s</span>
  <span class="oa-timeline-marker oa-episode-boundary-marker">3.5s</span>
  <span class="oa-timeline-marker">6.7s</span>
</div>
```

#### 5. `EpisodeTooltip` (Sub-component)

**Purpose**: Show episode details on hover

```html
<div class="oa-episode-tooltip"
     style="left: 200px; top: -80px"
     x-show="hoveredEpisodeId">
  <div class="oa-episode-tooltip-header">
    <strong>Disable Night Shift</strong>
    <span class="oa-episode-tooltip-meta">3.2s • 3 steps</span>
  </div>
  <div class="oa-episode-tooltip-description">
    User scrolls down to find Night Shift settings and toggles it off.
  </div>
  <div class="oa-episode-tooltip-confidence">
    Confidence: 95%
  </div>
</div>
```

#### 6. `EpisodeControls` (Sub-component)

**Purpose**: Navigation buttons for episodes

```html
<div class="oa-episode-controls">
  <button class="oa-episode-nav-btn"
          @click="prevEpisode()"
          :disabled="currentEpisodeIndex === 0">
    <svg>...</svg>
    <span>Previous Episode</span>
  </button>

  <div class="oa-episode-current-indicator">
    Episode <strong>2</strong> of <strong>2</strong>
  </div>

  <button class="oa-episode-nav-btn"
          @click="nextEpisode()"
          :disabled="currentEpisodeIndex >= episodes.length - 1">
    <span>Next Episode</span>
    <svg>...</svg>
  </button>
</div>
```

### Component Hierarchy

```
EpisodeTimeline (root)
├── EpisodeContext (current episode display)
├── EpisodeLabels
│   └── EpisodeLabel (×N)
│       └── EpisodeTooltip (on hover)
├── EpisodeTrack
│   ├── EpisodeSegment (×N)
│   ├── EpisodeBoundary (×N-1)
│   └── CurrentMarker
├── EpisodeMarkers
│   └── TimeMarker (×N+1)
└── EpisodeControls
    ├── PrevButton
    ├── CurrentIndicator
    └── NextButton
```

---

## Data Flow Design

### Option A: Load from Episode JSON File

**Best for**: Segmentation viewer, when episodes are pre-computed

```
┌─────────────────────────────────────────────────────────┐
│ User opens capture viewer with ?episodes=path.json      │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Check URL parameter for episode file path               │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ fetch(episodesPath).then(data => ...)                   │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Parse episode JSON (validate schema)                    │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Render EpisodeTimeline component with episodes          │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ If highlight_start param exists, jump to that episode   │
└─────────────────────────────────────────────────────────┘
```

**Implementation**:
```javascript
async function loadEpisodes() {
  const params = new URLSearchParams(window.location.search);
  const episodesPath = params.get('episodes') ||
                       '../episodes/recording_123_episodes.json';

  try {
    const response = await fetch(episodesPath);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const data = await response.json();
    return data.episodes || [];
  } catch (error) {
    console.error('Failed to load episodes:', error);
    return null; // Graceful degradation
  }
}
```

### Option B: Fetch from Catalog API

**Best for**: Capture viewer integrated with catalog system

```
┌─────────────────────────────────────────────────────────┐
│ Capture viewer knows recording_id                       │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Query catalog for segmentation results                  │
│ catalog.getSegmentationResults(recording_id)            │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Catalog returns episode file path or null               │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ If episodes exist, fetch JSON and render timeline       │
│ If not, show viewer without episode features            │
└─────────────────────────────────────────────────────────┘
```

**Implementation**:
```javascript
async function loadEpisodesFromCatalog(recordingId) {
  if (!window.CATALOG_API) return null;

  try {
    const result = await window.CATALOG_API.getSegmentationResults(recordingId);

    if (result && result.episode_file_path) {
      const response = await fetch(result.episode_file_path);
      const data = await response.json();
      return data.episodes;
    }

    return null;
  } catch (error) {
    console.error('Failed to load from catalog:', error);
    return null;
  }
}
```

### Option C: Embed Episodes in Capture Data

**Best for**: Unified recording format with inline episodes

```
┌─────────────────────────────────────────────────────────┐
│ Recording JSON includes "episodes" field                │
│ {                                                        │
│   "recording_id": "...",                                │
│   "steps": [...],                                       │
│   "episodes": [...]  ← Embedded                         │
│ }                                                        │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ Capture viewer reads episodes directly from data        │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│ If episodes array exists and has items, render timeline │
└─────────────────────────────────────────────────────────┘
```

**Implementation**:
```javascript
function initializeViewer(recordingData) {
  const hasEpisodes = recordingData.episodes &&
                      recordingData.episodes.length > 0;

  if (hasEpisodes) {
    renderEpisodeTimeline(recordingData.episodes);
  } else {
    renderSimpleTimeline();
  }
}
```

### Recommended Approach

**Use Option B (Catalog API) for loose coupling**:

✅ **Pros**:
- Separation of concerns (episodes managed separately)
- Easy to update episodes without regenerating viewer
- Works with existing catalog infrastructure
- Falls back gracefully if episodes not available

❌ **Cons**:
- Requires catalog system to be set up
- Extra network request

**Fallback chain**:
1. Try catalog API → 2. Try URL parameter → 3. Try default path → 4. No episodes (graceful)

---

## Implementation Phases

### Phase 1: MVP - Basic Episode Timeline (Week 1)

**Goal**: Get episode labels and basic navigation working

**Deliverables**:
- [ ] `EpisodeTimeline.js` component (standalone JS)
- [ ] Episode labels rendered above timeline
- [ ] Click episode label to jump to start time
- [ ] Current episode indicator
- [ ] Basic CSS styling (colors, spacing)
- [ ] Integration with capture_viewer.html
- [ ] Load episodes from JSON file (Option A)

**Success Criteria**:
- Episodes displayed with correct names
- Clicking label jumps to episode start
- Current episode updates as playback progresses
- Works with 1-5 episodes

**Code Example**:
```javascript
// Phase 1: Minimal implementation
class EpisodeTimeline {
  constructor(episodes, currentTime, onSeek) {
    this.episodes = episodes;
    this.currentTime = currentTime;
    this.onSeek = onSeek;
  }

  render(container) {
    const html = `
      <div class="oa-episode-labels">
        ${this.episodes.map(ep => this.renderLabel(ep)).join('')}
      </div>
      <div class="oa-timeline-track">
        ${this.episodes.map(ep => this.renderSegment(ep)).join('')}
      </div>
    `;
    container.innerHTML = html;
    this.attachListeners(container);
  }

  renderLabel(episode) {
    const left = (episode.start_time / this.totalDuration) * 100;
    const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;

    return `
      <div class="oa-episode-label"
           data-id="${episode.episode_id}"
           style="left: ${left}%; width: ${width}%">
        ${episode.name}
      </div>
    `;
  }

  attachListeners(container) {
    container.querySelectorAll('.oa-episode-label').forEach(label => {
      label.addEventListener('click', (e) => {
        const episodeId = e.currentTarget.dataset.id;
        const episode = this.episodes.find(ep => ep.episode_id === episodeId);
        this.onSeek(episode.start_time);
      });
    });
  }
}
```

### Phase 2: Enhanced UX (Week 2)

**Goal**: Add polish and advanced features

**Deliverables**:
- [ ] Episode descriptions (tooltip on hover)
- [ ] Episode navigation buttons (Prev/Next)
- [ ] Episode color coding (5-color palette)
- [ ] Episode boundary markers
- [ ] Current position marker on timeline
- [ ] Auto-advance option (jump to next episode when current ends)
- [ ] Episode progress indicator ("Step 2 of 3 in episode")
- [ ] Smooth animations (seek, transitions)

**Success Criteria**:
- Hover shows episode description
- Prev/Next buttons work correctly
- Episodes visually distinct with colors
- Boundaries clearly visible
- Smooth seek animation (300ms)

### Phase 3: Polish & Responsiveness (Week 3)

**Goal**: Production-ready with mobile support

**Deliverables**:
- [ ] Mobile responsive layout (labels stack on small screens)
- [ ] Touch interactions (swipe between episodes)
- [ ] Keyboard shortcuts (←/→ for prev/next episode, 1-9 for jump)
- [ ] Accessibility (ARIA labels, focus indicators)
- [ ] Episode transition animations
- [ ] Toast notifications ("Entering Episode 2")
- [ ] Episode metadata display (confidence, coherence)
- [ ] Performance optimization (render only visible labels)

**Success Criteria**:
- Works on mobile (375px width)
- Keyboard navigation works
- Screen reader announces episode changes
- Smooth on 20 episodes
- Passes WCAG 2.1 AA

### Phase 4: Advanced Features (Month 2)

**Goal**: Power user features and analytics

**Deliverables**:
- [ ] Episode bookmarks (save favorite moments)
- [ ] Episode comparison mode (side-by-side)
- [ ] Episode analytics (view counts, skip rate)
- [ ] User refinement (adjust boundaries, suggest names)
- [ ] Episode export (JSON, CSV, screenshots)
- [ ] Episode search (find episodes by name/description)
- [ ] Episode thumbnails in timeline

**Success Criteria**:
- Bookmarks persist in localStorage
- Analytics tracked and visualized
- User can adjust boundaries and save
- Export generates valid JSON

---

## Integration Guide

### Capture Viewer Integration

**File**: `/path/to/openadapt-viewer/capture_viewer.html`

**Steps**:

1. **Add Episode Loading Logic**:

```javascript
// Add to Alpine.js component
x-data="{
  episodes: [],
  currentEpisodeIndex: -1,

  async init() {
    // Existing init code...

    // Load episodes
    this.episodes = await loadEpisodesFromCatalog(this.recordingId);

    if (this.episodes && this.episodes.length > 0) {
      this.updateCurrentEpisode();
    }
  },

  updateCurrentEpisode() {
    if (!this.episodes.length) return;

    const currentStep = this.steps[this.currentStep];
    if (!currentStep) return;

    const currentTime = currentStep.timestamp;

    // Find which episode we're in
    for (let i = 0; i < this.episodes.length; i++) {
      const ep = this.episodes[i];
      if (currentTime >= ep.start_time && currentTime < ep.end_time) {
        if (this.currentEpisodeIndex !== i) {
          this.currentEpisodeIndex = i;
          this.showEpisodeTransition(ep);
        }
        break;
      }
    }
  },

  showEpisodeTransition(episode) {
    // Show toast notification
    console.log('Entering episode:', episode.name);
  },

  seekToEpisode(episodeId) {
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);
    if (!episode) return;

    // Find first step in this episode
    for (let i = 0; i < this.steps.length; i++) {
      if (this.steps[i].timestamp >= episode.start_time) {
        this.currentStep = i;
        break;
      }
    }
  }
}"
```

2. **Add Episode Timeline HTML**:

```html
<!-- Insert after playback controls, before action details -->
<template x-if="episodes && episodes.length > 0">
  <div class="oa-episode-timeline-container">
    <!-- Current episode indicator -->
    <div class="oa-episode-current-indicator" x-show="currentEpisodeIndex >= 0">
      <span class="oa-episode-current-label">Current Episode:</span>
      <strong x-text="`${currentEpisodeIndex + 1} of ${episodes.length}`"></strong>
      <span>-</span>
      <span x-text="episodes[currentEpisodeIndex]?.name"></span>
    </div>

    <!-- Episode labels -->
    <div class="oa-episode-labels">
      <template x-for="(episode, idx) in episodes" :key="episode.episode_id">
        <div class="oa-episode-label"
             :class="{ 'oa-episode-current': idx === currentEpisodeIndex }"
             :style="`left: ${(episode.start_time / totalDuration) * 100}%;
                      width: ${((episode.end_time - episode.start_time) / totalDuration) * 100}%;
                      background: var(--episode-${(idx % 5) + 1}-bg);`"
             @click="seekToEpisode(episode.episode_id)"
             :title="episode.description">
          <span class="oa-episode-label-text" x-text="episode.name"></span>
        </div>
      </template>
    </div>

    <!-- Timeline with episode boundaries (modify existing timeline) -->
    <div class="oa-timeline-track" @click="handleTimelineClick">
      <!-- Existing progress bar... -->

      <!-- Add episode boundaries -->
      <template x-for="(episode, idx) in episodes.slice(0, -1)" :key="`boundary-${idx}`">
        <div class="oa-episode-boundary"
             :style="`left: ${(episode.end_time / totalDuration) * 100}%`">
        </div>
      </template>
    </div>

    <!-- Episode navigation controls -->
    <div class="oa-episode-controls">
      <button @click="prevEpisode()"
              :disabled="currentEpisodeIndex <= 0"
              class="oa-episode-nav-btn">
        ◄ Previous Episode
      </button>

      <button @click="nextEpisode()"
              :disabled="currentEpisodeIndex >= episodes.length - 1"
              class="oa-episode-nav-btn">
        Next Episode ►
      </button>
    </div>
  </div>
</template>
```

3. **Add CSS Styles**:

```css
/* Episode Timeline Styles */
.oa-episode-timeline-container {
  margin: 16px 0;
  padding: 16px;
  background: var(--oa-bg-secondary);
  border-radius: var(--oa-border-radius-lg);
}

.oa-episode-current-indicator {
  margin-bottom: 16px;
  padding: 12px;
  background: var(--oa-bg-tertiary);
  border-radius: var(--oa-border-radius);
  font-size: 14px;
  color: var(--oa-text-secondary);
}

.oa-episode-current-indicator strong {
  color: var(--oa-accent);
  font-weight: 700;
}

.oa-episode-labels {
  position: relative;
  height: 32px;
  margin-bottom: 8px;
}

.oa-episode-label {
  position: absolute;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
  border: 2px solid transparent;
  opacity: 0.7;
}

.oa-episode-label:hover {
  opacity: 1;
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}

.oa-episode-label.oa-episode-current {
  opacity: 1;
  border-color: var(--oa-accent);
  box-shadow: 0 0 12px var(--oa-accent);
}

.oa-episode-label-text {
  font-size: 12px;
  font-weight: 600;
  color: white;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.oa-episode-boundary {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  background: rgba(255, 255, 255, 0.3);
  z-index: 10;
  pointer-events: none;
}

.oa-episode-controls {
  margin-top: 12px;
  display: flex;
  gap: 12px;
  justify-content: center;
}

.oa-episode-nav-btn {
  padding: 8px 16px;
  background: var(--oa-bg-tertiary);
  border: 1px solid var(--oa-border-color);
  border-radius: var(--oa-border-radius);
  color: var(--oa-text-primary);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.oa-episode-nav-btn:hover:not(:disabled) {
  background: var(--oa-accent-dim);
  color: var(--oa-accent);
  border-color: var(--oa-accent);
}

.oa-episode-nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Episode color variables */
:root {
  --episode-1-bg: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
  --episode-2-bg: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
  --episode-3-bg: linear-gradient(135deg, #ec4899 0%, #db2777 100%);
  --episode-4-bg: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
  --episode-5-bg: linear-gradient(135deg, #10b981 0%, #059669 100%);
}
```

4. **Add Helper Methods**:

```javascript
// Add to Alpine.js component methods
prevEpisode() {
  if (this.currentEpisodeIndex > 0) {
    const prevEp = this.episodes[this.currentEpisodeIndex - 1];
    this.seekToEpisode(prevEp.episode_id);
  }
},

nextEpisode() {
  if (this.currentEpisodeIndex < this.episodes.length - 1) {
    const nextEp = this.episodes[this.currentEpisodeIndex + 1];
    this.seekToEpisode(nextEp.episode_id);
  }
},

get totalDuration() {
  if (!this.steps.length) return 0;
  const lastStep = this.steps[this.steps.length - 1];
  return lastStep.timestamp + (lastStep.duration || 0);
}
```

### Segmentation Viewer Integration

**File**: `/path/to/openadapt-viewer/segmentation_viewer.html`

**Enhancement**: Add mini timeline preview to episode cards

```html
<!-- In episode card rendering -->
<div class="episode-card">
  <div class="episode-thumbnail">...</div>

  <div class="episode-content">
    <div class="episode-name">...</div>
    <div class="episode-description">...</div>

    <!-- NEW: Mini timeline preview -->
    <div class="episode-mini-timeline">
      <div class="timeline-track">
        <div class="timeline-segment"
             :style="`width: ${(episode.duration / totalRecordingDuration) * 100}%`">
        </div>
      </div>
      <div class="timeline-label">
        <span>{{episode.start_time_formatted}}</span>
        <span>{{episode.end_time_formatted}}</span>
      </div>
    </div>
  </div>
</div>
```

---

This completes Part 1 of the design document covering:
- Executive Summary
- Current State Analysis
- Design Goals
- Visual Design Specifications
- Component Architecture
- Data Flow Design
- Implementation Phases
- Integration Guide (partial)

The document continues in the next section with:
- User Interaction Patterns
- Technical Implementation Details
- Testing Strategy
- Accessibility & Responsiveness
- Advanced Features
- Appendices

Would you like me to continue with the remaining sections?
