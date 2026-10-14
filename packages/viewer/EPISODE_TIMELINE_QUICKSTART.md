# Episode Timeline Integration - Quick Start Guide

**TL;DR**: Add episode-aware timeline to capture viewer with labels, boundaries, and navigation.

## Quick Links

- **Full Design**: [EPISODE_TIMELINE_DESIGN.md](EPISODE_TIMELINE_DESIGN.md)
- **Technical Details**: [EPISODE_TIMELINE_DESIGN_PART2.md](EPISODE_TIMELINE_DESIGN_PART2.md)
- **Testing & Advanced**: [EPISODE_TIMELINE_DESIGN_PART3.md](EPISODE_TIMELINE_DESIGN_PART3.md)

## What You Get

```
Before:
  Timeline:  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  (just a bar, no context)

After:
  Episodes:  [Navigate to Settings] [Disable Night Shift]
  Timeline:  ━━━━━━━━━━━━━|━━━━━━━━━━━━━━━━━━
             0s          3.5s               6.7s
                           ●
  [◄ Prev Episode]  [Play]  [Next Episode ►]
```

**Key Features**:
- ✅ Episode labels above timeline
- ✅ Click label to jump to episode
- ✅ Episode boundaries marked
- ✅ Current episode highlighted
- ✅ Keyboard shortcuts (←/→ for prev/next)
- ✅ Hover for episode details
- ✅ Auto-advance between episodes
- ✅ Mobile responsive

## 5-Minute Integration

### 1. Add CSS

```html
<link rel="stylesheet" href="styles/episode_timeline.css">
```

### 2. Add JavaScript

```html
<script src="components/episode_timeline.js"></script>
```

### 3. Load Episodes

```javascript
async function loadEpisodes(recordingId) {
  const response = await fetch(`../episodes/${recordingId}_episodes.json`);
  const data = await response.json();
  return data.episodes;
}

const episodes = await loadEpisodes('turn-off-nightshift');
```

### 4. Initialize Timeline

```javascript
const timeline = new EpisodeTimeline({
  container: document.getElementById('timeline-container'),
  episodes: episodes,
  currentTime: 0,
  totalDuration: 6.7,
  onSeek: (time) => {
    // Your seek logic here
    player.seek(time);
  },
  onEpisodeChange: (episode) => {
    console.log('Now in:', episode.name);
  }
});
```

### 5. Update on Playback

```javascript
// In your playback loop
function onTimeUpdate(currentTime) {
  timeline.update({ currentTime });
}
```

Done! Timeline now shows episodes.

## File Structure

```
openadapt-viewer/
├── src/openadapt_viewer/
│   ├── components/
│   │   └── episode_timeline.js      ← Core component
│   └── styles/
│       └── episode_timeline.css     ← Styles
├── capture_viewer.html              ← Integrate here
└── test_episodes.json               ← Example data
```

## Episode Data Format

```json
{
  "episodes": [
    {
      "episode_id": "episode_001",
      "name": "Navigate to System Settings",
      "description": "User opens System Settings...",
      "start_time": 0.0,
      "end_time": 3.5,
      "duration": 3.5,
      "steps": ["Click Settings icon", "Wait for window", "Click Displays"],
      "boundary_confidence": 0.92,
      "screenshots": {
        "thumbnail": "path/to/thumbnail.png",
        "key_frames": [...]
      }
    }
  ]
}
```

## Common Tasks

### Change Episode Colors

```css
:root {
  --episode-1-bg: linear-gradient(135deg, #your-color 0%, #your-color-dark 100%);
  --episode-2-bg: linear-gradient(135deg, #another-color 0%, #another-color-dark 100%);
}
```

### Disable Features

```javascript
const timeline = new EpisodeTimeline({
  // ... other options
  config: {
    showLabels: true,           // Show episode labels
    showBoundaries: true,        // Show boundary markers
    enableClickNavigation: true, // Allow clicking to jump
    enableAutoAdvance: false,    // Don't auto-advance episodes
    labelTruncate: 30           // Max label length
  }
});
```

