"""Audio capture and transcription.

This module provides audio recording with optional Whisper transcription,
following OpenAdapt's proven implementation.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from openadapt_capture.events import AudioChunkEvent

if TYPE_CHECKING:
    import numpy as np

# Optional dependencies - imported at runtime
_sounddevice = None
_soundfile = None
_whisper = None
_np = None


def _import_audio_deps() -> None:
    """Import audio dependencies lazily."""
    global _sounddevice, _soundfile, _np
    if _sounddevice is None:
        try:
            import numpy as np
            import sounddevice
            import soundfile

            _sounddevice = sounddevice
            _soundfile = soundfile
            _np = np
        except ImportError as e:
            raise ImportError(
                "Audio dependencies required. Install with: "
                "pip install sounddevice soundfile numpy"
            ) from e


def _import_whisper() -> None:
    """Import whisper lazily."""
    global _whisper
    if _whisper is None:
        try:
            import whisper

            _whisper = whisper
        except ImportError as e:
            raise ImportError(
                "Whisper is required for transcription. Install with: "
                "pip install openai-whisper"
            ) from e


def _get_timestamp() -> float:
    """Get current timestamp."""
    return time.time()


# =============================================================================
# Audio Recorder
# =============================================================================


class AudioRecorder:
    """Records audio from the default microphone.

    Usage:
        recorder = AudioRecorder()
        recorder.start()
        # ... record for some time ...
        recorder.stop()

        # Get the recorded audio
        audio_data = recorder.get_audio()

        # Optionally transcribe
        text = recorder.transcribe()
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        channels: int = 1,
    ) -> None:
        """Initialize audio recorder.

        Args:
            sample_rate: Audio sample rate in Hz (44100 for quality, Whisper resamples).
            channels: Number of audio channels (1 for mono).
        """
        _import_audio_deps()

        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list["np.ndarray"] = []
        self._stream = None
        self._running = False
        self._start_time: float | None = None
        self._lock = threading.Lock()

    def _audio_callback(
        self,
        indata: "np.ndarray",
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        """Callback for audio stream."""
        if status:
            # Log any audio issues (overflow, underflow)
            import sys
            print(f"Audio status: {status}", file=sys.stderr)
        with self._lock:
            self._frames.append(indata.copy())

    @property
    def start_time(self) -> float | None:
        """Get the recording start time."""
        return self._start_time

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._running

    def start(self) -> None:
        """Start recording audio."""
        if self._running:
            return

        with self._lock:
            self._frames = []

        self._stream = _sounddevice.InputStream(
            callback=self._audio_callback,
            samplerate=self.sample_rate,
            channels=self.channels,
        )
        self._start_time = _get_timestamp()
        self._stream.start()
        self._running = True

    def stop(self) -> None:
        """Stop recording audio."""
        if not self._running:
            return

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._running = False

    def get_audio(self) -> "np.ndarray":
        """Get the recorded audio as a numpy array.

        Returns:
            Numpy array of audio samples (float32, normalized).
        """
        with self._lock:
            if not self._frames:
                return _np.array([], dtype=_np.float32)

            concatenated = _np.concatenate(self._frames, axis=0)
            return concatenated.flatten().astype(_np.float32)

    def get_duration(self) -> float:
        """Get the duration of recorded audio in seconds."""
        audio = self.get_audio()
        return len(audio) / self.sample_rate

    def save_flac(self, output_path: str | Path) -> None:
        """Save recorded audio to FLAC file.

        Args:
            output_path: Path to output FLAC file.
        """
        audio = self.get_audio()
        _soundfile.write(str(output_path), audio, self.sample_rate, format="FLAC")

    def get_flac_bytes(self) -> bytes:
        """Get recorded audio as FLAC-compressed bytes.

        Returns:
            FLAC-compressed audio data.
        """
        audio = self.get_audio()
        buffer = io.BytesIO()
        _soundfile.write(buffer, audio, self.sample_rate, format="FLAC")
        return buffer.getvalue()

    def transcribe(
        self,
        model_name: str = "base",
        word_timestamps: bool = True,
    ) -> dict[str, Any]:
        """Transcribe recorded audio using Whisper.

        Args:
            model_name: Whisper model to use (tiny, base, small, medium, large).
            word_timestamps: Whether to include word-level timestamps.

        Returns:
            Transcription result dict with 'text' and 'segments'.
        """
        _import_whisper()

        audio = self.get_audio()
        if len(audio) == 0:
            return {"text": "", "segments": []}

        model = _whisper.load_model(model_name)
        result = model.transcribe(
            audio,
            word_timestamps=word_timestamps,
            fp16=False,  # Use float32 for CPU compatibility
        )
        return result

    def __enter__(self) -> "AudioRecorder":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()


# =============================================================================
# Audio Chunk Generator
# =============================================================================


class AudioChunkGenerator:
    """Generates AudioChunkEvents from recorded audio.

    Splits audio into chunks for storage and processing.
    """

    def __init__(
        self,
        chunk_duration: float = 30.0,
        sample_rate: int = 16000,
    ) -> None:
        """Initialize chunk generator.

        Args:
            chunk_duration: Duration of each chunk in seconds.
            sample_rate: Audio sample rate.
        """
        self.chunk_duration = chunk_duration
        self.sample_rate = sample_rate

    def generate_chunks(
        self,
        audio: "np.ndarray",
        start_time: float,
        transcription: dict[str, Any] | None = None,
    ) -> list[AudioChunkEvent]:
        """Generate AudioChunkEvents from audio data.

        Args:
            audio: Audio data as numpy array.
            start_time: Unix timestamp of audio start.
            transcription: Optional transcription result.

        Returns:
            List of AudioChunkEvent objects.
        """
        _import_audio_deps()

        total_samples = len(audio)
        samples_per_chunk = int(self.chunk_duration * self.sample_rate)

        events = []
        chunk_idx = 0

        while chunk_idx * samples_per_chunk < total_samples:
            start_sample = chunk_idx * samples_per_chunk
            end_sample = min((chunk_idx + 1) * samples_per_chunk, total_samples)

            chunk_start_time = start_time + (start_sample / self.sample_rate)
            chunk_end_time = start_time + (end_sample / self.sample_rate)

            # Extract transcription for this chunk if available
            chunk_text = None
            if transcription and transcription.get("segments"):
                chunk_words = []
                for segment in transcription["segments"]:
                    if "words" in segment:
                        for word in segment["words"]:
                            word_start = start_time + word["start"]
                            word_end = start_time + word["end"]
                            if word_start >= chunk_start_time and word_end <= chunk_end_time:
                                chunk_words.append(word["word"])
                chunk_text = " ".join(chunk_words).strip() if chunk_words else None

            event = AudioChunkEvent(
                timestamp=chunk_start_time,
                start_time=chunk_start_time,
                end_time=chunk_end_time,
                transcription=chunk_text,
            )
            events.append(event)
            chunk_idx += 1

        return events


# =============================================================================
# Continuous Audio Capture
# =============================================================================


class ContinuousAudioCapture:
    """Continuous audio capture with streaming transcription support.

    For long captures, provides periodic callbacks with audio chunks.

    Usage:
        def on_chunk(event, audio_bytes):
            print(f"Chunk: {event.start_time} - {event.end_time}")

        capture = ContinuousAudioCapture(on_chunk, chunk_duration=30.0)
        capture.start()
        # ... capture audio ...
        capture.stop()
    """

    def __init__(
        self,
        callback: Callable[[AudioChunkEvent, bytes], None],
        chunk_duration: float = 30.0,
        sample_rate: int = 16000,
        channels: int = 1,
        transcribe: bool = False,
        whisper_model: str = "base",
    ) -> None:
        """Initialize continuous audio capture.

        Args:
            callback: Function called with (event, audio_bytes) for each chunk.
            chunk_duration: Duration of each chunk in seconds.
            sample_rate: Audio sample rate.
            channels: Number of channels.
            transcribe: Whether to transcribe each chunk.
            whisper_model: Whisper model to use for transcription.
        """
        _import_audio_deps()

        self.callback = callback
        self.chunk_duration = chunk_duration
        self.sample_rate = sample_rate
        self.channels = channels
        self.transcribe = transcribe
        self.whisper_model = whisper_model

        self._recorder: AudioRecorder | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        if transcribe:
            _import_whisper()
            self._whisper_model = _whisper.load_model(whisper_model)
        else:
            self._whisper_model = None

    def _process_chunk(self, audio: "np.ndarray", start_time: float) -> None:
        """Process a single audio chunk."""
        # Get transcription if enabled
        transcription = None
        if self._whisper_model is not None:
            try:
                transcription = self._whisper_model.transcribe(
                    audio,
                    word_timestamps=True,
                    fp16=False,
                )
                transcription_text = transcription.get("text", "").strip()
            except Exception:
                transcription_text = None
        else:
            transcription_text = None

        end_time = start_time + (len(audio) / self.sample_rate)

        event = AudioChunkEvent(
            timestamp=start_time,
            start_time=start_time,
            end_time=end_time,
            transcription=transcription_text,
        )

        # Convert to FLAC bytes
        buffer = io.BytesIO()
        _soundfile.write(buffer, audio, self.sample_rate, format="FLAC")
        audio_bytes = buffer.getvalue()

        self.callback(event, audio_bytes)

    def _capture_loop(self) -> None:
        """Main capture loop."""
        samples_per_chunk = int(self.chunk_duration * self.sample_rate)
        chunk_start_time = _get_timestamp()

        while not self._stop_event.is_set():
            # Wait for chunk duration
            self._stop_event.wait(self.chunk_duration)

            if self._recorder is not None:
                audio = self._recorder.get_audio()

                if len(audio) >= samples_per_chunk:
                    # Get samples for this chunk
                    chunk_audio = audio[:samples_per_chunk].copy()

                    # Process in separate thread to not block capture
                    threading.Thread(
                        target=self._process_chunk,
                        args=(chunk_audio, chunk_start_time),
                        daemon=True,
                    ).start()

                    # Reset recorder for next chunk
                    with self._recorder._lock:
                        self._recorder._frames = [
                            audio[samples_per_chunk:].reshape(-1, 1)
                        ] if len(audio) > samples_per_chunk else []

                    chunk_start_time = _get_timestamp()

    def start(self) -> None:
        """Start continuous audio capture."""
        if self._running:
            return

        self._recorder = AudioRecorder(
            sample_rate=self.sample_rate,
            channels=self.channels,
        )
        self._recorder.start()

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self) -> None:
        """Stop continuous audio capture and process remaining audio."""
        if not self._running:
            return

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        # Process any remaining audio
        if self._recorder is not None:
            audio = self._recorder.get_audio()
            if len(audio) > 0:
                start_time = (
                    self._recorder.start_time + self._recorder.get_duration()
                    - (len(audio) / self.sample_rate)
                )
                self._process_chunk(audio, start_time)

            self._recorder.stop()
            self._recorder = None

        self._running = False

    def __enter__(self) -> "ContinuousAudioCapture":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()
