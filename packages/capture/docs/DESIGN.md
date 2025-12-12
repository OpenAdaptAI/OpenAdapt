# openadapt-capture Design

## Problem Statement

We need a platform-agnostic representation of GUI interactions that:

1. Can be captured from any OS (macOS, Windows, Linux)
2. Can be scrubbed for privacy (via `openadapt-privacy`)
3. Can be replayed for automation
4. Can be used for ML training (via `openadapt-ml`)
5. Works as a generic library that any system can adapt to

## Design Goals

`openadapt-capture` is designed for **production use** - it should run uninterrupted for days without hindering the user.

### Key Features

1. **Production-ready** - Designed for continuous operation without degradation
2. **No external dependencies** - Native video capture via PyAV (no OBS required)
3. **Low resource footprint** - Non-blocking capture that doesn't slow down user's work
4. **Chunked media** - Video/audio split into manageable segments for long captures
5. **Audio capture** - Built-in audio recording with Whisper transcription
6. **Privacy-first** - Designed to integrate with `openadapt-privacy` for scrubbing
7. **Multi-process architecture** - Optimized queues for high-throughput event handling

### Production Requirements

For continuous capture over days/weeks:

1. **Memory bounded** - Stream events to disk, don't accumulate in RAM
2. **Chunked video** - Split into segments (e.g., 10 min each) to avoid giant files
3. **Graceful recovery** - Handle crashes, resume without data loss
4. **Minimal CPU** - Capture shouldn't impact user's work
5. **Disk management** - Configurable retention, auto-cleanup of old captures

## Scope Decision: Accessibility Data

OpenAdapt currently captures **accessibility tree data** (element state, UI hierarchy) via platform-specific APIs:
- macOS: `ApplicationServices` / `AXUIElement`
- Windows: `UIAutomation`
- Linux: `AT-SPI`

### Recommendation

**Start vision-only, add accessibility as optional layer later.**

The core capture should be:
- Input events (mouse, keyboard, scroll)
- Screen frames (video)
- Audio (optional)

Accessibility data can be added as an optional enrichment step, not a core requirement.

### Window Events

**Decision:** Exclude window change events from core capture.

Without accessibility data, window focus/bounds changes have limited value:
- We already have screenshots showing window state
- Window metadata without accessibility tree is just title + bounds
- If needed, can be added as optional stream later

## Terminology

| Concept | Term | Rationale |
|---------|------|-----------|
| Container | **Capture** | Avoids "Recording" (implies audio/video), "Session" (overloaded) |
| Atomic unit | **Event** | Standard term across systems |
| Event sequence | **Stream** | Time-ordered events of one type |
| Multi-capture | **Sequence** | Optional, for workflows |

## Event Types

### Raw Events (captured)

Record primitive events, combine them in post-processing:

```python
# Mouse events
"mouse.move"     # x, y
"mouse.down"     # x, y, button
"mouse.up"       # x, y, button
"mouse.scroll"   # x, y, dx, dy

# Keyboard events
"key.down"       # key, key_char, modifiers
"key.up"         # key, key_char, modifiers

# Screen events
"screen.frame"   # reference to video timestamp or image path

# Audio events (optional)
"audio.chunk"    # reference to audio file + timestamp range
```

### Derived Events (post-processing)

