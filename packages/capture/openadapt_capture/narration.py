"""Spoken narration beside a recording: utterances, scrubbing and the sidecar.

People explain their choices out loud while they demonstrate ("the insurance
is Medicare, so I skip the copay screen"). This module turns an on-device
transcript into timed **utterances**, scrubs patient data out of them with
``openadapt-privacy``, and writes them next to the recording as
``narration.json``. ``openadapt-flow`` reads that file, lines each utterance up
with the recorded steps, and uses it to *propose* (never certify) a branch rule.

Rules this module enforces:

1. **Scrub before storing.** :func:`scrub_utterances` runs every utterance
   through the scrubber before anything is written. With no scrubber (the
   ``privacy`` extra is missing) it raises :class:`NarrationNotScrubbed`, and
   the recorder stores no transcript at all. There is no plaintext fallback.
2. **Sentences, not words.** Word-level timings are used only to cut a segment
   into sentences, then dropped. A name spoken one word at a time cannot be
   recognised by a text scrubber, so per-word text is never stored.
3. **Local only.** :func:`transcribe_file` uses the same on-device backends as
   :mod:`openadapt_capture.audio`. Nothing is uploaded.

Timestamps in ``narration.json`` are on the recording's clock (the same clock
as action events), so an utterance can be matched to the step it explains on
any backend: web, native, RDP or Citrix.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

NARRATION_FILE = "narration.json"
NARRATION_SCHEMA = "openadapt.capture.narration/v1"

_SENTENCE_END = re.compile(r"[.?!]['\")\]]*$")

NO_SCRUBBER_MESSAGE = (
    "Narration is not stored without scrubbing. Spoken words can name a "
    "patient, and openadapt-privacy is not installed to remove that. Install "
    "it with:\n"
    '    pip install "openadapt-capture[audio,privacy]"\n'
    "    python -m spacy download en_core_web_sm"
)


class NarrationNotScrubbed(RuntimeError):
    """The transcript could not be scrubbed, so it was not stored."""

    def __init__(self, message: str = NO_SCRUBBER_MESSAGE) -> None:
        super().__init__(message)


@runtime_checkable
class TextScrubber(Protocol):
    """The part of an ``openadapt-privacy`` provider narration needs."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str: ...


@dataclass(frozen=True)
class Utterance:
    """One spoken sentence, timed on the recording's clock (seconds)."""

    start: float
    end: float
    text: str


def narration_scrubber() -> Optional[TextScrubber]:
    """The scrubber narration uses: the same provider ``share send`` uses."""
    from openadapt_capture.share_scrub import get_scrubber

    return get_scrubber()


def _scrubber_evidence(scrubber: TextScrubber) -> dict[str, Any]:
    try:
        version = metadata.version("openadapt-privacy")
    except metadata.PackageNotFoundError:
        version = None
    return {
        "class": f"{type(scrubber).__module__}.{type(scrubber).__qualname__}",
        "openadapt_privacy_version": version,
    }


def utterances_from_transcript(
    result: dict[str, Any], *, offset: float = 0.0
) -> list[Utterance]:
    """Split a Whisper-style result into sentence utterances.

    ``result`` is what :meth:`openadapt_capture.audio.AudioRecorder.transcribe`
    returns: ``{"segments": [{"start", "end", "text", "words"?}]}`` with times
    relative to the start of the audio. ``offset`` (the audio's start time on
    the recording clock) is added to every time. Every segment is used, not
    only the first. When a segment has word timings it is cut at words that
    end a sentence; otherwise the whole segment is one utterance.
    """
    out: list[Utterance] = []
    for segment in result.get("segments") or []:
        words = segment.get("words") or []
        if words:
            current: list[dict[str, Any]] = []
            for word in words:
                current.append(word)
                if _SENTENCE_END.search(str(word.get("word", "")).strip()):
                    out.append(_join(current, offset))
                    current = []
            if current:
                out.append(_join(current, offset))
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            out.append(
                Utterance(
                    start=round(float(segment.get("start", 0.0)) + offset, 3),
                    end=round(float(segment.get("end", 0.0)) + offset, 3),
                    text=text,
                )
            )
    if not out and str(result.get("text") or "").strip():
        # No segments (some engines, or a stub): one utterance at the start.
        out.append(Utterance(start=offset, end=offset, text=str(result["text"]).strip()))
    return [u for u in out if u.text]


