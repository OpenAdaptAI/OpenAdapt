"""Audio narration privacy boundary.

These tests pin the boundaries that make microphone capture admissible at all.
They are deliberately about durable safety invariants rather than wording:

1. Transcription is on-device only; there is no network recognizer and no
   silent fallback to one.
2. Narration is off by default, and the waveform is not retained by default.
3. A capture that could not be transcribed locally is refused before the
   microphone is opened, rather than recorded and then discarded.
4. Transcript text is never written to logs.
"""

from __future__ import annotations

import ast
import inspect
import multiprocessing
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from openadapt_capture import audio as audio_mod
from openadapt_capture.audio import (
    LOCAL_TRANSCRIPTION_BACKENDS,
    NoLocalTranscriptionBackend,
    _get_best_transcription_backend,
    require_local_transcription_backend,
    resolve_transcription_backend,
)

PACKAGE_ROOT = Path(audio_mod.__file__).resolve().parent


# ---------------------------------------------------------------------------
# 1. On-device only: no network recognizer, and no silent fallback to one.
# ---------------------------------------------------------------------------


def test_local_backend_allowlist_has_no_network_backend() -> None:
    """The allow-list must contain only engines that run on this machine."""
    assert LOCAL_TRANSCRIPTION_BACKENDS == ("faster-whisper", "openai-whisper")
    assert "api" not in LOCAL_TRANSCRIPTION_BACKENDS


def test_auto_detect_never_returns_a_network_backend(monkeypatch) -> None:
    """With no local engine installed, auto-detect returns None, not 'api'.

    This is the regression that matters most: auto-detect previously returned
    "api" when no local backend was importable, so ``capture transcribe`` with
    default arguments on a default install resolved to uploading the raw
    waveform to a third party.
    """
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _no_whisper(name, *args, **kwargs):
        if name in ("faster_whisper", "whisper"):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_whisper)

    backend = _get_best_transcription_backend()
    assert backend is None, f"auto-detect leaked a non-local backend: {backend!r}"


def test_require_backend_refuses_when_none_installed(monkeypatch) -> None:
    """A missing local engine is an error, never a reason to upload."""
    monkeypatch.setattr(audio_mod, "_get_best_transcription_backend", lambda: None)

    with pytest.raises(NoLocalTranscriptionBackend) as excinfo:
        require_local_transcription_backend()

    # The refusal must tell the operator how to proceed locally.
    assert "transcribe-fast" in str(excinfo.value)


def test_resolve_rejects_a_non_local_backend_name() -> None:
    """A backend name outside the allow-list is refused, not passed through."""
    for name in ("api", "openai", "whisper-1", "cloud"):
        with pytest.raises(ValueError, match="on-device only"):
            resolve_transcription_backend(name)


def test_resolve_passes_through_a_local_backend() -> None:
    """A valid on-device backend resolves to itself without importing it."""
    for name in LOCAL_TRANSCRIPTION_BACKENDS:
        assert resolve_transcription_backend(name) == name


def test_no_module_uploads_audio() -> None:
    """No module in the package may send audio to a remote service.

    A source-level contract check, so that reintroducing an upload path fails
    here rather than in a customer's recording.
    """
    forbidden = (
        "audio.transcriptions",
        "transcriptions.create",
        "openai_api_key",
    )
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {token}")
    assert not offenders, f"audio upload path present: {offenders}"


