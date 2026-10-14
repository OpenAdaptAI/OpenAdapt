"""Command-line interface for openadapt-capture.

Usage:
    capture install-ffmpeg
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
    images: bool = False,
    browser_events: bool = False,
    send_profile: bool = False,
    window_owner: str | None = None,
    window_title: str | None = None,
) -> None:
    """Record GUI interactions.

    Args:
        output_dir: Directory to save capture.
        description: Optional task description.
        video: Capture MP4 video (default: True). Requires an externally
            provisioned FFmpeg executable.
        audio: Record microphone narration (default: False). Audio is
            transcribed on this machine and the waveform is then discarded
            unless RECORD_AUDIO_RETAIN_WAVEFORM is explicitly enabled.
            Requires an on-device transcription backend to be installed.
        images: Also save screenshots as PNGs (default: False).
        browser_events: Removed legacy opt-in. If true, the command fails
            before it starts a recorder. Use openadapt-flow Playwright launch
            or attach recording instead.
        send_profile: Send profiling data via wormhole after recording (default: False).
        window_owner: Owner-app substring for window-scoped recording — capture
            ONE window in its own pixel space (e.g. --window-owner Parallels).
        window_title: Title substring to disambiguate the target window.
    """
    if browser_events:
        print(
            "The Chrome-extension WebSocket prototype is not part of the "
            "supported openadapt-capture runtime."
        )
        print("Use openadapt-flow Playwright launch or attach recording instead.")
        raise SystemExit(2)

    import time

    from openadapt_capture.recorder import Recorder

    output_dir = str(Path(output_dir).resolve())

    window = None
    if window_owner or window_title:
        window = {"owner": window_owner, "title": window_title}

    if audio:
        # Refuse before the microphone is ever opened, and make the microphone
        # visible in the operator's own terminal rather than relying solely on
        # the OS recording indicator.
        from openadapt_capture.audio import (
            NoLocalTranscriptionBackend,
            require_local_transcription_backend,
        )
        from openadapt_capture.config import settings

        try:
            audio_backend = require_local_transcription_backend()
        except NoLocalTranscriptionBackend as exc:
            print(str(exc))
            raise SystemExit(1) from exc

        retain = settings.RECORD_AUDIO_RETAIN_WAVEFORM
        print("MICROPHONE ON: this recording captures spoken narration.")
        print(f"  Transcribed on-device with {audio_backend}; audio is never uploaded.")
        print(
            "  Waveform retained in the capture database."
            if retain
            else "  Waveform discarded after transcription; only text is retained."
        )
        print(
            "  Anything said aloud is retained as text. Do not speak names, "
            "dates of birth, or other identifying details."
        )
        print()

    print(f"Recording to: {output_dir}")
    if window:
        print(f"Window-scoped: owner={window_owner!r} title={window_title!r}")
    print("Press Ctrl+C or type stop sequence to stop recording...")
    print()

    with Recorder(
        output_dir,
        task_description=description or "",
        capture_video=video,
        capture_audio=audio,
        capture_images=images,
        send_profile=send_profile,
        window=window,
    ) as recorder:
        if not recorder.wait_for_ready():
            print("Recording did not become ready. No successful capture was saved.")
            raise SystemExit(1)
        print(f"Capture session: {recorder.control_session_id}")
        print(
            "Stop from another terminal: "
            f"capture stop --session-id {recorder.control_session_id}"
        )
        try:
            while recorder.is_recording:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print()
    print(f"Recorded {recorder.event_count} events")
    print(f"Saved to: {output_dir}")


def status(
    session_id: str | None = None,
    timeout: float = 5.0,
    runtime_dir: str | None = None,
) -> None:
    """Show the authenticated status of an active recorder.

    Args:
        session_id: Exact Capture session ID. It can be omitted only when one
            recorder is active.
        timeout: Maximum seconds to wait for the recorder.
        runtime_dir: Owner-only runtime directory override for an embedded
            launcher or a test environment.
    """
    import json

    from openadapt_capture.control import CaptureControlError, status_recording

    try:
        current = status_recording(
            session_id,
            timeout=timeout,
            runtime_dir=runtime_dir,
        )
    except CaptureControlError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    print(json.dumps(current.__dict__, sort_keys=True))


def stop(
    session_id: str | None = None,
    timeout: float = 60.0,
    runtime_dir: str | None = None,
) -> None:
    """Stop one recorder and confirm its finalized Capture session.

    Args:
        session_id: Exact Capture session ID. It can be omitted only when one
            recorder is active.
        timeout: Maximum seconds to wait for finalization and integrity checks.
        runtime_dir: Owner-only runtime directory override for an embedded
            launcher or a test environment.
    """
    import json

    from openadapt_capture.control import CaptureControlError, stop_recording

    try:
        completed = stop_recording(
            session_id,
            timeout=timeout,
            runtime_dir=runtime_dir,
        )
    except CaptureControlError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    print(json.dumps(completed.__dict__, sort_keys=True))


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
    from openadapt_capture.capture import CaptureSession

    capture = CaptureSession.load(capture_dir)

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
    print(f"Browser events: {capture.browser_event_count}")

    # Event type breakdown
    from collections import Counter
    types = Counter(a.type for a in actions)
    if types:
        print("Event types:")
        for event_type, count in types.most_common():
            print(f"  {event_type}: {count}")

    # Browser event breakdown
    if capture.browser_event_count > 0:
        browser_events = capture.browser_events()
        btypes = Counter(type(e).__name__ for e in browser_events)
        print("Browser event types:")
        for btype, count in btypes.most_common():
            print(f"  {btype}: {count}")


def transcribe(
    capture_dir: str,
    model: str = "base",
    backend: str = "auto",
) -> None:
    """Transcribe a capture's audio using an on-device Whisper model.

    Transcription runs entirely on this machine. There is no remote backend:
    a raw waveform cannot be sanitized before upload and discloses the
    speaker's voice as well as their words.

    Args:
        capture_dir: Path to capture directory.
        model: Whisper model to use: tiny, base, small, medium, large.
        backend: On-device transcription backend to use:
            - "auto": Auto-detect best available (faster-whisper > openai-whisper)
            - "faster-whisper": Use faster-whisper (4x faster, recommended)
            - "openai-whisper": Use original openai-whisper
    """
    from openadapt_capture.audio import (
        NoLocalTranscriptionBackend,
        resolve_transcription_backend,
    )

    capture_dir = Path(capture_dir)
    audio_path = capture_dir / "audio.flac"
    transcript_path = capture_dir / "transcript.txt"
    transcript_json_path = capture_dir / "transcript.json"

    if not audio_path.exists():
        print(f"No audio file found at: {audio_path}")
        return

    try:
        backend = resolve_transcription_backend(backend)
    except (NoLocalTranscriptionBackend, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from exc

    print(f"Transcribing on-device with backend: {backend}")

    if backend == "faster-whisper":
        _transcribe_faster_whisper(audio_path, transcript_path, transcript_json_path, model)
    else:
        _transcribe_local(audio_path, transcript_path, transcript_json_path, model)


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


def install_ffmpeg(
    dry_run: bool = False,
    force: bool = False,
    probe: bool = True,
) -> None:
    """Install the pinned FFmpeg build that Capture records video with.

    Capture ships no FFmpeg bytes. This command is the opt-in: it downloads one
    exact archive, refuses it unless its SHA-256 matches the digest compiled
    into this package, and only then makes anything executable. The build is
    LGPL-2.1-or-later; FFmpeg's own licence text is installed beside it, and
    the receipt records the matching upstream source archive.

    An FFmpeg you already configured keeps priority. This one is used only when
    OPENADAPT_FFMPEG_PATH, Recorder(ffmpeg_path=...), Desktop's ffmpeg.json,
    and PATH all come up empty.

    Args:
        dry_run: Print the exact artifact, digest and destination, then stop.
        force: Reinstall even when this build is already present.
        probe: Run a real encode-and-decode check afterwards (default: True).
    """
    from openadapt_capture import ffmpeg_runtime

    try:
        planned = ffmpeg_runtime.plan()
    except ffmpeg_runtime.UnsupportedPlatformError as exc:
        print(str(exc))
        raise SystemExit(1) from exc

    print(f"Artifact:  {planned['url']}")
    print(f"SHA-256:   {planned['archive_sha256']}")
    print(f"Licence:   {planned['license']}")
    print(f"Source:    {planned['source_url']}")
    print(f"Install:   {planned['install_dir']}")
    print()

    if dry_run:
        print("Dry run. Nothing was downloaded.")
        return

    try:
        installed = ffmpeg_runtime.install(force=force)
    except ffmpeg_runtime.FFmpegProvisionError as exc:
        print(str(exc))
        raise SystemExit(1) from exc

    print(f"Installed {installed.build_id}")
    print(f"  ffmpeg   {installed.ffmpeg}")
    print(f"  ffprobe  {installed.ffprobe}")
    print(f"  licence  {installed.license_path}")
    print(f"  receipt  {ffmpeg_runtime.receipt_path()}")

    if not probe:
        return

    from openadapt_capture.video import (
        FFmpegUnavailableError,
        require_video_encoder,
        resolve_ffmpeg,
    )

    try:
        provision = require_video_encoder()
    except FFmpegUnavailableError as exc:
        print()
        print(f"The installed runtime did not pass the encode-and-decode check: {exc}")
        raise SystemExit(1) from exc

    print()
    print(f"Verified encoder {provision.codec} into {provision.muxer}.")
    resolved = resolve_ffmpeg()
    if resolved.executable != installed.ffmpeg:
        print(
            f"Note: recording will use the FFmpeg from {resolved.source} "
            f"({resolved.executable}), which keeps priority over this install. "
            "Set OPENADAPT_FFMPEG_PATH to prefer the installed one."
        )


def uninstall_ffmpeg() -> None:
    """Remove the FFmpeg runtime that `capture install-ffmpeg` installed."""
    from openadapt_capture import ffmpeg_runtime

    root = ffmpeg_runtime.runtime_root()
    if ffmpeg_runtime.uninstall():
        print(f"Removed {root}")
    else:
        print(f"Nothing to remove at {root}")


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
        "status": status,
        "stop": stop,
        "visualize": visualize,
        "info": info,
        "transcribe": transcribe,
        "share": share,
        "install-ffmpeg": install_ffmpeg,
        "uninstall-ffmpeg": uninstall_ffmpeg,
    })


if __name__ == "__main__":
    main()
