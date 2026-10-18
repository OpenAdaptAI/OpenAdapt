"""Generate animated demo GIF/video from capture recordings.

Creates an animated visualization of a recording with event overlays,
suitable for embedding in documentation or sharing.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from PIL import Image

    from openadapt_capture.capture import CaptureSession


def create_demo(
    capture_or_path: "CaptureSession | str | Path",
    output: str | Path | None = None,
    fps: int = 10,
    max_duration: float | None = 30.0,
    max_frames: int | None = None,
    show_events: bool = True,
    show_labels: bool = True,
    show_timestamp: bool = True,
    scale: float = 0.5,
    loop: int = 0,
) -> bytes | None:
    """Generate an animated demo from a capture recording.

    Args:
        capture_or_path: CaptureSession object or path to capture directory.
        output: Output path for GIF/MP4. If None, returns bytes.
        fps: Frames per second for output.
        max_duration: Maximum duration in seconds (None for full recording).
        max_frames: Maximum number of frames (None for unlimited).
        show_events: Whether to overlay event markers.
        show_labels: Whether to show event type labels.
        show_timestamp: Whether to show timestamp overlay.
        scale: Scale factor for output (0.5 = half size).
        loop: Number of times to loop (0 = infinite for GIF).

    Returns:
        GIF bytes if output is None, otherwise None.
    """
    from openadapt_capture.capture import CaptureSession

    # Load capture if path provided
    if isinstance(capture_or_path, (str, Path)):
        capture = CaptureSession.load(capture_or_path)
    else:
        capture = capture_or_path

    # Generate frames
    frames = list(
        _generate_frames(
            capture,
            fps=fps,
            max_duration=max_duration,
            max_frames=max_frames,
            show_events=show_events,
            show_labels=show_labels,
            show_timestamp=show_timestamp,
            scale=scale,
        )
    )

    if not frames:
        return None

    # Determine output format
    if output is not None:
        output = Path(output)
        suffix = output.suffix.lower()
    else:
        suffix = ".gif"

    if suffix == ".gif":
        return _save_gif(frames, output, fps=fps, loop=loop)
    elif suffix in (".mp4", ".webm"):
        return _save_video(frames, output, fps=fps)
    else:
        raise ValueError(f"Unsupported output format: {suffix}")


def _generate_frames(
    capture: "CaptureSession",
    fps: int,
    max_duration: float | None,
    max_frames: int | None,
    show_events: bool,
    show_labels: bool,
    show_timestamp: bool,
    scale: float,
) -> Iterator["Image.Image"]:
    """Generate annotated frames from capture.

    Yields:
        Annotated PIL Images.
    """
    from PIL import Image

    from openadapt_capture.visualize.overlays import annotate_frame

    # Get actions with screenshots
    actions = list(capture.actions())
    if not actions:
        return

    # Determine time range
    start_time = actions[0].timestamp
    if max_duration is not None:
        end_time = start_time + max_duration
    else:
        end_time = actions[-1].timestamp

    # Generate frames at regular intervals
    frame_interval = 1.0 / fps
    current_time = start_time
    frame_count = 0
    action_idx = 0

    # Track recent events to display (with decay)
    active_events: list[dict] = []
    event_duration = 0.5  # How long to show each event

    while current_time <= end_time:
        if max_frames is not None and frame_count >= max_frames:
            break

        # Find the action closest to current time (for screenshot)
        while action_idx < len(actions) - 1 and actions[action_idx + 1].timestamp <= current_time:
            action_idx += 1

        action = actions[action_idx]
        screenshot = action.screenshot

        if screenshot is None:
            current_time += frame_interval
            continue

        # Update active events
        if show_events:
            # Add new events that occurred since last frame
            for a in actions:
                if current_time - frame_interval < a.timestamp <= current_time:
                    event_dict = _action_to_event_dict(a)
                    if event_dict:
                        event_dict["_expire_time"] = a.timestamp + event_duration
                        active_events.append(event_dict)

            # Remove expired events
            active_events = [e for e in active_events if e["_expire_time"] > current_time]

        # Annotate frame
        if show_events or show_timestamp:
            # Prepare events for annotation (remove internal fields)
            events_to_draw = [{k: v for k, v in e.items() if not k.startswith("_")} for e in active_events]
            frame = annotate_frame(
                screenshot,
                events_to_draw,
                show_labels=show_labels,
                show_timestamp=show_timestamp,
                current_time=current_time - start_time,
            )
        else:
            frame = screenshot.copy()

        # Scale if needed
        if scale != 1.0:
            new_size = (int(frame.width * scale), int(frame.height * scale))
            frame = frame.resize(new_size, Image.Resampling.LANCZOS)

        # Convert to RGB for GIF compatibility
        if frame.mode == "RGBA":
            # Create white background
            bg = Image.new("RGB", frame.size, (255, 255, 255))
            bg.paste(frame, mask=frame.split()[3])
            frame = bg
        elif frame.mode != "RGB":
            frame = frame.convert("RGB")

        yield frame
        frame_count += 1
        current_time += frame_interval


def _action_to_event_dict(action) -> dict | None:
    """Convert an Action to an event dict for overlay drawing.

    Args:
        action: Action object from capture.

    Returns:
        Dict with event info, or None if not drawable.
    """
    event_type = action.type if isinstance(action.type, str) else action.type.value

    if "click" in event_type.lower():
        return {
            "type": event_type,
            "x": getattr(action, "x", 0),
            "y": getattr(action, "y", 0),
        }
    elif "drag" in event_type.lower():
        return {
            "type": event_type,
            "x": getattr(action, "x", 0),
            "y": getattr(action, "y", 0),
            "end_x": getattr(action, "end_x", getattr(action, "x", 0)),
            "end_y": getattr(action, "end_y", getattr(action, "y", 0)),
        }
    elif "scroll" in event_type.lower():
        return {
            "type": event_type,
            "x": getattr(action, "x", 0),
            "y": getattr(action, "y", 0),
            "dx": getattr(action, "dx", 0),
            "dy": getattr(action, "dy", 0),
        }
    elif "type" in event_type.lower():
        return {
            "type": event_type,
            "x": getattr(action, "x", 0),
            "y": getattr(action, "y", 0),
            "text": getattr(action, "text", ""),
        }

    return None


def _save_gif(
    frames: list["Image.Image"],
    output: Path | None,
    fps: int,
    loop: int,
) -> bytes | None:
    """Save frames as GIF.

    Args:
        frames: List of PIL Images.
        output: Output path, or None to return bytes.
        fps: Frames per second.
        loop: Loop count (0 = infinite).

    Returns:
        GIF bytes if output is None.
    """
    if not frames:
        return None

    duration = int(1000 / fps)  # milliseconds per frame

    if output is not None:
        frames[0].save(
            output,
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=loop,
            optimize=True,
        )
        return None
    else:
        buf = BytesIO()
        frames[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=loop,
            optimize=True,
        )
        buf.seek(0)
        return buf.read()


def _save_video(
    frames: list["Image.Image"],
    output: Path | None,
    fps: int,
) -> bytes | None:
    """Save frames as video (MP4/WebM).

    Args:
        frames: List of PIL Images.
        output: Output path, or None to return bytes.
        fps: Frames per second.

    Returns:
        Video bytes if output is None.
    """
    try:
        import imageio.v3 as iio
    except ImportError:
        try:
            import imageio as iio
        except ImportError:
            raise ImportError(
                "imageio is required for video output. Install with: uv add imageio"
            )

    import numpy as np

    if not frames:
        return None

    # Convert frames to numpy arrays
    frame_arrays = [np.array(f) for f in frames]

    if output is not None:
        iio.imwrite(output, frame_arrays, fps=fps)
        return None
    else:
        buf = BytesIO()
        iio.imwrite(buf, frame_arrays, fps=fps, extension=".mp4")
        buf.seek(0)
        return buf.read()


def demo_to_base64(
    capture_or_path: "CaptureSession | str | Path",
    **kwargs,
) -> str:
    """Generate demo and return as base64-encoded string.

    Useful for embedding in HTML.

    Args:
        capture_or_path: CaptureSession or path to capture.
        **kwargs: Arguments passed to create_demo.

    Returns:
        Base64-encoded GIF string.
    """
    gif_bytes = create_demo(capture_or_path, output=None, **kwargs)
    if gif_bytes is None:
        return ""
    return base64.b64encode(gif_bytes).decode("utf-8")
