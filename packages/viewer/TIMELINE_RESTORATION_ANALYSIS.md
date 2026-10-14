# Timeline Functionality Restoration Analysis
**Date**: January 17, 2026
**Problem**: Timeline functionality degraded in refactored benchmark viewer. Should support click-to-play-segment, visual segment indicators, and repeat functionality.

---

## Executive Summary

**Status**: Timeline functionality exists but is underutilized in benchmark viewer. The refactored benchmark_viewer.html has basic timeline clicking but lacks:
1. **Segment-based playback** - no way to click individual task segments to play them
2. **Visual segment indicators** - timeline doesn't show task boundaries
3. **Repeat/loop functionality** - no auto-repeat for segments
4. **Keyboard shortcuts** - limited navigation controls

**Best implementation found**: `episode_timeline.js` component (used in capture_viewer.html and segmentation_viewer.html) provides the foundation but needs adaptation for benchmark task-based viewing.

---

## 1. Comprehensive Review of Existing Timeline Implementations

### 1.1 benchmark_viewer.html (Current Refactored Version)

**File**: `/path/to/openadapt-viewer/benchmark_viewer.html`

**Timeline Features**:
```html
<div class="oa-timeline-track">
    <div class="oa-timeline-progress"></div>
</div>
```

**Capabilities**:
- ✅ Basic progress bar visual
- ✅ CSS styling with gradient
- ❌ No click interaction
- ❌ No segment visualization
- ❌ No task boundaries
- ❌ No playback controls
- ❌ Static implementation (no Alpine.js integration)

**Assessment**: **MINIMAL** - Just visual styling, no functionality.

---

### 1.2 capture_viewer.html (Best Timeline Implementation)

**File**: `/path/to/openadapt-viewer/capture_viewer.html`

**Timeline Features**:
```javascript
// Episode Timeline integration
episodeTimeline: null,

initializeEpisodeTimeline() {
    this.episodeTimeline = new EpisodeTimeline({
        container: document.getElementById('episode-timeline-container'),
        episodes: this.episodes,
        currentTime: this.getCurrentTime(),
        totalDuration: this.getTotalDuration(),
        onSeek: (time) => this.seekToTime(time),
        onEpisodeChange: (episode) => {
            console.log('Episode changed:', episode.name);
        }
    });
}
```

**Capabilities**:
- ✅ **Episode labels** - Visual labels above timeline
- ✅ **Click navigation** - Click episode labels to jump
- ✅ **Keyboard shortcuts** - ←/→ for prev/next episode
- ✅ **Current episode tracking** - Shows which episode you're in
- ✅ **Episode boundaries** - Visual dividers between episodes
- ✅ **Tooltips** - Hover for episode details
- ✅ **Auto-update** - Syncs with playback
- ✅ **Mobile responsive**

**Assessment**: **EXCELLENT** - Full-featured timeline component with all desired functionality.

---

### 1.3 segmentation_viewer.html (Episode-Based Timeline)

**File**: `/path/to/openadapt-viewer/segmentation_viewer.html`

**Timeline Features**:
```javascript
function renderTimeline(episode) {
    const segment = document.createElement('div');
    segment.className = 'timeline-segment';
    const startPercent = (episodeStart / totalDuration) * 100;
    const widthPercent = (episodeDuration / totalDuration) * 100;
    segment.style.left = startPercent + '%';
    segment.style.width = widthPercent + '%';
    segment.textContent = formatDuration(episodeDuration);
}
```

**Capabilities**:
- ✅ **Segment visualization** - Shows episode duration
- ✅ **Hover effects** - Segment highlights on hover
- ✅ **Duration display** - Shows time in segment
- ❌ No click-to-seek (displays only)
- ❌ No multiple segments (single episode view)