def test_openai_sdk_is_not_a_dependency() -> None:
    """The openai SDK existed only to upload waveforms; it must stay gone."""
    pyproject = (PACKAGE_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    deps_block = pyproject.split("[project.optional-dependencies]")[0]
    assert '"openai>' not in deps_block
    assert '"openai=' not in deps_block


# ---------------------------------------------------------------------------
# 2. Off by default; waveform not retained by default.
# ---------------------------------------------------------------------------


def test_narration_and_waveform_retention_are_off_by_default() -> None:
    """Defaults must not record or retain audio."""
    from openadapt_capture.config import Settings

    fields = Settings.model_fields
    assert fields["RECORD_AUDIO"].default is False
    assert fields["RECORD_AUDIO_RETAIN_WAVEFORM"].default is False


def test_html_viewer_does_not_embed_audio_by_default() -> None:
    """The self-contained viewer is easy to forward; keep voice out of it."""
    from openadapt_capture.visualize.html import create_html

    assert inspect.signature(create_html).parameters["include_audio"].default is False


# ---------------------------------------------------------------------------
# 3. Refuse before the microphone opens.
# ---------------------------------------------------------------------------


def test_record_audio_refuses_before_opening_the_microphone(monkeypatch) -> None:
    """No local backend => refuse without ever constructing an InputStream.

    Previously the stream was opened, the entire session was captured, and the
    missing backend surfaced only at stop time -- recording the operator for
    nothing and then failing the whole capture.
    """
    from openadapt_capture import recorder as recorder_mod

    opened: list[str] = []

    fake_sd = types.ModuleType("sounddevice")

    class _NeverOpened:
        def __init__(self, *args, **kwargs):
            opened.append("InputStream")
            raise AssertionError("microphone opened despite no local backend")

    fake_sd.InputStream = _NeverOpened
    fake_sd.CallbackFlags = object
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(audio_mod, "_get_best_transcription_backend", lambda: None)

    with pytest.raises(NoLocalTranscriptionBackend):
        recorder_mod.record_audio(
            recording=object(),
            db_path=":memory:",
            terminate_processing=multiprocessing.Event(),
            started_event=multiprocessing.Event(),
        )

    assert opened == [], "the microphone must not be opened before the refusal"


def test_record_audio_preflight_precedes_stream_construction_in_source() -> None:
    """The refusal must lexically precede the InputStream call.

    Ordering is the whole safety property here, so pin it structurally rather
    than relying on the mock above alone.
    """
    from openadapt_capture import recorder as recorder_mod

    source = inspect.getsource(recorder_mod.record_audio)
    preflight = source.index("require_local_transcription_backend()")
    stream = source.index("InputStream(")
    assert preflight < stream


# ---------------------------------------------------------------------------
# 4. The transcript is never logged.
# ---------------------------------------------------------------------------


def test_transcript_text_is_never_logged() -> None:
    """No logger call may take the transcript text as an argument.

    Narration can contain names, dates of birth, and diagnoses; logging it
    would copy that into terminal scrollback and every configured log sink.
    """
    from openadapt_capture import recorder as recorder_mod

    source = textwrap.dedent(inspect.getsource(recorder_mod.record_audio))
    tree = ast.parse(source)

    leaked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "logger":
            continue
        rendered = " ".join(ast.dump(arg) for arg in node.args)
        # The transcript lives in result_info["text"]; its length is fine.
        if "'text'" in rendered and "len" not in rendered:
            leaked.append(ast.dump(node))
    assert not leaked, f"transcript text reaches a logger: {leaked}"


# ---------------------------------------------------------------------------
# 5. Waveform retention behaviour.
# ---------------------------------------------------------------------------


class _FakeStream:
    """A microphone stub that yields deterministic non-empty frames."""

    samplerate = 16000

    def __init__(self, callback=None, samplerate=16000, channels=1):
        self._cb = callback
        self.samplerate = samplerate
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


def _run_record_audio(monkeypatch, *, retain: bool) -> dict:
    """Drive record_audio with a stubbed microphone and transcriber."""
    from openadapt_capture import recorder as recorder_mod

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.InputStream = _FakeStream
    fake_sd.CallbackFlags = object
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    monkeypatch.setattr(
        audio_mod, "_get_best_transcription_backend", lambda: "faster-whisper"
    )
    monkeypatch.setattr(
        recorder_mod,
        "_transcribe_on_device",
        lambda audio, backend: {"text": "patient name spoken aloud", "segments": []},
    )
    monkeypatch.setattr(
        recorder_mod.config, "RECORD_AUDIO_RETAIN_WAVEFORM", retain, raising=False
    )

    captured: dict = {}

    def _fake_insert(session, audio_data, text, recording, ts, rate, words):
        captured.update(
            audio_data=audio_data, text=text, sample_rate=rate, words=words
        )

    monkeypatch.setattr(recorder_mod.crud, "insert_audio_info", _fake_insert)
    monkeypatch.setattr(recorder_mod, "get_session_for_path", lambda p: None)

    terminate = multiprocessing.Event()
    threading.Timer(0.15, terminate.set).start()
    recorder_mod.record_audio(
        recording=types.SimpleNamespace(timestamp=time.time()),
        db_path=":memory:",
        terminate_processing=terminate,
        started_event=multiprocessing.Event(),
    )
    return captured


def test_waveform_is_discarded_by_default(monkeypatch) -> None:
    """Default narration keeps the transcript and drops the voice."""
    captured = _run_record_audio(monkeypatch, retain=False)

    assert captured["audio_data"] == b"", "waveform retained without opt-in"
    assert captured["text"] == "patient name spoken aloud"


def test_waveform_is_retained_only_on_explicit_opt_in(monkeypatch) -> None:
    """The opt-in still works, so this is a default change and not a removal."""
    captured = _run_record_audio(monkeypatch, retain=True)

    assert captured["audio_data"], "explicit retention produced no waveform"
    assert captured["audio_data"][:4] == b"fLaC"


def test_missing_microphone_does_not_crash_the_capture(monkeypatch) -> None:
    """No frames (permission denied / no device) must not raise ValueError.

    np.concatenate([]) previously raised, killing the audio subprocess and
    failing the entire recording before post-processing.
    """
    from openadapt_capture import recorder as recorder_mod

    class _SilentStream(_FakeStream):
        def start(self):  # never invokes the callback
            pass

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.InputStream = _SilentStream
    fake_sd.CallbackFlags = object
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(
        audio_mod, "_get_best_transcription_backend", lambda: "faster-whisper"
    )

    captured: dict = {}
    monkeypatch.setattr(
        recorder_mod.crud,
        "insert_audio_info",
        lambda s, a, t, r, ts, rate, w: captured.update(audio_data=a, text=t),
    )
    monkeypatch.setattr(recorder_mod, "get_session_for_path", lambda p: None)

    terminate = multiprocessing.Event()
    threading.Timer(0.1, terminate.set).start()
    recorder_mod.record_audio(
        recording=types.SimpleNamespace(timestamp=time.time()),
        db_path=":memory:",
        terminate_processing=terminate,
        started_event=multiprocessing.Event(),
    )

    assert captured == {"audio_data": b"", "text": ""}
