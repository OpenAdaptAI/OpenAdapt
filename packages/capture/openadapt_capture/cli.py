"""Command-line interface for openadapt-capture.

Usage:
    capture record ./my_capture
    capture visualize ./my_capture
    capture info ./my_capture
"""

from __future__ import annotations

from pathlib import Path


def record(
    output_dir: str,
    description: str | None = None,
    video: bool = True,
    audio: bool = False,
) -> None:
    """Record GUI interactions.

    Args:
        output_dir: Directory to save capture.
        description: Optional task description.
        video: Whether to capture video (default: True).
        audio: Whether to capture audio (default: False).
    """
    from openadapt_capture import Recorder

    output_dir = Path(output_dir)

    print(f"Recording to: {output_dir}")
    print("Press Enter to stop recording...")
    print()

    with Recorder(
        output_dir,
        task_description=description,
        capture_video=video,
        capture_audio=audio,
    ) as recorder:
        try:
            input()
        except KeyboardInterrupt:
            pass

    print()
    print(f"Captured {recorder.event_count} events")
    print(f"Saved to: {output_dir}")


def visualize(
    capture_dir: str,
    output: str | None = None,
    gif: bool = False,
    html: bool = True,
    fps: int = 10,
    scale: float = 0.5,
    open_viewer: bool = True,
) -> None:
    """Generate visualization from a capture.

    Args:
        capture_dir: Path to capture directory.
        output: Output path (default: capture_dir/viewer.html or .gif).
        gif: Generate GIF instead of HTML.
        html: Generate HTML viewer (default: True).
        fps: Frames per second for GIF.
        scale: Scale factor for GIF frames.
        open_viewer: Open the viewer after generation.
    """
    import subprocess
    import sys

    from openadapt_capture.visualize import create_demo, create_html

    capture_dir = Path(capture_dir)

    if gif:
        output_path = Path(output) if output else capture_dir / "demo.gif"
        print(f"Generating GIF: {output_path}")
        create_demo(capture_dir, output=output_path, fps=fps, scale=scale)
        print(f"Saved: {output_path}")

    if html:
        output_path = Path(output) if output and not gif else capture_dir / "viewer.html"
        print(f"Generating HTML viewer: {output_path}")
        create_html(capture_dir, output=output_path)
        print(f"Saved: {output_path}")

        if open_viewer:
            print("Opening viewer...")
            if sys.platform == "darwin":
                subprocess.run(["open", str(output_path)])
            elif sys.platform == "win32":
                subprocess.run(["start", str(output_path)], shell=True)
            else:
                subprocess.run(["xdg-open", str(output_path)])


def info(capture_dir: str) -> None:
    """Show information about a capture.

    Args:
        capture_dir: Path to capture directory.
    """
    from openadapt_capture import Capture

    capture = Capture.load(capture_dir)

    print(f"Capture ID: {capture.id}")
    print(f"Platform: {capture.platform}")
    print(f"Screen size: {capture.screen_size[0]}x{capture.screen_size[1]}")
    print(f"Duration: {capture.duration:.2f}s" if capture.duration else "Duration: N/A")
    print(f"Task: {capture.task_description or 'N/A'}")
    print(f"Video: {capture.video_path or 'N/A'}")
    print(f"Audio: {capture.audio_path or 'N/A'}")

    # Count events
    actions = list(capture.actions())
    print(f"Actions: {len(actions)}")

    # Event type breakdown
    from collections import Counter
    types = Counter(a.type for a in actions)
    if types:
        print("Event types:")
        for event_type, count in types.most_common():
            print(f"  {event_type}: {count}")


def transcribe(
    capture_dir: str,
    model: str = "base",
    api: bool = False,
    backend: str = "auto",
) -> None:
    """Transcribe audio from a capture using Whisper.

    Args:
        capture_dir: Path to capture directory.
        model: Whisper model to use. Local: tiny, base, small, medium, large.
               API: whisper-1 (default when --api is used).
        api: Use OpenAI Whisper API instead of local model (faster, requires API key).
             Deprecated: use --backend=api instead.
        backend: Transcription backend to use:
            - "auto": Auto-detect best available (faster-whisper > openai-whisper)
            - "faster-whisper": Use faster-whisper (4x faster, recommended)
            - "openai-whisper": Use original openai-whisper
            - "api": Use OpenAI Whisper API (requires API key)
    """

    capture_dir = Path(capture_dir)
    audio_path = capture_dir / "audio.flac"
    transcript_path = capture_dir / "transcript.txt"
    transcript_json_path = capture_dir / "transcript.json"

    if not audio_path.exists():
        print(f"No audio file found at: {audio_path}")
        return

    # Handle legacy --api flag
    if api:
        backend = "api"

    # Auto-detect backend if not specified
    if backend == "auto":
        from openadapt_capture.audio import _get_best_transcription_backend
        backend = _get_best_transcription_backend()
        print(f"Auto-detected backend: {backend}")

    if backend == "api":
        _transcribe_api(audio_path, transcript_path, transcript_json_path)
    elif backend == "faster-whisper":
        _transcribe_faster_whisper(audio_path, transcript_path, transcript_json_path, model)
    elif backend == "openai-whisper":
        _transcribe_local(audio_path, transcript_path, transcript_json_path, model)
    else:
        print(f"Unknown backend: {backend}")
        print("Valid options: auto, faster-whisper, openai-whisper, api")
        return