**CSS**:
```css
.timeline-segment {
    position: absolute;
    height: 100%;
    background: linear-gradient(135deg, #00d9ff 0%, #00ff88 100%);
    border-radius: 20px;
    transition: all 0.3s ease;
}

.timeline-segment:hover {
    filter: brightness(1.2);
    z-index: 10;
}
```

**Assessment**: **GOOD** - Visual segments with hover effects, but limited to single episode view.

---

### 1.4 synthetic_demo_viewer.html (No Timeline)

**File**: `/path/to/openadapt-viewer/synthetic_demo_viewer.html`

**Timeline Features**: NONE

**Assessment**: **N/A** - Demo viewer doesn't need timeline (text-based demos).

---

### 1.5 episode_timeline.js (Reusable Component)

**File**: `/path/to/openadapt_viewer/components/episode_timeline.js`

**Core Features**:
```javascript
class EpisodeTimeline {
    constructor(options) {
        this.episodes = options.episodes || [];
        this.currentTime = options.currentTime || 0;
        this.totalDuration = options.totalDuration;
        this.onSeek = options.onSeek || (() => {});
        this.onEpisodeChange = options.onEpisodeChange || (() => {});
    }

    renderLabels() {
        const labels = this.episodes.map((episode, index) => {
            const left = (episode.start_time / this.totalDuration) * 100;
            const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;
            return `<div class="oa-episode-label"
                         data-episode-id="${episode.episode_id}"
                         style="left: ${left}%; width: ${width}%;"
                         role="button">
                      <span>${episode.name}</span>
                    </div>`;
        });
    }
}
```

**Capabilities**:
- ✅ **Reusable component** - Can be integrated anywhere
- ✅ **Event-driven** - onSeek, onEpisodeChange callbacks
- ✅ **Visual labels** - Episode names displayed
- ✅ **Click navigation** - Jump to episode start
- ✅ **Keyboard shortcuts** - Full keyboard control
- ✅ **State management** - Tracks current episode
- ✅ **Accessibility** - ARIA labels, keyboard support
- ✅ **Configurable** - Many options

**Assessment**: **BEST-IN-CLASS** - Production-ready component, needs adaptation for task-based view.

---

## 2. Missing Features in Current Benchmark Viewer

### 2.1 Click Segment to Play on Repeat

**Current State**: No segment concept exists in benchmark viewer.

**Desired Behavior**:
1. User clicks a task segment in timeline
2. Viewer jumps to first step of that task
3. Plays through all steps of that task
4. Option to loop/repeat the task
5. Visual indicator shows "looping task X"

**Implementation Needed**:
```javascript
// Alpine.js data
{
    playbackMode: 'normal', // or 'loop-task'
    loopingTaskId: null,

    playTask(taskId) {
        const task = this.tasks.find(t => t.id === taskId);
        this.currentStep = task.firstStepIndex;
        this.playbackMode = 'loop-task';
        this.loopingTaskId = taskId;
        this.startPlayback();
    },

    onStepAdvance() {
        if (this.playbackMode === 'loop-task') {
            const task = this.tasks.find(t => t.id === this.loopingTaskId);
            if (this.currentStep >= task.lastStepIndex) {
                this.currentStep = task.firstStepIndex; // Loop back
            }
        }
    }
}
```

---

### 2.2 Visual Segment Indicators

**Current State**: Timeline is a simple progress bar with no task boundaries.

**Desired Behavior**:
```
Timeline:
┌──────────────────────────────────────────────────────┐
│ Task 001    │  Task 002  │   Task 003    │ Task 004 │
│ ████████    │  ████      │   ██████      │ ███      │
└──────────────────────────────────────────────────────┘
       ↑ Current position
```

**Implementation Needed**:
```html
<div class="oa-timeline-track">
    <!-- Render segments for each task -->
    <template x-for="task in tasks">
        <div class="oa-task-segment"
             :style="`left: ${taskStartPercent(task)}%;
                      width: ${taskWidthPercent(task)}%;`"
             :class="{'active': task.id === currentTask.id}"
             @click="playTask(task.id)">
            <span class="task-label" x-text="task.id"></span>
        </div>
    </template>

    <!-- Current position marker -->
    <div class="oa-current-marker" :style="`left: ${currentProgress}%`"></div>
</div>
```