### Keyboard Shortcuts

- `←` / `→`: Previous/Next episode
- `Home` / `End`: First/Last episode
- `1-9`: Jump to episode by number
- `Space`: Play/Pause

### Mobile Support

Automatically responsive. On mobile:
- Labels stack vertically
- Touch gestures work (swipe left/right)
- Long-press shows episode details

## Testing

```bash
# Run tests
uv run pytest tests/test_episode_timeline*.py -v

# Interactive demo
open test_episode_timeline.html
```

## Implementation Phases

**Phase 1 (Week 1)** - MVP:
- Basic labels and timeline
- Click to jump
- Current episode indicator

**Phase 2 (Week 2)** - Enhanced UX:
- Tooltips on hover
- Navigation buttons
- Color coding
- Animations

**Phase 3 (Week 3)** - Polish:
- Mobile responsive
- Accessibility
- Keyboard shortcuts

**Phase 4 (Month 2)** - Advanced:
- Bookmarks
- Episode comparison
- Analytics
- User refinement

## Troubleshooting

### Episodes not showing

1. Check console for errors
2. Verify episode JSON structure
3. Ensure `container` element exists
4. Check `totalDuration` is correct

### Timeline not updating

1. Call `timeline.update({ currentTime })` on each time change
2. Check `currentTime` is in seconds (not milliseconds)
3. Verify `totalDuration` matches episode end times

### Click not seeking

1. Ensure `onSeek` callback is provided
2. Check callback is called (add `console.log`)
3. Verify your player's seek function works

### Styling looks wrong

1. Ensure CSS file is loaded
2. Check for CSS conflicts (inspect element)
3. Verify CSS variables are defined

## Examples

### Alpine.js Integration

```html
<div x-data="{
  episodes: [],
  currentTime: 0,
  timeline: null,

  async init() {
    this.episodes = await loadEpisodes();
    this.$nextTick(() => {
      this.timeline = new EpisodeTimeline({
        container: this.$refs.timeline,
        episodes: this.episodes,
        currentTime: this.currentTime,
        totalDuration: this.getTotalDuration(),
        onSeek: (time) => this.seekToTime(time)
      });
    });
  },

  seekToTime(time) {
    // Find step at this time and update currentStep
    for (let i = 0; i < this.steps.length; i++) {
      if (this.steps[i].timestamp >= time) {
        this.currentStep = i;
        break;
      }
    }
  },

  // Watch currentTime and update timeline
  $watch('currentTime', (newTime) => {
    if (this.timeline) {
      this.timeline.update({ currentTime: newTime });
    }
  })
}">
  <div x-ref="timeline"></div>
</div>
```

### React Integration

```jsx
import { useEffect, useRef } from 'react';
import EpisodeTimeline from './components/episode_timeline.js';

function CaptureViewer({ episodes, currentTime, onSeek }) {
  const timelineRef = useRef(null);
  const timelineInstance = useRef(null);

  useEffect(() => {
    if (timelineRef.current && episodes.length > 0) {
      timelineInstance.current = new EpisodeTimeline({
        container: timelineRef.current,
        episodes,
        currentTime,
        totalDuration: episodes[episodes.length - 1].end_time,
        onSeek
      });
    }

    return () => {
      if (timelineInstance.current) {
        timelineInstance.current.destroy();
      }
    };
  }, [episodes]);

  useEffect(() => {
    if (timelineInstance.current) {
      timelineInstance.current.update({ currentTime });
    }
  }, [currentTime]);

  return <div ref={timelineRef}></div>;
}
```

## Resources

- **Design Docs**: See EPISODE_TIMELINE_DESIGN*.md files
- **API Reference**: See Part 3, Appendix B
- **CSS Classes**: See Part 3, Appendix C
- **Examples**: `/path/to/openadapt-viewer/test_episode_timeline.html`

## Support

For questions or issues:
1. Check full design documentation
2. Review test files for working examples
3. Open issue on GitHub with details

---

**Last Updated**: 2026-01-17
**Version**: 1.0