def _transcribe_api(
    audio_path: Path,
    transcript_path: Path,
    transcript_json_path: Path,
) -> None:
    """Transcribe using OpenAI Whisper API."""

    from openadapt_capture.config import settings

    if not settings.openai_api_key:
        print("OpenAI API key not found.")
        print("Set OPENAI_API_KEY environment variable or add to .env file.")
        return

    try:
        from openai import OpenAI
    except ImportError:
        print("OpenAI package not installed. Install with: uv add openai")
        return

    print("Transcribing audio with OpenAI Whisper API...")

    client = OpenAI(api_key=settings.openai_api_key)

    with open(audio_path, "rb") as audio_file:
        # Get transcript with timestamps
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    transcript = result.text.strip()

    # Extract segments with timestamps
    segments = []
    for segment in getattr(result, "segments", []) or []:
        segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    # Save transcripts
    _save_transcript(transcript, segments, transcript_path, transcript_json_path)


def _transcribe_local(
    audio_path: Path,
    transcript_path: Path,
    transcript_json_path: Path,
    model: str,
) -> None:
    """Transcribe using local openai-whisper model."""

    print(f"Transcribing audio with openai-whisper ({model} model)...")
    print("This may take a moment...")

    try:
        import whisper
    except ImportError:
        print("Whisper not installed. Install with: pip install openai-whisper")
        print("Or use faster-whisper: pip install faster-whisper")
        return

    # Load model and transcribe with word timestamps
    whisper_model = whisper.load_model(model)
    result = whisper_model.transcribe(str(audio_path), fp16=False, word_timestamps=True)

    transcript = result.get("text", "").strip()

    # Extract segments with timestamps
    segments = []
    for segment in result.get("segments", []):
        segments.append({
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"].strip(),
        })

    # Save transcripts
    _save_transcript(transcript, segments, transcript_path, transcript_json_path)


def _transcribe_faster_whisper(
    audio_path: Path,
    transcript_path: Path,
    transcript_json_path: Path,
    model: str,
) -> None:
    """Transcribe using faster-whisper (4x faster than openai-whisper)."""

    print(f"Transcribing audio with faster-whisper ({model} model)...")
    print("This is 4x faster than openai-whisper...")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed. Install with: pip install faster-whisper")
        print("Or use openai-whisper: pip install openai-whisper")
        return

    # Load model with CPU and int8 for efficiency
    whisper_model = WhisperModel(model, device="cpu", compute_type="int8")

    # Transcribe with word timestamps
    segments_iter, info = whisper_model.transcribe(
        str(audio_path),
        word_timestamps=True,
    )

    # Collect segments
    segments = []
    full_text_parts = []
    for segment in segments_iter:
        segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })
        full_text_parts.append(segment.text)

    transcript = "".join(full_text_parts).strip()

    # Save transcripts
    _save_transcript(transcript, segments, transcript_path, transcript_json_path)


def _save_transcript(
    transcript: str,
    segments: list[dict],
    transcript_path: Path,
    transcript_json_path: Path,
) -> None:
    """Save transcript to files and print output."""
    import json

    # Save plain text transcript
    transcript_path.write_text(transcript, encoding="utf-8")

    # Save JSON with timestamps
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8"
    )

    print(f"Saved transcript to: {transcript_path}")
    print(f"Saved timestamps to: {transcript_json_path}")
    print()
    print("Transcript:")
    print("-" * 40)
    for seg in segments:
        start = seg["start"]
        mins = int(start // 60)
        secs = start % 60
        print(f"[{mins}:{secs:05.2f}] {seg['text']}")


def share(action: str, path_or_code: str, output_dir: str = ".") -> None:
    """Share recordings via Magic Wormhole.

    Args:
        action: Either "send" or "receive".
        path_or_code: Recording path (for send) or wormhole code (for receive).
        output_dir: Output directory for receive (default: current dir).

    Examples:
        capture share send ./my_recording
        capture share receive 7-guitarist-revenge
        capture share receive 7-guitarist-revenge ./recordings
    """
    from openadapt_capture.share import receive, send

    if action == "send":
        send(path_or_code)
    elif action == "receive":
        receive(path_or_code, output_dir)
    else:
        print(f"Unknown action: {action}. Use 'send' or 'receive'.")


def main() -> None:
    """CLI entry point."""
    import fire
    fire.Fire({
        "record": record,
        "visualize": visualize,
        "info": info,
        "transcribe": transcribe,
        "share": share,
    })


if __name__ == "__main__":
    main()