def _join(words: Sequence[dict[str, Any]], offset: float) -> Utterance:
    text = "".join(str(w.get("word", "")) for w in words).strip()
    text = re.sub(r"\s+", " ", text)
    return Utterance(
        start=round(float(words[0].get("start", 0.0)) + offset, 3),
        end=round(float(words[-1].get("end", 0.0)) + offset, 3),
        text=text,
    )


def scrub_utterances(
    utterances: Sequence[Utterance], scrubber: Optional[TextScrubber]
) -> list[Utterance]:
    """Return the utterances with patient data scrubbed out.

    Raises :class:`NarrationNotScrubbed` when there is no scrubber or it
    fails. The caller must then store nothing.
    """
    if scrubber is None:
        raise NarrationNotScrubbed()
    out: list[Utterance] = []
    for u in utterances:
        try:
            text = scrubber.scrub_text(u.text)
        except Exception as exc:  # noqa: BLE001 - any failure means not scrubbed
            raise NarrationNotScrubbed(
                f"The scrubber failed on the transcript, so it was not stored: {exc}"
            ) from exc
        out.append(Utterance(start=u.start, end=u.end, text=str(text or "").strip()))
    return out


def write_narration(
    directory: str | Path,
    utterances: Sequence[Utterance],
    *,
    scrubber: TextScrubber,
    transcription_backend: Optional[str] = None,
    audio_start: Optional[float] = None,
) -> Path:
    """Write already-scrubbed utterances to ``<directory>/narration.json``.

    The file says which scrubber ran. It is written atomically.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": NARRATION_SCHEMA,
        "clock": "recording",
        "audio_start": audio_start,
        "transcription": {"backend": transcription_backend, "on_device": True},
        "scrubbed": True,
        "scrubbed_by": _scrubber_evidence(scrubber),
        "utterances": [asdict(u) for u in utterances],
    }
    path = directory / NARRATION_FILE
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".narration-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_narration(directory: str | Path) -> Optional[list[Utterance]]:
    """Read ``narration.json`` from a capture directory, or ``None``.

    Refuses (``ValueError``) a file that does not say it was scrubbed.
    """
    path = Path(directory) / NARRATION_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != NARRATION_SCHEMA:
        raise ValueError(f"{path} is not a narration file ({data.get('schema')!r})")
    if data.get("scrubbed") is not True:
        raise ValueError(f"{path} does not say it was scrubbed; refusing to read it")
    return [
        Utterance(start=float(u["start"]), end=float(u["end"]), text=str(u["text"]))
        for u in data.get("utterances") or []
    ]


SAMPLE_RATE = 16000


def load_waveform(path: str | Path) -> Any:
    """Read a 16 kHz audio file as a mono float32 array (what Whisper takes).

    WAV is read with the standard library; other formats (FLAC) need
    ``soundfile`` from the ``audio`` extra. Other sample rates are refused
    rather than resampled silently.
    """
    import numpy as np

    path = Path(path)
    if path.suffix.lower() == ".wav":
        import wave

        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            raw = handle.readframes(handle.getnframes())
        if width != 2:
            raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
    else:
        import soundfile

        data, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
        data = data.mean(axis=1)
    if rate != SAMPLE_RATE:
        raise ValueError(f"{path}: sample rate {rate} Hz; narration needs 16000 Hz")
    return np.ascontiguousarray(data, dtype=np.float32)


def transcribe_file(
    path: str | Path, *, backend: str = "auto", model: str = "base"
) -> dict[str, Any]:
    """Transcribe an audio file on this machine (no network recognizer).

    Returns a Whisper-style result with word timings. Raises
    :class:`~openadapt_capture.audio.NoLocalTranscriptionBackend` when no local
    backend is installed.
    """
    from openadapt_capture.audio import resolve_transcription_backend

    chosen = resolve_transcription_backend(backend)
    waveform = load_waveform(path)
    if chosen == "faster-whisper":
        from faster_whisper import WhisperModel

        engine = WhisperModel(model, device="cpu", compute_type="int8")
        segments, info = engine.transcribe(waveform, word_timestamps=True)
        out = []
        for seg in segments:
            item: dict[str, Any] = {"start": seg.start, "end": seg.end, "text": seg.text}
            if seg.words:
                item["words"] = [
                    {"word": w.word, "start": w.start, "end": w.end} for w in seg.words
                ]
            out.append(item)
        return {
            "text": "".join(s["text"] for s in out),
            "segments": out,
            "language": info.language,
            "backend": chosen,
        }
    import whisper

    result = whisper.load_model(model).transcribe(
        waveform, word_timestamps=True, fp16=False
    )
    result["backend"] = chosen
    return result