**CSS Needed**:
```css
.oa-task-segment {
    position: absolute;
    height: 100%;
    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
    border-right: 2px solid rgba(255, 255, 255, 0.2);
    cursor: pointer;
    transition: all 0.2s ease;
}

.oa-task-segment:hover {
    filter: brightness(1.3);
    transform: translateY(-2px);
}

.oa-task-segment.active {
    background: linear-gradient(135deg, #00d4aa 0%, #00ff88 100%);
    box-shadow: 0 0 0 2px var(--oa-accent);
}

.task-label {
    position: absolute;
    top: -20px;
    left: 50%;
    transform: translateX(-50%);
    font-size: 10px;
    color: var(--oa-text-muted);
    white-space: nowrap;
}
```

---

### 2.3 Progress Tracking Per Segment

**Current State**: Only global progress tracking.

**Desired Behavior**:
- Each task segment shows its own completion percentage
- Visual fill shows how far through the current task
- Completed tasks are fully filled, current task partially filled, future tasks empty

**Implementation**:
```javascript
taskProgress(task) {
    const taskSteps = this.getTaskSteps(task);
    const completedSteps = taskSteps.filter((_, idx) =>
        taskSteps[idx] <= this.currentStep
    ).length;
    return (completedSteps / taskSteps.length) * 100;
}
```

```html
<div class="oa-task-segment">
    <div class="task-progress-fill"
         :style="`width: ${taskProgress(task)}%`"></div>
</div>
```

---

### 2.4 Keyboard Shortcuts

**Current State**: No keyboard navigation for tasks.

**Desired Keyboard Controls**:
- `1-9`: Jump to task 1-9 directly
- `[` / `]`: Previous/Next task
- `L`: Toggle loop mode for current task
- `Home`: Jump to first task
- `End`: Jump to last task
- Space, ←, →: Already implemented for steps

**Implementation**:
```javascript
@keydown.window="handleKeyPress($event)"

handleKeyPress(e) {
    // Number keys for direct task access
    if (e.key >= '1' && e.key <= '9') {
        const taskIndex = parseInt(e.key) - 1;
        if (taskIndex < this.tasks.length) {
            this.playTask(this.tasks[taskIndex].id);
        }
    }

    // [ ] for prev/next task
    if (e.key === '[') this.prevTask();
    if (e.key === ']') this.nextTask();

    // L for loop toggle
    if (e.key === 'l' || e.key === 'L') {
        this.toggleLoopMode();
    }
}
```

---

### 2.5 Playback Speed Controls

**Current State**: ❌ Not implemented in benchmark viewer (though in capture viewer)

**Desired Behavior**: Speed selector: 0.5x, 1x, 2x, 4x

**Implementation**: Already exists in capture_viewer.html, can be copied directly.

---

## 3. Comparison Matrix

| Feature | benchmark_viewer.html | capture_viewer.html | segmentation_viewer.html | episode_timeline.js |
|---------|----------------------|---------------------|-------------------------|---------------------|
| **Visual Segments** | ❌ | ✅ | ✅ | ✅ |
| **Click to Seek** | ❌ | ✅ | ❌ | ✅ |
| **Segment Labels** | ❌ | ✅ | ✅ | ✅ |
| **Current Indicator** | ❌ | ✅ | ✅ | ✅ |
| **Hover Tooltips** | ❌ | ✅ | ❌ | ✅ |
| **Keyboard Nav** | ❌ | ✅ | ❌ | ✅ |
| **Progress Per Segment** | ❌ | ✅ | ✅ | ✅ |
| **Segment Repeat/Loop** | ❌ | ❌ | ❌ | ❌ |
| **Playback Speed** | ❌ | ✅ | ❌ | ❌ |
| **Alpine.js Integration** | ❌ | ✅ | ✅ | ⚠️ (vanilla JS) |

