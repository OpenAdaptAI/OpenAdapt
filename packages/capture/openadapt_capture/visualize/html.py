"""Generate interactive HTML viewer for capture recordings.

Creates a self-contained HTML file with timeline navigation,
frame viewing, event list, and audio playback.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

    from openadapt_capture.capture import CaptureSession


def create_html(
    capture_or_path: "CaptureSession | str | Path",
    output: str | Path | None = None,
    max_events: int | None = 200,
    include_audio: bool = True,
    frame_scale: float = 1.0,
    frame_quality: int = 85,
) -> str:
    """Generate an interactive HTML viewer for a capture recording.

    Args:
        capture_or_path: CaptureSession object or path to capture directory.
        output: Output path for HTML file. If None, returns HTML string.
        max_events: Maximum events to include (None for all).
        include_audio: Whether to include audio playback.
        frame_scale: Scale factor for embedded frames.
        frame_quality: JPEG quality for embedded frames (1-100).

    Returns:
        HTML string if output is None, otherwise None after writing file.
    """
    from openadapt_capture.capture import CaptureSession

    # Load capture if path provided
    if isinstance(capture_or_path, (str, Path)):
        capture = CaptureSession.load(capture_or_path)
        capture_path = Path(capture_or_path)
    else:
        capture = capture_or_path
        capture_path = capture.capture_dir

    # Get capture metadata
    capture_id = capture.id
    duration = capture.duration or 0
    screen_width, screen_height = capture.screen_size
    pixel_ratio = capture.pixel_ratio
    audio_start_time = capture._metadata.audio_start_time

    # Get actions
    actions = list(capture.actions())
    if max_events is not None and len(actions) > max_events:
        # Sample evenly
        step = len(actions) / max_events
        indices = [int(i * step) for i in range(max_events)]
        actions = [actions[i] for i in indices]

    # Prepare frame data
    frames_data = []
    events_data = []
    # Use audio_start_time as reference if available, otherwise first action
    start_time = audio_start_time if audio_start_time else (actions[0].timestamp if actions else 0)

    # Add a "start" marker at time 0 with the first frame from the video
    if actions and capture.video_path:
        first_frame = capture.get_frame_at(start_time)
        if first_frame:
            frame_b64 = _image_to_base64(first_frame, scale=frame_scale, quality=frame_quality)
            frames_data.append({
                "index": 0,
                "time": 0.0,
                "image": frame_b64,
            })
            events_data.append({
                "index": 0,
                "time": 0.0,
                "type": "recording.start",
            })

    # Offset for indices if we added start marker
    idx_offset = len(frames_data)

    for i, action in enumerate(actions):
        idx = i + idx_offset
        rel_time = action.timestamp - start_time
        event_type = action.type if isinstance(action.type, str) else action.type.value

        # Encode screenshot
        screenshot = action.screenshot
        if screenshot is not None:
            frame_b64 = _image_to_base64(screenshot, scale=frame_scale, quality=frame_quality)
        else:
            frame_b64 = ""

        frames_data.append({
            "index": idx,
            "time": rel_time,
            "image": frame_b64,
        })

        # Event data
        event_dict = {
            "index": idx,
            "time": rel_time,
            "type": event_type,
            "x": getattr(action, "x", None),
            "y": getattr(action, "y", None),
        }

        # Add type-specific fields
        if hasattr(action, "text"):
            event_dict["text"] = action.text
        if hasattr(action, "keys"):
            keys = action.keys
            if keys:
                event_dict["keys"] = "+".join(keys)
        if hasattr(action, "button"):
            event_dict["button"] = str(action.button)
        # dx/dy for drags and scrolls
        if hasattr(action.event, "dx"):
            event_dict["dx"] = action.event.dx
            event_dict["dy"] = action.event.dy

        events_data.append(event_dict)

    # Prepare audio data and get audio duration
    audio_b64 = ""
    audio_type = ""
    audio_duration = 0.0
    transcript = ""
    if include_audio:
        audio_path = capture_path / "audio.flac"
        if audio_path.exists():
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            audio_type = "audio/flac"
            # Get audio duration using soundfile
            try:
                import soundfile as sf
                info = sf.info(str(audio_path))
                audio_duration = info.duration
            except ImportError:
                pass
            # Load transcript if exists (prefer JSON with timestamps)
            transcript_json_path = capture_path / "transcript.json"
            transcript_path = capture_path / "transcript.txt"
            if transcript_json_path.exists():
                transcript = transcript_json_path.read_text(encoding="utf-8")
            elif transcript_path.exists():
                # Wrap plain text in JSON format
                plain_text = transcript_path.read_text(encoding="utf-8")
                transcript = json.dumps({"text": plain_text, "segments": []})
        else:
            # Try other formats
            for ext, mime in [(".mp3", "audio/mpeg"), (".wav", "audio/wav"), (".ogg", "audio/ogg")]:
                alt_path = capture_path / f"audio{ext}"
                if alt_path.exists():
                    with open(alt_path, "rb") as f:
                        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                    audio_type = mime
                    break

    # Calculate effective duration as max of audio duration and last event time
    last_event_time = events_data[-1]["time"] if events_data else 0
    effective_duration = max(audio_duration, duration, last_event_time)

    # Add an "end" marker at the end of the recording
    if effective_duration > 0 and capture.video_path:
        # Get the last frame from the video
        end_timestamp = start_time + effective_duration
        last_frame = capture.get_frame_at(end_timestamp)
        if last_frame:
            end_idx = len(frames_data)
            frame_b64 = _image_to_base64(last_frame, scale=frame_scale, quality=frame_quality)
            frames_data.append({
                "index": end_idx,
                "time": effective_duration,
                "image": frame_b64,
            })
            events_data.append({
                "index": end_idx,
                "time": effective_duration,
                "type": "recording.end",
            })

    # Generate HTML
    html = _generate_html(
        capture_id=capture_id,
        duration=effective_duration,
        frames_data=frames_data,
        events_data=events_data,
        audio_b64=audio_b64,
        audio_type=audio_type,
        screen_width=screen_width,
        screen_height=screen_height,
        pixel_ratio=pixel_ratio,
        transcript=transcript,
    )

    if output is not None:
        output = Path(output)
        output.write_text(html, encoding="utf-8")
        return None
    else:
        return html


def _image_to_base64(image: "Image.Image", scale: float = 1.0, quality: int = 85) -> str:
    """Convert PIL Image to base64 JPEG string.

    Args:
        image: PIL Image.
        scale: Scale factor.
        quality: JPEG quality.

    Returns:
        Base64-encoded JPEG string.
    """
    from PIL import Image

    if scale != 1.0:
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    # Convert to RGB if needed
    if image.mode in ("RGBA", "P"):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "RGBA":
            bg.paste(image, mask=image.split()[3])
        else:
            bg.paste(image)
        image = bg
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _generate_html(
    capture_id: str,
    duration: float,
    frames_data: list[dict],
    events_data: list[dict],
    audio_b64: str,
    audio_type: str,
    screen_width: int,
    screen_height: int,
    pixel_ratio: float,
    transcript: str = "",
) -> str:
    """Generate the HTML content.

    Args:
        capture_id: Capture identifier.
        duration: Recording duration in seconds (audio duration if available).
        frames_data: List of frame data dicts.
        events_data: List of event data dicts.
        audio_b64: Base64-encoded audio data.
        transcript: Optional transcript text.
        audio_type: Audio MIME type.
        screen_width: Screen width in physical pixels.
        screen_height: Screen height in physical pixels.
        pixel_ratio: Display pixel ratio (physical/logical).

    Returns:
        Complete HTML string.
    """
    # Format duration
    minutes = int(duration // 60)
    seconds = duration % 60
    duration_str = f"{minutes}:{seconds:05.2f}"

    # Serialize data to JSON
    frames_json = json.dumps(frames_data)
    events_json = json.dumps(events_data)

    # Build conditional HTML sections (Python 3.10 compatible)
    audio_controls_html = ""
    if audio_b64:
        audio_controls_html = """
                    <div class="audio-controls">
                        <label>🔊 Volume</label>
                        <input type="range" id="volume" min="0" max="1" step="0.1" value="0.5">
                        <label><input type="checkbox" id="mute"> Mute</label>
                    </div>
        """

    transcript_panel_html = ""
    if transcript:
        transcript_panel_html = """
                <div class="transcript-panel">
                    <h2>Transcript</h2>
                    <div class="transcript-content" id="transcript-content"></div>
                </div>
        """

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capture Viewer - {capture_id}</title>
    <style>
        :root {{
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-tertiary: #1a1a24;
            --border-color: rgba(255, 255, 255, 0.06);
            --text-primary: #f0f0f0;
            --text-secondary: #888;
            --text-muted: #555;
            --accent: #00d4aa;
            --accent-dim: rgba(0, 212, 170, 0.15);
            --accent-hover: #00ffcc;
            --click-color: #ff5f5f;
            --drag-color: #00d4aa;
            --scroll-color: #a78bfa;
            --type-color: #34d399;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: "SF Pro Display", -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }}

        .container {{
            max-width: 1440px;
            margin: 0 auto;
            padding: 24px;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 24px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            margin-bottom: 24px;
        }}

        header h1 {{
            font-size: 1.1rem;
            font-weight: 600;
            letter-spacing: -0.02em;
            color: var(--text-primary);
        }}

        header .meta {{
            color: var(--text-secondary);
            font-size: 0.85rem;
            font-family: "SF Mono", Monaco, "Cascadia Code", monospace;
        }}

        .main-content {{
            display: grid;
            grid-template-columns: 1fr 340px;
            gap: 24px;
        }}

        .viewer-section {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
        }}

        .frame-container {{
            position: relative;
            background: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 420px;
        }}

        .frame-container img {{
            max-width: 100%;
            max-height: 70vh;
            object-fit: contain;
        }}

        .frame-container canvas {{
            position: absolute;
            top: 0;
            left: 0;
            pointer-events: none;
        }}

        .frame-overlay {{
            position: absolute;
            top: 12px;
            right: 12px;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(8px);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-family: "SF Mono", Monaco, monospace;
            border: 1px solid var(--border-color);
        }}

        .controls {{
            padding: 16px 20px;
            background: var(--bg-tertiary);
            border-top: 1px solid var(--border-color);
        }}

        .playback-controls {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 16px;
        }}

        .playback-controls button {{
            background: var(--accent);
            border: none;
            color: var(--bg-primary);
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 0.95rem;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
            font-weight: 600;
        }}

        .playback-controls button:hover {{
            background: var(--accent-hover);
            transform: scale(1.05);
        }}

        .playback-controls button.nav {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            width: 32px;
            height: 32px;
            font-size: 0.8rem;
        }}

        .playback-controls button.nav:hover {{
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border-color: rgba(255, 255, 255, 0.12);
        }}

        .time-display {{
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.85rem;
            min-width: 110px;
            color: var(--text-secondary);
        }}

        .timeline {{
            width: 100%;
            height: 6px;
            background: var(--bg-secondary);
            border-radius: 3px;
            cursor: pointer;
            position: relative;
            border: 1px solid var(--border-color);
        }}

        .timeline-progress {{
            height: 100%;
            background: var(--accent);
            border-radius: 3px;
            width: 0%;
            transition: width 0.1s;
        }}

        .timeline-markers {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
        }}

        .timeline-marker {{
            position: absolute;
            width: 2px;
            height: 100%;
            background: rgba(255, 255, 255, 0.25);
            transform: translateX(-50%);
        }}

        .audio-controls {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
        }}

        .audio-controls label {{
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}

        .audio-controls input[type="range"] {{
            flex: 1;
            max-width: 100px;
            accent-color: var(--accent);
        }}

        .sidebar {{
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .events-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            flex: 1;
            display: flex;
            flex-direction: column;
            max-height: 50vh;
        }}

        .events-panel h2 {{
            padding: 14px 18px;
            font-size: 0.9rem;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
            letter-spacing: -0.01em;
        }}

        .events-list {{
            flex: 1;
            overflow-y: auto;
            padding: 8px;
        }}

        .events-list::-webkit-scrollbar {{
            width: 6px;
        }}

        .events-list::-webkit-scrollbar-track {{
            background: transparent;
        }}

        .events-list::-webkit-scrollbar-thumb {{
            background: var(--bg-tertiary);
            border-radius: 3px;
        }}

        .event-item {{
            padding: 10px 14px;
            border-radius: 8px;
            cursor: pointer;
            margin-bottom: 4px;
            transition: all 0.15s ease;
            font-size: 0.82rem;
            border: 1px solid transparent;
        }}

        .event-item:hover {{
            background: var(--bg-tertiary);
            border-color: var(--border-color);
        }}

        .event-item.active {{
            background: var(--accent-dim);
            border-color: var(--accent);
        }}

        .event-item .event-time {{
            color: var(--text-muted);
            font-family: "SF Mono", Monaco, monospace;
            margin-right: 10px;
            font-size: 0.75rem;
        }}

        .event-item.active .event-time {{
            color: var(--accent);
        }}

        .event-type {{
            font-weight: 500;
        }}

        .event-type.click {{ color: var(--click-color); }}
        .event-type.drag {{ color: var(--drag-color); }}
        .event-type.scroll {{ color: var(--scroll-color); }}
        .event-type.type {{ color: var(--type-color); }}

        .event-item.active .event-type {{
            color: var(--text-primary);
        }}

        .details-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            max-height: 30vh;
        }}

        .details-panel h2 {{
            padding: 14px 18px;
            font-size: 0.9rem;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .copy-btn {{
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.7rem;
            transition: all 0.15s ease;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            font-weight: 500;
        }}

        .copy-btn:hover {{
            background: var(--bg-secondary);
            color: var(--text-primary);
            border-color: rgba(255, 255, 255, 0.12);
        }}

        .copy-btn.copied {{
            background: var(--accent-dim);
            color: var(--accent);
            border-color: var(--accent);
        }}

        .overlay-toggle {{
            display: flex;
            align-items: center;
            margin-left: 12px;
            gap: 8px;
            cursor: pointer;
            user-select: none;
        }}

        .overlay-toggle input {{
            display: none;
        }}

        .toggle-slider {{
            width: 36px;
            height: 20px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            position: relative;
            transition: all 0.2s ease;
        }}

        .toggle-slider::after {{
            content: '';
            position: absolute;
            width: 14px;
            height: 14px;
            background: var(--text-muted);
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: all 0.2s ease;
        }}

        .overlay-toggle input:checked + .toggle-slider {{
            background: var(--accent-dim);
            border-color: var(--accent);
        }}

        .overlay-toggle input:checked + .toggle-slider::after {{
            background: var(--accent);
            left: 18px;
        }}

        .overlay-label {{
            color: var(--text-secondary);
            font-size: 0.8rem;
            font-weight: 500;
        }}

        .overlay-toggle input:checked ~ .overlay-label {{
            color: var(--text-primary);
        }}

        .details-content {{
            padding: 14px 18px;
            font-size: 0.82rem;
            overflow-y: auto;
            max-height: calc(30vh - 50px);
        }}

        .transcript-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
        }}

        .transcript-panel h2 {{
            padding: 14px 18px;
            font-size: 0.9rem;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
        }}

        .transcript-content {{
            padding: 14px 18px;
            font-size: 0.85rem;
            line-height: 1.9;
            color: var(--text-secondary);
            max-height: 150px;
            overflow-y: auto;
        }}

        .transcript-segment {{
            display: inline;
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
            transition: all 0.15s ease;
        }}

        .transcript-segment:hover {{
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }}

        .transcript-segment.active {{
            background: var(--accent-dim);
            color: var(--accent);
        }}

        .transcript-time {{
            color: var(--text-muted);
            font-size: 0.7rem;
            font-family: "SF Mono", Monaco, monospace;
            margin-right: 4px;
        }}

        .detail-row {{
            display: flex;
            margin-bottom: 6px;
        }}

        .detail-key {{
            color: var(--text-muted);
            min-width: 80px;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }}

        .detail-value {{
            font-family: "SF Mono", Monaco, monospace;
            color: var(--text-secondary);
        }}

        .keyboard-hint {{
            text-align: center;
            padding: 16px;
            color: var(--text-muted);
            font-size: 0.75rem;
            letter-spacing: 0.02em;
        }}

        @media (max-width: 900px) {{
            .main-content {{
                grid-template-columns: 1fr;
            }}

            .sidebar {{
                flex-direction: row;
            }}

            .events-panel, .details-panel {{
                flex: 1;
                max-height: 300px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Capture Viewer</h1>
            <div class="meta">ID: {capture_id} | Duration: {duration_str}</div>
        </header>

        <div class="main-content">
            <div class="viewer-section">
                <div class="frame-container" id="frame-container">
                    <img id="frame-image" src="" alt="Frame">
                    <canvas id="overlay-canvas"></canvas>
                    <div class="frame-overlay" id="frame-time">0:00.00</div>
                </div>

                <div class="controls">
                    <div class="playback-controls">
                        <button class="nav" id="btn-first" title="First (Home)">⏮</button>
                        <button class="nav" id="btn-prev" title="Previous (←)">◀</button>
                        <button id="btn-play" title="Play/Pause (Space)">▶</button>
                        <button class="nav" id="btn-next" title="Next (→)">▶</button>
                        <button class="nav" id="btn-last" title="Last (End)">⏭</button>
                        <div class="time-display">
                            <span id="current-time">0:00.00</span> / {duration_str}
                        </div>
                        <label class="overlay-toggle" title="Toggle overlay (O)">
                            <input type="checkbox" id="btn-overlay" checked>
                            <span class="toggle-slider"></span>
                            <span class="overlay-label">Overlay</span>
                        </label>
                    </div>

                    <div class="timeline" id="timeline">
                        <div class="timeline-progress" id="timeline-progress"></div>
                        <div class="timeline-markers" id="timeline-markers"></div>
                    </div>

                    {audio_controls_html}
                </div>
            </div>

            <div class="sidebar">
                <div class="events-panel">
                    <h2><span>Events ({len(events_data)})</span><button class="copy-btn" id="copy-all-btn" title="Copy all events">Copy All</button></h2>
                    <div class="events-list" id="events-list"></div>
                </div>

                <div class="details-panel">
                    <h2><span>Event Details</span><button class="copy-btn" id="copy-btn" title="Copy to clipboard">Copy</button></h2>
                    <div class="details-content" id="details-content">
                        <em style="color: #666;">Select an event to view details</em>
                    </div>
                </div>

                {transcript_panel_html}
            </div>
        </div>

        <div class="keyboard-hint">
            Keyboard: Space (play/pause) | ← → (prev/next) | Home/End (first/last) | O (toggle overlay)
        </div>
    </div>

    {"" if not audio_b64 else f'<audio id="audio" src="data:{audio_type};base64,{audio_b64}"></audio>'}

    <script>
        // Data
        const frames = {frames_json};
        const events = {events_json};
        const duration = {duration};
        const hasAudio = {"true" if audio_b64 else "false"};
        const screenWidth = {screen_width};
        const screenHeight = {screen_height};
        const pixelRatio = {pixel_ratio};
        const transcriptData = {transcript if transcript else '{"text": "", "segments": []}'};


        // State
        let currentIndex = 0;
        let isPlaying = false;
        let playInterval = null;
        let showOverlay = true;

        // Elements
        const frameContainer = document.getElementById('frame-container');
        const frameImage = document.getElementById('frame-image');
        const overlayCanvas = document.getElementById('overlay-canvas');
        const overlayCtx = overlayCanvas.getContext('2d');
        const frameTime = document.getElementById('frame-time');
        const currentTimeEl = document.getElementById('current-time');
        const timelineProgress = document.getElementById('timeline-progress');
        const timelineMarkers = document.getElementById('timeline-markers');
        const eventsList = document.getElementById('events-list');
        const detailsContent = document.getElementById('details-content');
        const btnPlay = document.getElementById('btn-play');
        const btnOverlay = document.getElementById('btn-overlay');
        const btnCopy = document.getElementById('copy-btn');
        const btnCopyAll = document.getElementById('copy-all-btn');
        const audio = document.getElementById('audio');
        const transcriptContent = document.getElementById('transcript-content');

        // Current event for copying
        let currentEvent = null;

        // Initialize
        function init() {{
            // Render event markers on timeline
            events.forEach((event, i) => {{
                const marker = document.createElement('div');
                marker.className = 'timeline-marker';
                marker.style.left = (event.time / duration * 100) + '%';
                timelineMarkers.appendChild(marker);
            }});

            // Render events list
            events.forEach((event, i) => {{
                const item = document.createElement('div');
                item.className = 'event-item';
                item.dataset.index = i;

                const typeClass = getTypeClass(event.type);
                const timeStr = formatTime(event.time);

                item.innerHTML = `
                    <span class="event-time">${{timeStr}}</span>
                    <span class="event-type ${{typeClass}}">${{event.type}}</span>
                `;

                item.addEventListener('click', () => goToIndex(i));
                eventsList.appendChild(item);
            }});

            // Render transcript segments if available
            if (transcriptContent && transcriptData.segments && transcriptData.segments.length > 0) {{
                transcriptData.segments.forEach((segment, i) => {{
                    const span = document.createElement('span');
                    span.className = 'transcript-segment';
                    span.dataset.index = i;
                    span.dataset.start = segment.start;
                    span.dataset.end = segment.end;

                    const timeSpan = document.createElement('span');
                    timeSpan.className = 'transcript-time';
                    timeSpan.textContent = formatTime(segment.start);
                    span.appendChild(timeSpan);

                    const textNode = document.createTextNode(segment.text + ' ');
                    span.appendChild(textNode);

                    span.addEventListener('click', () => seekToTranscript(segment.start));
                    transcriptContent.appendChild(span);
                }});
            }} else if (transcriptContent && transcriptData.text) {{
                // Fallback: show plain text if no segments
                transcriptContent.textContent = transcriptData.text;
            }}

            // Show first frame
            updateDisplay();

            // Audio setup
            if (hasAudio && audio) {{
                const volumeSlider = document.getElementById('volume');
                const muteCheckbox = document.getElementById('mute');

                if (volumeSlider) {{
                    volumeSlider.addEventListener('input', () => {{
                        audio.volume = volumeSlider.value;
                    }});
                    audio.volume = volumeSlider.value;
                }}

                if (muteCheckbox) {{
                    muteCheckbox.addEventListener('change', () => {{
                        audio.muted = muteCheckbox.checked;
                    }});
                }}
            }}
        }}

        function getTypeClass(type) {{
            if (type.includes('click')) return 'click';
            if (type.includes('drag')) return 'drag';
            if (type.includes('scroll')) return 'scroll';
            if (type.includes('type')) return 'type';
            return '';
        }}

        function formatTime(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = (seconds % 60).toFixed(2).padStart(5, '0');
            return `${{mins}}:${{secs}}`;
        }}

        function drawOverlay(event) {{
            // Get displayed image dimensions and position
            const imgRect = frameImage.getBoundingClientRect();
            const containerRect = frameContainer.getBoundingClientRect();

            // Use actual image dimensions (naturalWidth/Height) for aspect ratio
            // This handles Retina displays where image pixels != screen coordinates
            const imgNaturalWidth = frameImage.naturalWidth || screenWidth;
            const imgNaturalHeight = frameImage.naturalHeight || screenHeight;

            // Scale ratio: image pixels / screen coordinates (e.g., 2.0 for Retina)
            const pixelRatioX = imgNaturalWidth / screenWidth;
            const pixelRatioY = imgNaturalHeight / screenHeight;

            // Calculate actual displayed size (accounting for object-fit: contain)
            const imgAspect = imgNaturalWidth / imgNaturalHeight;
            const containerAspect = imgRect.width / imgRect.height;

            let displayWidth, displayHeight, offsetX, offsetY;
            if (imgAspect > containerAspect) {{
                displayWidth = imgRect.width;
                displayHeight = imgRect.width / imgAspect;
                offsetX = 0;
                offsetY = (imgRect.height - displayHeight) / 2;
            }} else {{
                displayHeight = imgRect.height;
                displayWidth = imgRect.height * imgAspect;
                offsetX = (imgRect.width - displayWidth) / 2;
                offsetY = 0;
            }}

            // Set canvas size to match container
            overlayCanvas.width = imgRect.width;
            overlayCanvas.height = imgRect.height;
            overlayCanvas.style.width = imgRect.width + 'px';
            overlayCanvas.style.height = imgRect.height + 'px';

            // Position canvas over image
            const imgOffsetX = imgRect.left - containerRect.left;
            const imgOffsetY = imgRect.top - containerRect.top;
            overlayCanvas.style.left = imgOffsetX + 'px';
            overlayCanvas.style.top = imgOffsetY + 'px';

            // Clear canvas
            overlayCtx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

            if (!showOverlay) return;

            // Scale factors: convert mouse coordinates to display pixels
            // Mouse coords from pynput are in LOGICAL space (e.g., 1512x982 on Retina)
            // screenWidth/Height we stored are in PHYSICAL space (e.g., 3024x1964)
            // pixelRatio = physical/logical (e.g., 2.0 for Retina)
            //
            // To map mouse coord to display:
            // 1. mouseCoord * pixelRatio = physical image coord
            // 2. physical image coord * (displayWidth / imgNaturalWidth) = display coord
            const scaleX = (displayWidth / imgNaturalWidth) * pixelRatio;
            const scaleY = (displayHeight / imgNaturalHeight) * pixelRatio;

            // Draw based on event type
            const type = event.type;

            if (type.includes('click') || type === 'mouse.down' || type === 'mouse.up') {{
                const x = offsetX + (event.x * scaleX);
                const y = offsetY + (event.y * scaleY);
                const radius = 20;

                // Outer glow
                overlayCtx.beginPath();
                overlayCtx.arc(x, y, radius + 10, 0, Math.PI * 2);
                overlayCtx.fillStyle = 'rgba(255, 100, 100, 0.3)';
                overlayCtx.fill();

                // Main circle
                overlayCtx.beginPath();
                overlayCtx.arc(x, y, radius, 0, Math.PI * 2);
                overlayCtx.strokeStyle = '#ff5f5f';
                overlayCtx.lineWidth = 3;
                overlayCtx.stroke();

                // Center dot
                overlayCtx.beginPath();
                overlayCtx.arc(x, y, 4, 0, Math.PI * 2);
                overlayCtx.fillStyle = '#ff5f5f';
                overlayCtx.fill();

                // Crosshair
                overlayCtx.beginPath();
                overlayCtx.moveTo(x - radius - 5, y);
                overlayCtx.lineTo(x - radius + 10, y);
                overlayCtx.moveTo(x + radius - 10, y);
                overlayCtx.lineTo(x + radius + 5, y);
                overlayCtx.moveTo(x, y - radius - 5);
                overlayCtx.lineTo(x, y - radius + 10);
                overlayCtx.moveTo(x, y + radius - 10);
                overlayCtx.lineTo(x, y + radius + 5);
                overlayCtx.strokeStyle = '#ff5f5f';
                overlayCtx.lineWidth = 2;
                overlayCtx.stroke();

            }} else if (type.includes('drag')) {{
                const startX = offsetX + (event.x * scaleX);
                const startY = offsetY + (event.y * scaleY);
                // End position = start + delta
                const endX = offsetX + ((event.x + event.dx) * scaleX);
                const endY = offsetY + ((event.y + event.dy) * scaleY);

                // Draw arrow from start to end
                overlayCtx.beginPath();
                overlayCtx.moveTo(startX, startY);
                overlayCtx.lineTo(endX, endY);
                overlayCtx.strokeStyle = '#00d4aa';
                overlayCtx.lineWidth = 3;
                overlayCtx.stroke();

                // Start circle
                overlayCtx.beginPath();
                overlayCtx.arc(startX, startY, 8, 0, Math.PI * 2);
                overlayCtx.fillStyle = '#00d4aa';
                overlayCtx.fill();

                // End arrowhead
                const angle = Math.atan2(endY - startY, endX - startX);
                overlayCtx.beginPath();
                overlayCtx.moveTo(endX, endY);
                overlayCtx.lineTo(endX - 15 * Math.cos(angle - 0.4), endY - 15 * Math.sin(angle - 0.4));
                overlayCtx.lineTo(endX - 15 * Math.cos(angle + 0.4), endY - 15 * Math.sin(angle + 0.4));
                overlayCtx.closePath();
                overlayCtx.fillStyle = '#00d4aa';
                overlayCtx.fill();

            }} else if (type.includes('scroll')) {{
                const x = offsetX + (event.x * scaleX);
                const y = offsetY + (event.y * scaleY);

                // Scroll indicator
                overlayCtx.beginPath();
                overlayCtx.arc(x, y, 15, 0, Math.PI * 2);
                overlayCtx.strokeStyle = '#a78bfa';
                overlayCtx.lineWidth = 2;
                overlayCtx.stroke();

                // Arrows indicating scroll direction
                const dy = event.dy || 0;
                if (dy !== 0) {{
                    const arrowY = dy > 0 ? -25 : 25;
                    overlayCtx.beginPath();
                    overlayCtx.moveTo(x, y + arrowY);
                    overlayCtx.lineTo(x - 8, y + arrowY + (dy > 0 ? 10 : -10));
                    overlayCtx.lineTo(x + 8, y + arrowY + (dy > 0 ? 10 : -10));
                    overlayCtx.closePath();
                    overlayCtx.fillStyle = '#a78bfa';
                    overlayCtx.fill();
                }}

            }} else if (type.includes('type')) {{
                // Text bubble for keyboard input
                const text = event.text || event.keys || '';
                if (text) {{
                    const bubbleX = 20;
                    const bubbleY = overlayCanvas.height - 60;
                    const padding = 10;

                    overlayCtx.font = '16px monospace';
                    const metrics = overlayCtx.measureText(text);
                    const textWidth = Math.min(metrics.width, 300);

                    // Background
                    overlayCtx.fillStyle = 'rgba(52, 211, 153, 0.9)';
                    overlayCtx.beginPath();
                    overlayCtx.roundRect(bubbleX, bubbleY, textWidth + padding * 2, 36, 8);
                    overlayCtx.fill();

                    // Text
                    overlayCtx.fillStyle = '#fff';
                    overlayCtx.fillText(text.substring(0, 30), bubbleX + padding, bubbleY + 24);
                }}
            }}

            // Draw label
            let label = event.type.split('.').pop();
            if (event.text) label += ': ' + event.text.substring(0, 20);
            else if (event.keys) label += ': ' + event.keys;

            overlayCtx.font = 'bold 14px sans-serif';
            const labelMetrics = overlayCtx.measureText(label);
            const labelX = 10;
            const labelY = 30;

            // Label background
            overlayCtx.fillStyle = 'rgba(0, 0, 0, 0.7)';
            overlayCtx.beginPath();
            overlayCtx.roundRect(labelX - 5, labelY - 18, labelMetrics.width + 10, 24, 4);
            overlayCtx.fill();

            // Label text
            overlayCtx.fillStyle = '#fff';
            overlayCtx.fillText(label, labelX, labelY);
        }}

        function updateDisplay(skipAudioSync = false) {{
            const frame = frames[currentIndex];
            const event = events[currentIndex];

            // Update frame
            if (frame && frame.image) {{
                frameImage.src = 'data:image/jpeg;base64,' + frame.image;
                // Draw overlay after image loads
                frameImage.onload = () => drawOverlay(event);
            }}

            // Draw overlay immediately if image already loaded
            if (frameImage.complete) {{
                setTimeout(() => drawOverlay(event), 0);
            }}

            // Update time displays
            const timeStr = formatTime(event.time);
            frameTime.textContent = timeStr;
            if (!isPlaying) {{
                currentTimeEl.textContent = timeStr;
            }}

            // Update timeline (only if not playing - playback updates this directly)
            if (!isPlaying) {{
                const progress = (event.time / duration) * 100;
                timelineProgress.style.width = progress + '%';
            }}

            // Update events list
            document.querySelectorAll('.event-item').forEach((item, i) => {{
                item.classList.toggle('active', i === currentIndex);
            }});

            // Scroll active event into view
            const activeItem = eventsList.querySelector('.event-item.active');
            if (activeItem) {{
                activeItem.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
            }}

            // Update details
            updateDetails(event);

            // Sync audio only on manual navigation, not during playback
            if (hasAudio && audio && !skipAudioSync && !isPlaying) {{
                audio.currentTime = event.time;
            }}
        }}

        function updateDetails(event) {{
            currentEvent = event;
            let html = '';
            for (const [key, value] of Object.entries(event)) {{
                if (key === 'index') continue;
                const displayValue = key === 'time' ? formatTime(value) : value;
                html += `<div class="detail-row">
                    <span class="detail-key">${{key}}:</span>
                    <span class="detail-value">${{displayValue ?? '-'}}</span>
                </div>`;
            }}
            detailsContent.innerHTML = html;
        }}

        function formatEventForCopy(event) {{
            const lines = [];
            for (const [key, value] of Object.entries(event)) {{
                if (key === 'index') continue;
                const displayValue = key === 'time' ? formatTime(value) : value;
                lines.push(`${{key}}: ${{displayValue ?? '-'}}`);
            }}
            return lines.join('\\n');
        }}

        function copyEventDetails() {{
            if (!currentEvent) return;

            const text = formatEventForCopy(currentEvent);

            navigator.clipboard.writeText(text).then(() => {{
                btnCopy.textContent = 'Copied!';
                btnCopy.classList.add('copied');
                setTimeout(() => {{
                    btnCopy.textContent = 'Copy';
                    btnCopy.classList.remove('copied');
                }}, 1500);
            }});
        }}

        function copyAllEvents() {{
            const allText = events.map((event, i) => {{
                return `--- Event ${{i + 1}} ---\\n${{formatEventForCopy(event)}}`;
            }}).join('\\n\\n');

            navigator.clipboard.writeText(allText).then(() => {{
                btnCopyAll.textContent = 'Copied!';
                btnCopyAll.classList.add('copied');
                setTimeout(() => {{
                    btnCopyAll.textContent = 'Copy All';
                    btnCopyAll.classList.remove('copied');
                }}, 1500);
            }});
        }}

        function goToIndex(index) {{
            currentIndex = Math.max(0, Math.min(frames.length - 1, index));
            // If playing, pause first so we can seek properly
            if (isPlaying) {{
                togglePlay();
            }}
            updateDisplay();
            // Force audio sync for navigation
            if (hasAudio && audio) {{
                const targetTime = events[currentIndex].time;
                audio.currentTime = targetTime;
                updateActiveTranscript(targetTime);
            }}
        }}

        function seekToTranscript(time) {{
            // Seek audio to transcript segment time
            if (hasAudio && audio) {{
                audio.currentTime = time;
                // Find the closest event to this time
                let closest = 0;
                let minDiff = Infinity;
                events.forEach((event, i) => {{
                    const diff = Math.abs(event.time - time);
                    if (diff < minDiff) {{
                        minDiff = diff;
                        closest = i;
                    }}
                }});
                currentIndex = closest;
                updateDisplay(true);  // skip audio sync since we just set it
                updateActiveTranscript(time);
            }}
        }}

        function updateActiveTranscript(currentTime) {{
            if (!transcriptContent) return;
            const segments = transcriptContent.querySelectorAll('.transcript-segment');
            segments.forEach(segment => {{
                const start = parseFloat(segment.dataset.start);
                const end = parseFloat(segment.dataset.end);
                const isActive = currentTime >= start && currentTime < end;
                segment.classList.toggle('active', isActive);
                if (isActive) {{
                    segment.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
                }}
            }});
        }}

        function next() {{
            if (currentIndex < frames.length - 1) {{
                currentIndex++;
                updateDisplay();
            }} else if (isPlaying) {{
                togglePlay();
            }}
        }}

        function prev() {{
            if (currentIndex > 0) {{
                currentIndex--;
                updateDisplay();
            }}
        }}

        function togglePlay() {{
            isPlaying = !isPlaying;
            btnPlay.textContent = isPlaying ? '⏸' : '▶';

            if (isPlaying) {{
                // Start audio from current event time
                if (hasAudio && audio) {{
                    audio.currentTime = events[currentIndex].time;
                    audio.play();
                }}
                // Use audio timeupdate to drive event updates, or fallback timer
                if (hasAudio && audio) {{
                    audio.ontimeupdate = () => {{
                        if (!isPlaying) return;
                        const currentTime = audio.currentTime;
                        // Find the event that matches current audio time
                        let newIndex = currentIndex;
                        for (let i = 0; i < events.length; i++) {{
                            if (events[i].time <= currentTime) {{
                                newIndex = i;
                            }} else {{
                                break;
                            }}
                        }}
                        if (newIndex !== currentIndex) {{
                            currentIndex = newIndex;
                            updateDisplay(true);  // skip audio sync
                        }}
                        // Update timeline progress
                        const percent = (currentTime / duration) * 100;
                        timelineProgress.style.width = percent + '%';
                        currentTimeEl.textContent = formatTime(currentTime);
                        // Update active transcript segment
                        updateActiveTranscript(currentTime);
                    }};
                    audio.onended = () => {{
                        isPlaying = false;
                        btnPlay.textContent = '▶';
                    }};
                }} else {{
                    // No audio: use timer-based playback at real speed
                    const startTime = events[currentIndex].time;
                    const startRealTime = Date.now();
                    playInterval = setInterval(() => {{
                        const elapsed = (Date.now() - startRealTime) / 1000;
                        const currentTime = startTime + elapsed;
                        // Find matching event
                        let newIndex = currentIndex;
                        for (let i = currentIndex; i < events.length; i++) {{
                            if (events[i].time <= currentTime) {{
                                newIndex = i;
                            }} else {{
                                break;
                            }}
                        }}
                        if (newIndex !== currentIndex) {{
                            currentIndex = newIndex;
                            updateDisplay();
                        }}
                        // Update timeline
                        const percent = (currentTime / duration) * 100;
                        timelineProgress.style.width = percent + '%';
                        currentTimeEl.textContent = formatTime(currentTime);
                        // Stop at end
                        if (currentTime >= duration) {{
                            togglePlay();
                        }}
                    }}, 50);
                }}
            }} else {{
                if (hasAudio && audio) {{
                    audio.pause();
                    audio.ontimeupdate = null;
                    audio.onended = null;
                }}
                clearInterval(playInterval);
            }}
        }}

        function toggleOverlay() {{
            showOverlay = !showOverlay;
            btnOverlay.checked = showOverlay;
            drawOverlay(events[currentIndex]);
        }}

        // Event listeners
        document.getElementById('btn-play').addEventListener('click', togglePlay);
        document.getElementById('btn-next').addEventListener('click', next);
        document.getElementById('btn-prev').addEventListener('click', prev);
        document.getElementById('btn-first').addEventListener('click', () => goToIndex(0));
        document.getElementById('btn-last').addEventListener('click', () => goToIndex(frames.length - 1));
        btnCopy.addEventListener('click', copyEventDetails);
        btnCopyAll.addEventListener('click', copyAllEvents);
        btnOverlay.addEventListener('change', () => {{
            showOverlay = btnOverlay.checked;
            drawOverlay(events[currentIndex]);
        }});

        document.getElementById('timeline').addEventListener('click', (e) => {{
            const rect = e.target.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const percent = x / rect.width;
            const targetTime = percent * duration;

            // Find closest frame
            let closest = 0;
            let minDiff = Infinity;
            frames.forEach((frame, i) => {{
                const diff = Math.abs(frame.time - targetTime);
                if (diff < minDiff) {{
                    minDiff = diff;
                    closest = i;
                }}
            }});

            goToIndex(closest);
        }});

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {{
            if (e.target.tagName === 'INPUT') return;

            switch (e.code) {{
                case 'Space':
                    e.preventDefault();
                    togglePlay();
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    next();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    prev();
                    break;
                case 'Home':
                    e.preventDefault();
                    goToIndex(0);
                    break;
                case 'End':
                    e.preventDefault();
                    goToIndex(frames.length - 1);
                    break;
                case 'KeyO':
                    e.preventDefault();
                    toggleOverlay();
                    break;
            }}
        }});

        // Initialize
        init();
    </script>
</body>
</html>
'''

    return html
