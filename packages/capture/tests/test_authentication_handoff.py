"""Source-time authentication handoff contract tests."""

from __future__ import annotations

import io
import itertools
import json
import multiprocessing
import os
import queue
import threading
import time

import pytest
from PIL import Image

from openadapt_capture import recorder as recorder_module
from openadapt_capture.authentication import (
    AUTHENTICATION_HANDOFF_FILENAME,
    AuthenticationBoundaryError,
    AuthenticationHandoff,
    AuthenticationHandoffController,
    AuthenticationHandoffError,
    AuthenticationHandoffManifest,
    FreshFrameProof,
    _write_manifest,
    frame_sha256,
    load_authentication_handoffs,
)
from openadapt_capture.capture import CaptureSession, InvalidCaptureEvent
from openadapt_capture.config import RecordingConfig, config_override
from openadapt_capture.input_observer import ObservedMouseButton
from openadapt_capture.recorder import OrderedEventJournal, read_screen_events
from openadapt_capture.terminal import (
    ARTIFACT_MANIFEST_FILENAME,
    CAPTURE_TERMINAL_FILENAME,
    seal_capture,
)
from tests.test_capture_terminal import _desktop_capture_directory


class _Clock:
    def __init__(self) -> None:
        self.value = 1.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


def _bound_controller(tmp_path):
    controller = AuthenticationHandoffController()
    controller.bind(tmp_path, _Clock())
    return controller


def test_empty_manifest_is_created_canonically(tmp_path) -> None:
    _bound_controller(tmp_path)

    marker = tmp_path / AUTHENTICATION_HANDOFF_FILENAME
    assert load_authentication_handoffs(tmp_path).intervals == ()
    if os.name != "nt":
        assert marker.stat().st_mode & 0o077 == 0


def test_begin_drains_inflight_retention_before_marker_starts(tmp_path) -> None:
    controller = _bound_controller(tmp_path)
    retention = controller.begin_retention()
    assert retention is not None
    result = []

    thread = threading.Thread(
        target=lambda: result.append(
            controller.begin(
                methods="password_manager",
                requires_user_presence=True,
                saved_account_selected=True,
            )
        )
    )
    thread.start()
    time.sleep(0.05)

    assert controller.protected is True
    assert controller.audio_suppressed.is_set() is False
    assert result == []
    assert controller.begin_retention() is None

    retention.release()
    thread.join(timeout=1)
    assert len(result) == 1
    interval = load_authentication_handoffs(tmp_path).intervals[0]
    assert interval.methods == ("password_manager",)
    assert interval.saved_account_selected is True
    assert interval.outcome is None


def test_begin_waits_for_audio_process_acknowledgement(tmp_path) -> None:
    controller = AuthenticationHandoffController()
    controller.configure_audio(True)
    controller.bind(tmp_path, _Clock())
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            controller.begin(methods="passkey", requires_user_presence=True)
        )
    )
    thread.start()
    time.sleep(0.05)

    assert result == []
    assert controller.audio_suppressed.is_set()
    controller.audio_suppression_ack.set()
    thread.join(timeout=1)

    assert len(result) == 1


def test_begin_barrier_timeout_stays_protected_and_fails_closed(tmp_path) -> None:
    controller = AuthenticationHandoffController()
    controller.configure_audio(True)
    controller.bind(tmp_path, _Clock())

    with pytest.raises(AuthenticationBoundaryError, match="protected boundary"):
        controller.begin(
            methods="passkey",
            requires_user_presence=True,
            timeout=0.01,
        )

    assert controller.protected is True
    assert controller.audio_suppressed.is_set()
    assert load_authentication_handoffs(tmp_path).intervals == ()
    with pytest.raises(AuthenticationBoundaryError, match="unmarked"):
        controller.abort_active()


def test_end_waits_for_fresh_frame_before_reopening_sources(tmp_path) -> None:
    controller = _bound_controller(tmp_path)
    handle = controller.begin(
        methods=("password_manager", "mfa"),
        requires_user_presence=True,
    )
    result = []
    thread = threading.Thread(
        target=lambda: result.append(controller.end(handle, outcome="completed", timeout=1))
    )
    thread.start()
    time.sleep(0.05)

    assert controller.begin_retention() is None
    resume = controller.begin_screen_retention()
    assert resume is not None and resume.resume
    image = Image.new("RGB", (2, 2), "green")
    controller.complete_resume_frame(
        resume,
        timestamp=3.0,
        source_ordinal=7,
        frame_sha256=frame_sha256(image),
        capture_source="desktop-screenshot",
        window_geometry_generation=None,
    )
    resume.release()
    thread.join(timeout=1)

    assert len(result) == 1
    assert result[0].outcome == "completed"
    assert result[0].resume_frame.source_ordinal == 7
    assert controller.audio_suppressed.is_set() is False
    normal = controller.begin_retention()
    assert normal is not None
    normal.release()