**Legend**: ✅ = Implemented, ❌ = Missing, ⚠️ = Partial/Different

---

## 4. Best Timeline Features from Each Viewer

### 4.1 From capture_viewer.html
```javascript
// Episode timeline integration with full state management
episodeTimeline: null,
episodes: [],
currentEpisodeIndex: -1,

async init() {
    await this.loadEpisodes();
    if (this.episodes.length > 0) {
        this.$nextTick(() => {
            this.initializeEpisodeTimeline();
        });
    }
},

initializeEpisodeTimeline() {
    this.episodeTimeline = new EpisodeTimeline({
        container: document.getElementById('episode-timeline-container'),
        episodes: this.episodes,
        currentTime: this.getCurrentTime(),
        totalDuration: this.getTotalDuration(),
        onSeek: (time) => this.seekToTime(time),
        onEpisodeChange: (episode) => {
            console.log('Episode changed:', episode.name);
        }
    });
}
```

**Key Learnings**:
- Use EpisodeTimeline component
- Integrate with Alpine.js lifecycle
- Provide callbacks for seek and change events
- Update timeline on every time change via `x-effect`

---

### 4.2 From segmentation_viewer.html
```css
.timeline-segment {
    position: absolute;
    height: 100%;
    background: linear-gradient(135deg, #00d9ff 0%, #00ff88 100%);
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #0a0a0f;
    font-size: 0.85em;
    font-weight: bold;
    transition: all 0.3s ease;
}

.timeline-segment:hover {
    filter: brightness(1.2);
    z-index: 10;
}
```

**Key Learnings**:
- Use absolute positioning for segments
- Gradients make segments visually appealing
- Hover effects improve interactivity
- Show duration inside segment

---

### 4.3 From episode_timeline.js
```javascript
handleLabelClick(e) {
    if (!this.config.enableClickNavigation) return;
    const episodeId = e.currentTarget.dataset.episodeId;
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);
    if (episode) {
        this.seekToEpisode(episode);
    }
}

handleKeydown(e) {
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
            this.seekToEpisode(this.episodes[0]);
            break;
        // Number keys 1-9 for direct access
        default:
            if (e.key >= '1' && e.key <= '9') {
                const index = parseInt(e.key) - 1;
                if (index < this.episodes.length) {
                    this.seekToEpisode(this.episodes[index]);
                }
            }
    }
}
```

**Key Learnings**:
- Comprehensive keyboard shortcuts
- Click handlers with data attributes
- Configurable navigation
- Number key direct access

---

## 5. Recommended Implementation Plan

### Phase 1: Basic Task Segmentation (Foundation)

**Goal**: Show task boundaries in timeline

**Steps**:
1. Map benchmark tasks to "episodes" format
2. Calculate task start/end times from step indices
3. Render visual segments for each task
4. Add task labels above timeline

**Code**:
```javascript
// In Alpine.js component
tasks: [],

init() {
    this.tasks = this.computeTaskSegments();
},

computeTaskSegments() {
    return benchmarkTasks.map(task => ({
        id: task.id,
        name: task.id,
        start_time: this.steps[task.firstStepIndex].timestamp,
        end_time: this.steps[task.lastStepIndex].timestamp,
        duration: task.duration,
        steps: task.steps
    }));
}
```

---

### Phase 2: Click-to-Seek Functionality

**Goal**: Click task segment to jump to that task

**Steps**:
1. Add click handlers to task segments
2. Calculate clicked task from mouse position
3. Seek to task start time
4. Update current task indicator

**Code**:
```javascript
seekToTask(taskId) {
    const task = this.tasks.find(t => t.id === taskId);
    if (task) {
        // Find first step of this task
        this.currentStep = this.findStepIndex(task.start_time);
        this.currentTaskId = taskId;
    }
}
```

