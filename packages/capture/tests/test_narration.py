"""Narration beside a recording: timed sentences, scrubbed before storing.

The audio fixture ``fixtures/narration/medicare_copay.wav`` is synthetic
speech made on macOS with ``say`` ("Patient John Smith. If the insurance is
Medicare, skip the copay screen."). The name in it is invented.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from openadapt_capture import audio as audio_mod
from openadapt_capture import share_scrub
from openadapt_capture.narration import (
    NARRATION_FILE,
    NarrationNotScrubbed,
    Utterance,
    load_waveform,
    read_narration,
    scrub_utterances,
    utterances_from_transcript,
    write_narration,
)

FIXTURE = Path(__file__).parent / "fixtures" / "narration" / "medicare_copay.wav"


class NameScrubber:
    """Stand-in for openadapt-privacy: removes the one invented name."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("John Smith", "<PERSON>")


@pytest.fixture()
def scrubber():
    share_scrub.set_scrubber(NameScrubber())
    yield share_scrub.get_scrubber()
    share_scrub.reset_scrubber()


def test_every_segment_is_cut_into_timed_sentences():
    result = {
        "segments": [
            {
                "start": 0.0,
                "end": 4.0,
                "text": " Open the chart. If it's Medicare, skip copay.",
                "words": [
                    {"word": " Open", "start": 0.0, "end": 0.4},
                    {"word": " the", "start": 0.4, "end": 0.6},
                    {"word": " chart.", "start": 0.6, "end": 1.0},
                    {"word": " If", "start": 1.5, "end": 1.7},
                    {"word": " it's", "start": 1.7, "end": 2.0},
                    {"word": " Medicare,", "start": 2.0, "end": 2.6},
                    {"word": " skip", "start": 2.8, "end": 3.1},
                    {"word": " copay.", "start": 3.1, "end": 3.6},
                ],
            },
            {"start": 6.0, "end": 7.0, "text": " Now save."},
        ]
    }
    utts = utterances_from_transcript(result, offset=100.0)
    assert utts == [
        Utterance(100.0, 101.0, "Open the chart."),
        Utterance(101.5, 103.6, "If it's Medicare, skip copay."),
        Utterance(106.0, 107.0, "Now save."),
    ]


def test_scrubbing_is_required_and_removes_the_name():
    utts = [Utterance(0.0, 1.0, "Patient John Smith, insurance Medicare.")]
    with pytest.raises(NarrationNotScrubbed):
        scrub_utterances(utts, None)
    (clean,) = scrub_utterances(utts, NameScrubber())
    assert "John Smith" not in clean.text
    assert "Medicare" in clean.text


def test_sidecar_records_the_scrubber_and_refuses_unscrubbed_files(tmp_path):
    utts = scrub_utterances([Utterance(1.0, 2.0, "John Smith is here.")], NameScrubber())
    path = write_narration(tmp_path, utts, scrubber=NameScrubber(), audio_start=1.0)
    data = json.loads(path.read_text())
    assert data["scrubbed"] is True
    assert data["scrubbed_by"]["class"].endswith("NameScrubber")
    assert "John Smith" not in path.read_text()
    assert read_narration(tmp_path) == utts

    data["scrubbed"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="scrubbed"):
        read_narration(tmp_path)


class _Stream:
    samplerate = 16000

    def __init__(self, callback=None, samplerate=16000, channels=1):
        self._cb = callback
        self._stop = threading.Event()

    def start(self):
        import numpy as np

        def loop():
            while not self._stop.is_set():
                self._cb(np.zeros((160, 1), dtype=np.float32), 160, None, None)
                time.sleep(0.005)

        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def close(self):
        pass