def test_resume_timeout_keeps_capture_protected(tmp_path) -> None:
    controller = _bound_controller(tmp_path)
    handle = controller.begin(methods="passkey", requires_user_presence=True)

    with pytest.raises(AuthenticationHandoffError, match="remains protected"):
        controller.end(handle, timeout=0.01)

    assert controller.protected is True
    assert controller.begin_retention() is None


def test_shutdown_records_aborted_handoff_without_resume_claim(tmp_path) -> None:
    controller = _bound_controller(tmp_path)
    controller.begin(methods="sso", requires_user_presence=False)

    aborted = controller.abort_active()

    assert aborted is not None
    assert aborted.outcome == "aborted"
    assert aborted.resume_frame is None
    assert load_authentication_handoffs(tmp_path).intervals == (aborted,)


@pytest.mark.parametrize(
    "methods",
    [
        (),
        ("password_manager", "password_manager"),
        ({"provider": "vault"},),
        "1password",
        "password",
    ],
)
def test_method_contract_rejects_identity_or_secret_free_text(tmp_path, methods) -> None:
    controller = _bound_controller(tmp_path)

    with pytest.raises(ValueError, match="methods"):
        controller.begin(methods=methods, requires_user_presence=True)


def test_marker_loader_rejects_noncanonical_or_extra_data(tmp_path) -> None:
    _bound_controller(tmp_path)
    marker = tmp_path / AUTHENTICATION_HANDOFF_FILENAME
    payload = json.loads(marker.read_text())
    payload["account"] = "person@example.test"
    marker.write_text(json.dumps(payload))

    with pytest.raises(AuthenticationHandoffError, match="malformed"):
        load_authentication_handoffs(tmp_path)