---

### Phase 3: Segment Loop/Repeat

**Goal**: Play task segment on repeat

**Steps**:
1. Add "Loop Task" button/toggle
2. Track loop mode state
3. Auto-restart task when reaching last step
4. Visual indicator for looping

**Code**:
```javascript
playbackMode: 'normal', // or 'loop-task'
loopingTaskId: null,

toggleLoopMode() {
    if (this.playbackMode === 'loop-task') {
        this.playbackMode = 'normal';
        this.loopingTaskId = null;
    } else {
        this.playbackMode = 'loop-task';
        this.loopingTaskId = this.currentTaskId;
    }
},

// Modify playback advance logic
onPlaybackTick() {
    if (this.currentStep < this.totalSteps - 1) {
        this.currentStep++;
    } else if (this.playbackMode === 'loop-task') {
        // Restart task
        const task = this.tasks.find(t => t.id === this.loopingTaskId);
        this.currentStep = this.findStepIndex(task.start_time);
    } else {
        this.stopPlayback();
    }
}
```

---

### Phase 4: Enhanced UI & Keyboard Shortcuts

**Goal**: Polish and keyboard navigation

**Steps**:
1. Add keyboard shortcuts (1-9, [, ], L)
2. Hover tooltips for task segments
3. Visual feedback (current task highlight)
4. Progress fill per task

---

## 6. Code Templates

### 6.1 Enhanced Timeline Component (Alpine.js)

```html
<div x-data="benchmarkViewer()" x-init="init()">
    <!-- Task Timeline -->
    <div class="oa-timeline-section">
        <div class="oa-timeline-labels">
            <template x-for="task in tasks" :key="task.id">
                <div class="oa-task-label"
                     :style="`left: ${taskPosition(task)}%; width: ${taskWidth(task)}%;`"
                     :class="{'active': task.id === currentTaskId}"
                     @click="seekToTask(task.id)">
                    <span x-text="task.name"></span>
                </div>
            </template>
        </div>

        <div class="oa-timeline-track" @click="handleTimelineClick($event)">
            <!-- Task segments -->
            <template x-for="task in tasks" :key="task.id">
                <div class="oa-task-segment"
                     :style="`left: ${taskPosition(task)}%; width: ${taskWidth(task)}%;`"
                     :class="{'active': task.id === currentTaskId, 'looping': task.id === loopingTaskId}">
                    <!-- Progress fill within segment -->
                    <div class="task-progress"
                         :style="`width: ${taskProgress(task)}%`"></div>
                </div>
            </template>

            <!-- Current position marker -->
            <div class="oa-current-marker" :style="`left: ${currentProgress}%`"></div>
        </div>
    </div>

    <!-- Loop controls -->
    <div class="oa-loop-controls">
        <button @click="toggleLoopMode()"
                :class="{'active': playbackMode === 'loop-task'}">
            <span x-show="playbackMode !== 'loop-task'">Loop Task</span>
            <span x-show="playbackMode === 'loop-task'">Stop Loop</span>
        </button>
        <span x-show="playbackMode === 'loop-task'" x-text="`Looping: ${loopingTaskId}`"></span>
    </div>
</div>
```

---

### 6.2 Alpine.js Component Logic