def _record(monkeypatch, tmp_path, transcript):
    from openadapt_capture import recorder as recorder_mod

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.InputStream = _Stream
    fake_sd.CallbackFlags = object
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(audio_mod, "_get_best_transcription_backend", lambda: "faster-whisper")
    monkeypatch.setattr(recorder_mod, "_transcribe_on_device", lambda a, b: transcript)
    stored: dict = {}
    monkeypatch.setattr(
        recorder_mod.crud,
        "insert_audio_info",
        lambda s, a, text, r, ts, rate, words: stored.update(text=text, words=words, ts=ts),
    )
    monkeypatch.setattr(recorder_mod, "get_session_for_path", lambda p: None)
    terminate = multiprocessing.Event()
    threading.Timer(0.1, terminate.set).start()
    recorder_mod.record_audio(
        recording=types.SimpleNamespace(timestamp=time.time()),
        db_path=str(tmp_path / "recording.db"),
        terminate_processing=terminate,
        started_event=multiprocessing.Event(),
    )
    return stored


def test_recorded_transcript_is_scrubbed_before_it_is_stored(monkeypatch, tmp_path, scrubber):
    transcript = {
        "text": "Patient John Smith. If the insurance is Medicare, skip the copay screen.",
        "segments": [
            {"start": 0.2, "end": 1.6, "text": " Patient John Smith."},
            {"start": 2.0, "end": 4.4, "text": " If the insurance is Medicare, skip the copay screen."},
        ],
    }
    stored = _record(monkeypatch, tmp_path, transcript)
    assert "John Smith" not in stored["text"]
    assert stored["words"] == []
    sidecar = (tmp_path / NARRATION_FILE).read_text()
    assert "John Smith" not in sidecar
    utts = read_narration(tmp_path)
    assert [u.text for u in utts] == [
        "Patient <PERSON>.",
        "If the insurance is Medicare, skip the copay screen.",
    ]
    # Times are on the recording clock: audio start plus the segment offset.
    assert utts[1].start == pytest.approx(stored["ts"] + 2.0, abs=1e-3)


def test_no_scrubber_means_the_microphone_never_opens(monkeypatch, tmp_path):
    from openadapt_capture import recorder as recorder_mod

    share_scrub.set_scrubber(None)
    opened: list[str] = []

    class _Never:
        def __init__(self, *a, **k):
            opened.append("mic")

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.InputStream = _Never
    fake_sd.CallbackFlags = object
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(audio_mod, "_get_best_transcription_backend", lambda: "faster-whisper")
    try:
        with pytest.raises(NarrationNotScrubbed):
            recorder_mod.record_audio(
                recording=object(),
                db_path=str(tmp_path / "recording.db"),
                terminate_processing=multiprocessing.Event(),
                started_event=multiprocessing.Event(),
            )
    finally:
        share_scrub.reset_scrubber()
    assert opened == []
    assert not (tmp_path / NARRATION_FILE).exists()


def test_fixture_is_16khz_synthetic_speech():
    wave = load_waveform(FIXTURE)
    assert wave.dtype.name == "float32"
    assert 2.0 < len(wave) / 16000 < 8.0
    assert float(abs(wave).max()) > 0.05


@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None,
    reason="faster-whisper (audio extra) not installed",
)
def test_local_whisper_transcribes_the_fixture(scrubber):
    """End to end on this machine: synthetic speech -> local Whisper -> scrub.

    Needs the ``tiny.en`` model in the local Hugging Face cache; skipped when
    it is not there, so the test never downloads anything.
    """
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache("Systran/faster-whisper-tiny.en", "model.bin")
    except Exception:  # noqa: BLE001
        cached = None
    if not isinstance(cached, str):
        pytest.skip("tiny.en Whisper model is not cached locally")
    from openadapt_capture.narration import transcribe_file

    result = transcribe_file(FIXTURE, backend="faster-whisper", model=str(Path(cached).parent))
    utts = scrub_utterances(utterances_from_transcript(result), scrubber)
    text = " ".join(u.text for u in utts).lower()
    assert "medicare" in text and "copay" in text
    assert "john smith" not in text