def test_sealed_loader_binds_resume_proof_to_exact_retained_frame(tmp_path) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 2),
        action_ordinal=None,
    )
    (capture_dir / ARTIFACT_MANIFEST_FILENAME).unlink()
    (capture_dir / CAPTURE_TERMINAL_FILENAME).unlink()
    with CaptureSession.load(capture_dir) as capture:
        digests = []
        for screenshot in capture._recording.screenshots:
            with Image.open(io.BytesIO(screenshot.png_data)) as retained:
                retained.load()
                digests.append(frame_sha256(retained))
    handoff = AuthenticationHandoff(
        interval_id="00000000-0000-4000-8000-000000000001",
        methods=("password_manager",),
        requires_user_presence=True,
        saved_account_selected=True,
        started_at=11.5,
        entry_frame=FreshFrameProof(
            timestamp=11.0,
            source_ordinal=1,
            frame_sha256=digests[0],
            capture_source="desktop-screenshot",
        ),
        ended_at=12.5,
        outcome="completed",
        suppressed_sources=(
            "audio",
            "browser",
            "input",
            "screen",
            "structural",
            "window",
        ),
        resume_frame=FreshFrameProof(
            timestamp=12.0,
            source_ordinal=2,
            frame_sha256=digests[1],
            capture_source="desktop-screenshot",
        ),
    )
    _write_manifest(
        capture_dir / AUTHENTICATION_HANDOFF_FILENAME,
        AuthenticationHandoffManifest(
            schema_version="openadapt.capture.authentication-handoffs/v1",
            intervals=(handoff,),
        ),
    )
    seal_capture(
        capture_dir,
        session_id="authentication-test",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={
            "action": 0,
            "screen": 2,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=2,
    )

    with CaptureSession.load_verified(capture_dir) as capture:
        assert capture.authentication_handoffs == (handoff,)

    (capture_dir / ARTIFACT_MANIFEST_FILENAME).unlink()
    (capture_dir / CAPTURE_TERMINAL_FILENAME).unlink()
    wrong = handoff.model_copy(
        update={"resume_frame": handoff.resume_frame.model_copy(update={"source_ordinal": 1})}
    )
    _write_manifest(
        capture_dir / AUTHENTICATION_HANDOFF_FILENAME,
        AuthenticationHandoffManifest(
            schema_version="openadapt.capture.authentication-handoffs/v1",
            intervals=(wrong,),
        ),
    )
    seal_capture(
        capture_dir,
        session_id="authentication-test",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={
            "action": 0,
            "screen": 2,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=2,
    )
    with pytest.raises(InvalidCaptureEvent, match="resume proof"):
        CaptureSession.load_verified(capture_dir)


def test_sealed_loader_rejects_source_event_after_aborted_handoff(tmp_path) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 2),
        action_ordinal=None,
    )
    (capture_dir / ARTIFACT_MANIFEST_FILENAME).unlink()
    (capture_dir / CAPTURE_TERMINAL_FILENAME).unlink()
    with CaptureSession.load(capture_dir) as capture:
        first = capture._recording.screenshots[0]
        with Image.open(io.BytesIO(first.png_data)) as retained:
            retained.load()
            entry_digest = frame_sha256(retained)
    handoff = AuthenticationHandoff(
        interval_id="00000000-0000-4000-8000-000000000002",
        methods=("sso",),
        requires_user_presence=False,
        saved_account_selected=False,
        started_at=11.5,
        entry_frame=FreshFrameProof(
            timestamp=11.0,
            source_ordinal=1,
            frame_sha256=entry_digest,
            capture_source="desktop-screenshot",
        ),
        ended_at=12.5,
        outcome="aborted",
        suppressed_sources=(
            "audio",
            "browser",
            "input",
            "screen",
            "structural",
            "window",
        ),
    )
    _write_manifest(
        capture_dir / AUTHENTICATION_HANDOFF_FILENAME,
        AuthenticationHandoffManifest(
            schema_version="openadapt.capture.authentication-handoffs/v1",
            intervals=(handoff,),
        ),
    )
    seal_capture(
        capture_dir,
        session_id="authentication-abort-test",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={
            "action": 0,
            "screen": 2,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=2,
    )

    with pytest.raises(InvalidCaptureEvent, match="after an aborted"):
        CaptureSession.load_verified(capture_dir)


def test_screen_reader_captures_nothing_until_resume_frame(tmp_path, monkeypatch) -> None:
    timestamps = itertools.count(10)

    def timestamp() -> float:
        return float(next(timestamps))

    controller = AuthenticationHandoffController()
    controller.configure_entry_frame(True)
    controller.bind(tmp_path, timestamp)
    event_q = OrderedEventJournal()
    terminate = multiprocessing.Event()
    started = threading.Event()
    calls = 0

    def screenshot() -> Image.Image:
        nonlocal calls
        calls += 1
        return Image.new("RGB", (4, 4), (calls % 255, 0, 0))

    monkeypatch.setattr("openadapt_capture.recorder.utils.take_screenshot", screenshot)
    monkeypatch.setattr(
        "openadapt_capture.recorder.utils.get_timestamp",
        timestamp,
    )
    with config_override(RecordingConfig(screen_capture_fps=100)):
        reader = threading.Thread(
            target=read_screen_events,
            args=(event_q, terminate, object(), started),
            kwargs={"authentication": controller},
        )
        reader.start()
        assert started.wait(timeout=1)
        event_q.get(timeout=1)
        handle = controller.begin(
            methods="password_manager",
            requires_user_presence=True,
        )
        entry_events = []
        while True:
            try:
                entry_events.append(event_q.get_nowait())
            except queue.Empty:
                break
        assert entry_events
        entry_ordinal = entry_events[-1].source_ordinal
        protected_calls = calls
        time.sleep(0.05)
        assert calls == protected_calls

        result = []
        closer = threading.Thread(target=lambda: result.append(controller.end(handle, timeout=1)))
        closer.start()
        resume_event = event_q.get(timeout=1)
        closer.join(timeout=1)
        assert resume_event.type == "screen"
        assert entry_ordinal < resume_event.source_ordinal
        assert result[0].resume_frame.source_ordinal == resume_event.source_ordinal

        terminate.set()
        reader.join(timeout=1)
        assert not reader.is_alive()


def test_input_is_dropped_before_structural_observation(tmp_path, monkeypatch) -> None:
    controller = _bound_controller(tmp_path)
    controller.begin(methods="password_manager", requires_user_presence=True)
    terminate = threading.Event()
    event_q = OrderedEventJournal()
    observations = []

    class StructuralObserver:
        def observe(self, request):
            observations.append(request)
            return None

    class FakeObserver:
        def __init__(self, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback(
                ObservedMouseButton(
                    x=10,
                    y=20,
                    button="left",
                    pressed=True,
                    timestamp=5.0,
                )
            )
            terminate.set()

        def check_health(self) -> None:
            return

        def stop(self) -> None:
            return

    monkeypatch.setattr(
        recorder_module,
        "create_input_observer",
        lambda callback, **_kwargs: FakeObserver(callback),
    )
    recorder_module.read_input_events(
        event_q,
        terminate,
        object(),
        threading.Event(),
        structural_observer=StructuralObserver(),
        authentication=controller,
    )

    assert event_q.empty()
    assert observations == []