Combine raw events into higher-level actions (see OpenAdapt's `events.py`):

```python
# Derived from mouse.down + mouse.up
"mouse.click"        # single click
"mouse.doubleclick"  # two clicks within threshold
"mouse.drag"         # down + move + up (TODO: add to OpenAdapt)

# Derived from key.down + key.up sequences
"key.type"           # sequence of characters typed
"key.shortcut"       # modifier + key combination
```

### Event Processing Pipeline

Based on OpenAdapt's `events.py`:

1. `remove_invalid_keyboard_events` - Filter invalid key codes
2. `remove_redundant_mouse_move_events` - Remove moves that don't change position
3. `merge_consecutive_keyboard_events` - Combine key sequences into "type" events
4. `merge_consecutive_mouse_move_events` - Reduce move event density
5. `merge_consecutive_mouse_scroll_events` - Combine scroll events
6. `merge_consecutive_mouse_click_events` - Detect single/double clicks

**TODO:** Add `mouse.drag` detection (currently missing from OpenAdapt).

## Proposed Abstraction

### Event Schema

```python
@dataclass
class Event:
    timestamp: float  # Unix timestamp (seconds, float for sub-ms)
    type: str         # Event type identifier
    data: dict        # Event-specific payload
```

### Stream Schema

```python
@dataclass
class Stream:
    id: str
    type: str  # "action" | "screen" | "audio"
    events: list[Event]
```

**Note:** Using "action" not "input" - clearer terminology.

### Capture Schema

```python
@dataclass
class Capture:
    id: str
    started_at: float
    ended_at: float | None
    platform: str  # "darwin" | "win32" | "linux"
    screen_dimensions: tuple[int, int]  # For coordinate normalization
    streams: dict[str, Stream]
    metadata: dict  # task_description, etc.
```

## Media Handling

### Video Encoding

Based on OpenAdapt's working implementation:

```python
codec = "libx264"      # H.264 for compatibility
pix_fmt = "yuv444p"    # Full color (vs yuv420p for smaller files)
crf = 0                # Lossless (adjustable for size vs quality)
preset = "veryslow"    # Maximum compression
fps = 24               # Configurable
```

### Storage: SQLite vs Filesystem

OpenAdapt uses SQLite for events. Benchmarks show it's faster than filesystem for:
- Many small writes (events)
- Querying by timestamp
- Atomic transactions

**Recommendation:** SQLite for events, filesystem for media (video, audio).

```
capture_abc123/
├── capture.db            # SQLite: events, metadata
├── screen/
│   └── video.mp4         # Or chunked: video_001.mp4, video_002.mp4, ...
└── audio/
    └── audio.flac        # Compressed audio
```

### Screenshots vs Video

**Recommendation:** Default to video mode.

- More storage-efficient (H.264 compression)
- Easier timestamp alignment
- Screenshots can be extracted from video when needed
- Individual screenshots useful for debugging frame alignment

## Audio Handling

Based on OpenAdapt's implementation:

1. **Capture:** `sounddevice.InputStream` at 16kHz mono
2. **Storage:** FLAC compression (lossless, ~50% size reduction)
3. **Transcription:** Whisper with word-level timestamps
4. **Schema:**
   ```python
   AudioInfo:
       flac_data: bytes
       transcribed_text: str
       sample_rate: int
       words_with_timestamps: list[dict]  # [{"word": "hello", "start": 0.5, "end": 0.8}]
   ```

Transcription stored separately from audio stream events, linked by timestamp.

## Coordinate Handling

**Decision:** Store absolute pixels, include screen dimensions in metadata.

```python
Capture:
    screen_dimensions: (1920, 1080)

Event (mouse.click):
    data: {x: 500, y: 300, button: "left"}
```

Normalization can happen at read time if needed:
```python
normalized_x = event.data["x"] / capture.screen_dimensions[0]
```

## Privacy Integration

Scrubbing operates at the Capture level:

```python
from openadapt_privacy import PresidioScrubbingProvider

def scrub_capture(capture: Capture, scrubber: ScrubbingProvider) -> Capture:
    """Return a new Capture with PII removed."""
    # Scrub metadata (task_description, etc.)
    # Scrub any text in key events
    # Scrub video frames
    # Scrub audio transcription
    ...
```

## Open Questions

1. **Drag detection:** How to detect drag events from mouse.down → move → mouse.up sequences?
   - Time threshold?
   - Distance threshold?
   - Review other implementations

2. **Video chunking:** What segment duration for long captures?
   - 10 minutes? 1 hour?
   - Based on file size or time?

3. **SQLite schema:** Match OpenAdapt's schema for compatibility, or start fresh?

## Next Steps

1. Define exact event schemas (Pydantic models)
2. Implement SQLite storage for events
3. Implement video capture with PyAV
4. Port event processing from OpenAdapt's `events.py`
5. Add drag detection
6. Integrate with `openadapt-privacy`
7. Add audio capture
