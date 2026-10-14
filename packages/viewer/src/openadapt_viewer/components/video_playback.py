"""Video playback component from captured screenshots.

This component provides:
- Canvas-based video rendering from screenshot sequences
- Play/pause/seek functionality
- Frame-by-frame navigation
- Variable playback speeds
- Optional action overlays on frames
- Support for both embedded and file-based screenshots
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import TypedDict


class ScreenshotFrame(TypedDict, total=False):
    """Frame definition for video playback."""

    path: str  # Path to screenshot file
    data: str  # Base64-encoded image data (alternative to path)
    timestamp: float  # Timestamp in seconds
    action: dict  # Associated action (optional)
    overlays: list[dict]  # Click/highlight overlays


def video_playback(
    frames: list[ScreenshotFrame] | None = None,
    width: int = 960,
    height: int = 540,
    autoplay: bool = False,
    loop: bool = False,
    show_controls: bool = True,
    show_timeline: bool = True,
    show_frame_counter: bool = True,
    default_fps: float = 2.0,
    speeds: list[float] | None = None,
    embed_images: bool = False,
    alpine_data_name: str = "videoPlayer",
    class_name: str = "",
) -> str:
    """Render a video playback component from screenshot frames.

    Args:
        frames: List of screenshot frames with paths and optional timestamps
        width: Video display width in pixels
        height: Video display height in pixels
        autoplay: Start playing automatically
        loop: Loop playback at end
        show_controls: Show play/pause/speed controls
        show_timeline: Show clickable timeline
        show_frame_counter: Show frame counter
        default_fps: Default frames per second
        speeds: Available playback speeds (default: [0.5, 1, 2, 4, 8])
        embed_images: If True, embed images as base64
        alpine_data_name: Alpine.js x-data variable name
        class_name: Additional CSS classes

    Returns:
        HTML string for the video playback component
    """
    frames = frames or []
    speeds = speeds or [0.25, 0.5, 1, 2, 4, 8]
    extra_class = f" {class_name}" if class_name else ""

    # Process frames - embed images if requested
    processed_frames = []
    for frame in frames:
        frame_data = dict(frame)
        if embed_images and "path" in frame_data and frame_data.get("path"):
            path = Path(frame_data["path"])
            if path.exists():
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                suffix = path.suffix.lower()
                mime = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                }.get(suffix, "image/png")
                frame_data["data"] = f"data:{mime};base64,{data}"
                frame_data.pop("path", None)
        processed_frames.append(frame_data)

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    frames_json = html.escape(json.dumps(processed_frames))
    html.escape(json.dumps(speeds))

    # Speed options for dropdown
    speed_options = "\n".join(
        f'<option value="{s}" {"selected" if s == 1 else ""}>{s}x</option>'
        for s in speeds
    )

    return f'''<div class="oa-video-player{extra_class}"
     x-data="{{
         frames: {frames_json},
         currentFrame: 0,
         isPlaying: {'true' if autoplay else 'false'},
         loop: {'true' if loop else 'false'},
         fps: {default_fps},
         playbackSpeed: 1,
         playbackInterval: null,
         preloadedImages: [],
         isLoaded: false,

         get totalFrames() {{ return this.frames.length; }},
         get currentFrameData() {{ return this.frames[this.currentFrame] || {{}}; }},
         get currentImageSrc() {{
             const frame = this.currentFrameData;
             return frame.data || frame.path || '';
         }},
         get currentTimestamp() {{
             const frame = this.currentFrameData;
             return frame.timestamp !== undefined ? frame.timestamp : this.currentFrame / this.fps;
         }},
         get duration() {{
             if (this.frames.length === 0) return 0;
             const lastFrame = this.frames[this.frames.length - 1];
             return lastFrame.timestamp !== undefined ? lastFrame.timestamp : (this.frames.length - 1) / this.fps;
         }},
         get progress() {{
             return this.totalFrames > 1 ? (this.currentFrame / (this.totalFrames - 1)) * 100 : 0;
         }},

         init() {{
             this.preloadImages();
             this.$watch('playbackSpeed', () => {{
                 if (this.isPlaying) {{
                     this.stopPlayback();
                     this.startPlayback();
                 }}
             }});
         }},

         preloadImages() {{
             this.frames.forEach((frame, index) => {{
                 const img = new Image();
                 img.onload = () => {{
                     this.preloadedImages[index] = img;
                     if (index === this.frames.length - 1) {{
                         this.isLoaded = true;
                     }}
                 }};
                 img.src = frame.data || frame.path || '';
             }});
             if (this.frames.length === 0) {{
                 this.isLoaded = true;
             }}
         }},

         goToFrame(index) {{
             this.currentFrame = Math.max(0, Math.min(index, this.totalFrames - 1));
         }},
         prevFrame() {{
             if (this.currentFrame > 0) this.currentFrame--;
         }},
         nextFrame() {{
             if (this.currentFrame < this.totalFrames - 1) this.currentFrame++;
         }},
         goToStart() {{
             this.currentFrame = 0;
         }},
         goToEnd() {{
             this.currentFrame = Math.max(0, this.totalFrames - 1);
         }},

         togglePlayback() {{
             if (this.isPlaying) {{
                 this.stopPlayback();
             }} else {{
                 this.startPlayback();
             }}
         }},
         startPlayback() {{
             if (this.totalFrames <= 1) return;
             if (this.currentFrame >= this.totalFrames - 1) {{
                 this.currentFrame = 0;
             }}
             this.isPlaying = true;
             const interval = 1000 / (this.fps * this.playbackSpeed);
             this.playbackInterval = setInterval(() => {{
                 if (this.currentFrame < this.totalFrames - 1) {{
                     this.currentFrame++;
                 }} else if (this.loop) {{
                     this.currentFrame = 0;
                 }} else {{
                     this.stopPlayback();
                 }}
             }}, interval);
         }},
         stopPlayback() {{
             this.isPlaying = false;
             if (this.playbackInterval) {{
                 clearInterval(this.playbackInterval);
                 this.playbackInterval = null;
             }}
         }},

         seekToPercent(percent) {{
             const frameIndex = Math.floor(percent * (this.totalFrames - 1));
             this.goToFrame(frameIndex);
         }},
         handleTimelineClick(e) {{
             const rect = e.target.getBoundingClientRect();
             const clickX = e.clientX - rect.left;
             const percent = clickX / rect.width;
             this.seekToPercent(percent);
         }},

         formatTime(seconds) {{
             const mins = Math.floor(seconds / 60);
             const secs = Math.floor(seconds % 60);
             return mins.toString().padStart(2, '0') + ':' + secs.toString().padStart(2, '0');
         }}
     }}"
     @keydown.space.window.prevent="togglePlayback()"
     @keydown.left.window="prevFrame()"
     @keydown.right.window="nextFrame()"
     @keydown.home.window="goToStart()"
     @keydown.end.window="goToEnd()"
     style="width: {width}px;">

    <!-- Video Display Area -->
    <div class="oa-video-display" style="position: relative; width: 100%; height: {height}px; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius-lg); overflow: hidden;">
        <!-- Loading State -->
        <div x-show="!isLoaded" style="position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;">
            <div style="color: var(--oa-text-muted);">Loading frames...</div>
        </div>

        <!-- Frame Display -->
        <img x-show="isLoaded && currentImageSrc"
             :src="currentImageSrc"
             style="width: 100%; height: 100%; object-fit: contain;"
             alt="Frame">

        <!-- No Frames State -->
        <div x-show="isLoaded && totalFrames === 0"
             style="position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--oa-text-muted);">
            No frames available
        </div>

        <!-- Action Overlay -->
        <template x-if="currentFrameData.action && currentFrameData.action.type === 'click' && currentFrameData.action.x !== undefined">
            <div style="position: absolute; transform: translate(-50%, -50%); width: 28px; height: 28px; border-radius: 50%; border: 3px solid var(--oa-accent); background: var(--oa-accent-dim); display: flex; align-items: center; justify-content: center; pointer-events: none;"
                 :style="'left: ' + (currentFrameData.action.x * 100) + '%; top: ' + (currentFrameData.action.y * 100) + '%'">
                <span style="font-size: 11px; font-weight: bold; color: var(--oa-accent);" x-text="currentFrame + 1"></span>
            </div>
        </template>

        <!-- Custom Overlays -->
        <template x-for="(overlay, idx) in (currentFrameData.overlays || [])" :key="idx">
            <div :class="'oa-overlay oa-overlay-' + (overlay.type || 'click') + ' oa-overlay-' + (overlay.variant || '')"
                 :style="'left: ' + (overlay.x * 100) + '%; top: ' + (overlay.y * 100) + '%;'">
                <span x-show="overlay.label" class="oa-overlay-label" x-text="overlay.label"></span>
            </div>
        </template>
    </div>

    <!-- Controls -->
    <div x-show="{str(show_controls).lower()}" class="oa-video-controls" style="display: flex; align-items: center; gap: var(--oa-space-sm); padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius); margin-top: var(--oa-space-sm);">
        <!-- Rewind -->
        <button class="oa-playback-btn" @click="goToStart()" :disabled="currentFrame === 0" title="Go to start (Home)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/>
            </svg>
        </button>

        <!-- Previous Frame -->
        <button class="oa-playback-btn" @click="prevFrame()" :disabled="currentFrame === 0" title="Previous frame (Left arrow)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
            </svg>
        </button>

        <!-- Play/Pause -->
        <button class="oa-playback-btn oa-playback-btn-primary" @click="togglePlayback()" :disabled="totalFrames <= 1" title="Play/Pause (Space)">
            <svg x-show="!isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5v14l11-7z"/>
            </svg>
            <svg x-show="isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
            </svg>
        </button>

        <!-- Next Frame -->
        <button class="oa-playback-btn" @click="nextFrame()" :disabled="currentFrame >= totalFrames - 1" title="Next frame (Right arrow)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
            </svg>
        </button>

        <!-- End -->
        <button class="oa-playback-btn" @click="goToEnd()" :disabled="currentFrame >= totalFrames - 1" title="Go to end (End)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 18l8.5-6L6 6v12zm2-12v12h2V6h-2z" transform="scale(-1, 1) translate(-24, 0)"/>
            </svg>
        </button>

        <!-- Time Display -->
        <span style="flex: 1; text-align: center; font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary); font-family: var(--oa-font-mono);"
              x-text="formatTime(currentTimestamp) + ' / ' + formatTime(duration)">
        </span>

        <!-- Frame Counter -->
        <span x-show="{str(show_frame_counter).lower()}"
              style="font-size: var(--oa-font-size-sm); color: var(--oa-text-muted);"
              x-text="'Frame ' + (currentFrame + 1) + '/' + totalFrames">
        </span>

        <!-- Speed Selector -->
        <select class="oa-playback-speed"
                x-model.number="playbackSpeed"
                title="Playback speed">
            {speed_options}
        </select>

        <!-- Loop Toggle -->
        <button class="oa-playback-btn" @click="loop = !loop" :class="{{'oa-playback-btn-active': loop}}" title="Loop playback">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" :style="loop ? 'color: var(--oa-accent)' : ''">
                <path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0020 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 004 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/>
            </svg>
        </button>
    </div>

    <!-- Timeline -->
    <div x-show="{str(show_timeline).lower()}" class="oa-video-timeline" style="margin-top: var(--oa-space-sm);">
        <div class="oa-timeline-track"
             @click="handleTimelineClick($event)"
             style="height: 12px; cursor: pointer; position: relative;">
            <!-- Progress -->
            <div class="oa-timeline-progress"
                 :style="'width: ' + progress + '%'"></div>
            <!-- Frame markers (for small number of frames) -->
            <template x-if="totalFrames <= 30">
                <template x-for="(frame, idx) in frames" :key="idx">
                    <div @click.stop="goToFrame(idx)"
                         :class="{{'oa-frame-marker-active': idx === currentFrame}}"
                         style="position: absolute; top: 0; bottom: 0; width: 3px; background: var(--oa-text-muted); opacity: 0.3; cursor: pointer; transition: all 0.15s;"
                         :style="'left: calc(' + (idx / (totalFrames - 1) * 100) + '% - 1.5px);' + (idx === currentFrame ? 'opacity: 1; background: var(--oa-accent);' : '')">
                    </div>
                </template>
            </template>
        </div>
        <!-- Timestamps -->
        <div style="display: flex; justify-content: space-between; margin-top: var(--oa-space-xs); font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">
            <span>0:00</span>
            <span x-text="formatTime(duration)"></span>
        </div>
    </div>
</div>'''


def video_playback_with_actions(
    frames: list[ScreenshotFrame] | None = None,
    width: int = 960,
    height: int = 540,
    show_action_details: bool = True,
    **kwargs,
) -> str:
    """Video playback with integrated action details panel.

    Args:
        frames: List of screenshot frames
        width: Video display width
        height: Video display height
        show_action_details: Whether to show action details below video
        **kwargs: Additional arguments passed to video_playback

    Returns:
        HTML string for video playback with action details
    """
    frames = frames or []
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    frames_json = html.escape(json.dumps(frames))

    video_html = video_playback(
        frames=frames,
        width=width,
        height=height,
        **kwargs,
    )

    if not show_action_details:
        return video_html

    # Wrap video with action details panel
    return f'''<div class="oa-video-with-actions" x-data="{{
        frames: {frames_json},
        get currentAction() {{
            const player = document.querySelector('[x-data*=\\"videoPlayer\\"]');
            if (!player || !player.__x) return null;
            const data = player.__x.$data;
            return data.currentFrameData?.action || null;
        }}
    }}">
    {video_html}

    <!-- Action Details Panel -->
    <div class="oa-action-details-panel" style="margin-top: var(--oa-space-md); padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg);">
        <h4 style="margin: 0 0 var(--oa-space-sm) 0; font-size: var(--oa-font-size-sm); font-weight: 600; color: var(--oa-text-secondary);">Action Details</h4>
        <div x-show="currentAction" class="oa-action-info">
            <div style="display: flex; align-items: center; gap: var(--oa-space-sm); flex-wrap: wrap;">
                <span class="oa-action-badge" x-text="currentAction?.type?.toUpperCase()"></span>
                <span style="font-family: var(--oa-font-mono); font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary);"
                      x-text="JSON.stringify(currentAction || {{}})"></span>
            </div>
        </div>
        <div x-show="!currentAction" style="color: var(--oa-text-muted); font-size: var(--oa-font-size-sm);">
            No action for current frame
        </div>
    </div>
</div>'''