```javascript
function benchmarkViewer() {
    return {
        // ... existing data ...
        tasks: [],
        currentTaskId: null,
        playbackMode: 'normal', // or 'loop-task'
        loopingTaskId: null,

        init() {
            this.tasks = this.computeTaskSegments();
            this.updateCurrentTask();
        },

        computeTaskSegments() {
            // Convert benchmark tasks to timeline segments
            return benchmarkData.tasks.map(task => ({
                id: task.id,
                name: task.id,
                start_time: this.steps[task.firstStepIndex].timestamp,
                end_time: this.steps[task.lastStepIndex].timestamp,
                duration: task.duration,
                firstStepIndex: task.firstStepIndex,
                lastStepIndex: task.lastStepIndex
            }));
        },

        taskPosition(task) {
            return (task.start_time / this.getTotalDuration()) * 100;
        },

        taskWidth(task) {
            return (task.duration / this.getTotalDuration()) * 100;
        },

        taskProgress(task) {
            if (this.currentStep < task.firstStepIndex) return 0;
            if (this.currentStep > task.lastStepIndex) return 100;

            const taskStepCount = task.lastStepIndex - task.firstStepIndex + 1;
            const completedSteps = this.currentStep - task.firstStepIndex + 1;
            return (completedSteps / taskStepCount) * 100;
        },

        seekToTask(taskId) {
            const task = this.tasks.find(t => t.id === taskId);
            if (task) {
                this.currentStep = task.firstStepIndex;
                this.currentTaskId = taskId;
            }
        },

        updateCurrentTask() {
            // Find which task we're currently in
            for (const task of this.tasks) {
                if (this.currentStep >= task.firstStepIndex &&
                    this.currentStep <= task.lastStepIndex) {
                    this.currentTaskId = task.id;
                    break;
                }
            }
        },

        toggleLoopMode() {
            if (this.playbackMode === 'loop-task') {
                this.playbackMode = 'normal';
                this.loopingTaskId = null;
            } else {
                this.playbackMode = 'loop-task';
                this.loopingTaskId = this.currentTaskId;
            }
        },

        // Override nextStep to support looping
        nextStep() {
            if (this.currentStep < this.totalSteps - 1) {
                this.currentStep++;
            } else if (this.playbackMode === 'loop-task') {
                // Loop back to start of current task
                const task = this.tasks.find(t => t.id === this.loopingTaskId);
                this.currentStep = task.firstStepIndex;
            }
            this.updateCurrentTask();
        },

        handleTimelineClick(e) {
            const rect = e.currentTarget.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const percent = clickX / rect.width;
            const targetTime = percent * this.getTotalDuration();

            // Find step closest to this time
            for (let i = 0; i < this.steps.length; i++) {
                if (this.steps[i].timestamp >= targetTime) {
                    this.currentStep = i;
                    this.updateCurrentTask();
                    break;
                }
            }
        }
    };
}
```

---

### 6.3 Enhanced CSS

```css
/* Task Timeline Section */
.oa-timeline-section {
    margin-bottom: 24px;
}

.oa-timeline-labels {
    position: relative;
    height: 24px;
    margin-bottom: 8px;
}

.oa-task-label {
    position: absolute;
    top: 0;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    color: var(--oa-text-muted);
    cursor: pointer;
    transition: all 0.2s ease;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.03);
}

.oa-task-label:hover {
    background: rgba(255, 255, 255, 0.08);
    color: var(--oa-text-primary);
}

.oa-task-label.active {
    background: var(--oa-accent-dim);
    color: var(--oa-accent);
}

/* Task Segments */
.oa-task-segment {
    position: absolute;
    top: 0;
    height: 100%;
    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
    border-right: 1px solid rgba(255, 255, 255, 0.1);
    cursor: pointer;
    transition: all 0.2s ease;
    overflow: hidden;
}

.oa-task-segment:hover {
    filter: brightness(1.3);
    transform: translateY(-1px);
}

.oa-task-segment.active {
    background: linear-gradient(135deg, #00d4aa 0%, #00ff88 100%);
    box-shadow: 0 0 0 2px var(--oa-accent);
}

.oa-task-segment.looping {
    background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
    animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.7; }
}

/* Task Progress Fill */
.task-progress {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    background: rgba(255, 255, 255, 0.3);
    transition: width 0.2s ease;
}

/* Current Position Marker */
.oa-current-marker {
    position: absolute;
    top: -4px;
    width: 3px;
    height: calc(100% + 8px);
    background: var(--oa-accent);
    box-shadow: 0 0 8px var(--oa-accent);
    transition: left 0.1s linear;
    z-index: 10;
}

/* Loop Controls */
.oa-loop-controls {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 12px;
}

.oa-loop-controls button {
    padding: 8px 16px;
    border: 1px solid var(--oa-border-color);
    border-radius: 6px;
    background: var(--oa-bg-tertiary);
    color: var(--oa-text-primary);
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.2s ease;
}

.oa-loop-controls button:hover {
    background: var(--oa-accent-dim);
    border-color: var(--oa-accent);
    color: var(--oa-accent);
}

.oa-loop-controls button.active {
    background: var(--oa-warning-bg);
    border-color: var(--oa-warning);
    color: var(--oa-warning);
}
```

---

## 7. Testing Plan

### 7.1 Unit Tests
- [ ] Task segment calculation (start/end times)
- [ ] Task progress calculation
- [ ] Loop mode logic
- [ ] Keyboard shortcut handlers

### 7.2 Integration Tests
- [ ] Click segment → jumps to task
- [ ] Loop mode → restarts task at end
- [ ] Keyboard shortcuts → navigate tasks
- [ ] Visual updates → segments highlight correctly

### 7.3 Manual Testing
- [ ] Test with 5-task benchmark
- [ ] Test with 20-task benchmark
- [ ] Test loop mode for each task
- [ ] Test all keyboard shortcuts
- [ ] Test mobile responsiveness

---

## 8. Deliverables Checklist

- [ ] **Comprehensive Comparison Document** (this file)
- [ ] **Enhanced Timeline Component Code**
  - [ ] HTML template
  - [ ] Alpine.js logic
  - [ ] CSS styling
- [ ] **Updated benchmark_viewer.html**
  - [ ] Integrated task segments
  - [ ] Click-to-seek functionality
  - [ ] Loop mode controls
  - [ ] Keyboard shortcuts
- [ ] **Documentation**
  - [ ] User guide for timeline features
  - [ ] Developer guide for extending timeline
  - [ ] Keyboard shortcuts reference
- [ ] **Tests**
  - [ ] Automated tests for core logic
  - [ ] Manual test scenarios
  - [ ] Visual regression tests

---

## 9. Next Steps

### Immediate (Phase 1)
1. ✅ Complete this analysis document
2. Extract task segment calculation logic
3. Create visual segments in benchmark_viewer.html
4. Add task labels above timeline

### Short-term (Phase 2-3)
5. Implement click-to-seek for tasks
6. Add loop mode functionality
7. Visual feedback for current/looping task
8. Test with sample benchmark data

### Long-term (Phase 4)
9. Keyboard shortcuts
10. Hover tooltips
11. Progress indicators per task
12. Performance optimization for 100+ tasks

---

## 10. Appendix: Code Locations

### Key Files Reviewed
- `/path/to/openadapt-viewer/benchmark_viewer.html` - Current refactored version (minimal timeline)
- `/path/to/openadapt-viewer/capture_viewer.html` - Best reference implementation
- `/path/to/openadapt-viewer/segmentation_viewer.html` - Episode-based timeline
- `/path/to/openadapt-viewer/src/openadapt_viewer/components/episode_timeline.js` - Reusable component
- `/path/to/openadapt-viewer/test_benchmark_refactored.html` - Test file
- `/path/to/openadapt-viewer/viewers/benchmark/minimal_viewer.html` - Minimal reference

### Related Documentation
- `/path/to/openadapt-viewer/CLAUDE.md` - Episode Timeline Component section
- `/path/to/openadapt-viewer/EPISODE_TIMELINE_DESIGN.md` - Component design doc
- `/path/to/openadapt-viewer/EPISODE_TIMELINE_QUICKSTART.md` - Integration guide

---

**Analysis completed**: January 17, 2026
**Estimated implementation time**: 8-12 hours
**Priority**: P0 (timeline degradation blocks effective benchmark review)
