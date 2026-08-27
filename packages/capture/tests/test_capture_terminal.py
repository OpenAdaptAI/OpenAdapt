"""Immutable capture terminal and verified-consumer snapshot tests."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

import openadapt_capture.terminal as terminal_module
from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.db.models import ActionEvent, Recording, Screenshot, WindowEvent
from openadapt_capture.events import window_geometry_epoch_sha256
from openadapt_capture.terminal import (
    ARTIFACT_MANIFEST_FILENAME,
    CAPTURE_TERMINAL_FILENAME,
    CaptureSealError,
    seal_capture,
    verify_capture_artifacts,
)


def _capture_directory(root: Path) -> Path:
    capture_dir = root / "capture"
    capture_dir.mkdir(parents=True)
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    crud.insert_recording(
        session,
        {
            "timestamp": 10.0,
            "monitor_width": 800,
            "monitor_height": 600,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "test",
            "task_description": "sealed capture",
        },
    )
    session.close()
    engine.dispose()
    (capture_dir / "artifact.bin").write_bytes(b"artifact")
    (capture_dir / "capture-state.json").write_text('{"phase":"finalizing"}\n')
    return capture_dir


def _seal(capture_dir: Path):
    return seal_capture(
        capture_dir,
        session_id="session-1",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=12.0,
        event_counts={
            "action": 0,
            "screen": 0,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=None,
    )


def _v2_capture_directory(
    root: Path,
    *,
    frame_ordinals: tuple[int, ...] = (1, 3),
    action_ordinal: int | None = 2,
    before_binding_ordinal: int | None = None,
    after_binding_ordinal: int | None = None,
    wrong_png_digest: bool = False,
    video: bool = False,
) -> Path:
    """Build one sealed v2 capture for artifact-contract regressions."""
    capture_dir = root / "capture"
    capture_dir.mkdir(parents=True)
    state = {
        "schema_version": "openadapt.capture.window-scoped/v2",
        "window_capture": True,
        "window_id": "42",
        "owner": "FixtureApp",
        "pid": 4242,
        "process_start_time": 9.0,
        "coordinate_source": "test-screen-points",
        "geometry_generation": 1,
        "display_topology_sha256": "a" * 64,
        "bounds": [10.0, 20.0, 80.0, 60.0],
        "scale": 1.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "viewport": [80, 60],
        "source_viewport": [80, 60],
        "content_rect": [0, 0, 80, 60],
        "fit_scale": 1.0,
        "on_screen": True,
    }
    state["geometry_epoch_sha256"] = window_geometry_epoch_sha256(state)
    config = {
        "capture_window": {
            **state,
            "target": {"owner": "FixtureApp", "title": None},
            "title": "Fixture Window",
            "initial_bounds": state["bounds"],
            "coordinate_space": "window_pixels",
        }
    }

    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    try:
        recording = Recording(
            timestamp=10.0,
            monitor_width=80,
            monitor_height=60,
            platform="linux",
            task_description="sealed v2 capture",
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            config=config,
        )
        session.add(recording)
        session.flush()
        frames: dict[int, tuple[Screenshot, WindowEvent]] = {}
        for index, ordinal in enumerate(frame_ordinals):
            timestamp = 11.0 + index
            output = io.BytesIO()
            Image.new("RGB", (80, 60), (20 + index, 40, 60)).save(
                output,
                format="PNG",
            )
            png = output.getvalue()
            screenshot = Screenshot(
                recording_id=recording.id,
                recording_timestamp=10.0,
                timestamp=timestamp,
                source_ordinal=ordinal,
                png_data=png,
                png_sha256=("f" * 64 if wrong_png_digest else hashlib.sha256(png).hexdigest()),
            )
            window = WindowEvent(
                recording_id=recording.id,
                recording_timestamp=10.0,
                timestamp=timestamp,
                source_ordinal=ordinal,
                title="Fixture Window",
                left=10,
                top=20,
                width=80,
                height=60,
                window_id="42",
                state=state,
            )
            session.add_all((screenshot, window))
            frames[ordinal] = (screenshot, window)
        if action_ordinal is not None:
            before_ordinal = before_binding_ordinal or frame_ordinals[0]
            before, window = frames[before_ordinal]
            after_ordinal = after_binding_ordinal or frame_ordinals[-1]
            after, after_window = frames[after_ordinal]
            has_after = after_ordinal > action_ordinal
            session.add(
                ActionEvent(
                    recording_id=recording.id,
                    recording_timestamp=10.0,
                    timestamp=11.5,
                    source_ordinal=action_ordinal,
                    name="click",
                    mouse_x=20.0,
                    mouse_y=20.0,
                    mouse_button_name="left",
                    mouse_pressed=False,
                    screenshot=before,
                    screenshot_timestamp=before.timestamp,
                    screenshot_source_ordinal=before_ordinal,
                    after_screenshot_timestamp=after.timestamp if has_after else None,
                    after_screenshot_source_ordinal=after_ordinal if has_after else None,
                    window_event=window,
                    window_event_timestamp=window.timestamp,
                    window_event_source_ordinal=before_ordinal,
                    after_window_event_timestamp=(
                        after_window.timestamp if has_after else None
                    ),
                    after_window_event_source_ordinal=(
                        after_ordinal if has_after else None
                    ),
                    window_geometry_generation=1,
                    after_window_geometry_generation=1 if has_after else None,
                )
            )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    if video:
        (capture_dir / "video.mp4").write_bytes(b"fixture")
    seal_capture(
        capture_dir,
        session_id="v2-session",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={
            "action": int(action_ordinal is not None),
            "screen": len(frame_ordinals),
            "window": len(frame_ordinals),
            "browser": 0,
            "video": len(frame_ordinals) if video else 0,
        },
        last_source_ordinal=max((*frame_ordinals, action_ordinal or 0)),
    )
    return capture_dir


def _desktop_capture_directory(
    root: Path,
    *,
    frame_ordinals: tuple[int, ...] = (1, 3),
    window_ordinals: tuple[int, ...] = (),
    action_ordinal: int | None = 2,
    before_binding_ordinal: int | None = None,
    after_binding_ordinal: int | None = None,
    wrong_png_digest: bool = False,
    retain_png: bool = True,
) -> Path:
    """Build one sealed virtual-desktop capture for contract regressions."""
    capture_dir = root / "capture"
    capture_dir.mkdir(parents=True)
    config = {
        "capture_desktop": {
            "schema_version": "openadapt.capture.display-topology/v1",
            "coordinate_space": "virtual_desktop_pixels",
            "origin": [0, 0],
            "viewport": [80, 60],
            "monitor_count": 1,
            "monitors": [[0, 0, 80, 60]],
            "topology_sha256": "a" * 64,
        }
    }

    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    try:
        recording = Recording(
            timestamp=10.0,
            monitor_width=80,
            monitor_height=60,
            platform="linux",
            task_description="sealed desktop capture",
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            config=config,
        )
        session.add(recording)
        session.flush()
        frames: dict[int, Screenshot] = {}
        for index, ordinal in enumerate(frame_ordinals):
            timestamp = 11.0 + index
            output = io.BytesIO()
            Image.new("RGB", (80, 60), (20 + index, 40, 60)).save(
                output,
                format="PNG",
            )
            png = output.getvalue()
            screenshot = Screenshot(
                recording_id=recording.id,
                recording_timestamp=10.0,
                timestamp=timestamp,
                source_ordinal=ordinal,
                png_data=png if retain_png else None,
                png_sha256=(
                    ("f" * 64 if wrong_png_digest else hashlib.sha256(png).hexdigest())
                    if retain_png
                    else None
                ),
            )
            session.add(screenshot)
            frames[ordinal] = screenshot
        for ordinal in window_ordinals:
            session.add(
                WindowEvent(
                    recording_id=recording.id,
                    recording_timestamp=10.0,
                    timestamp=10.0 + ordinal,
                    source_ordinal=ordinal,
                    title="Fixture Window",
                    left=0,
                    top=0,
                    width=80,
                    height=60,
                    window_id="fixture-window",
                    state={},
                )
            )
        if action_ordinal is not None:
            before_ordinal = before_binding_ordinal or frame_ordinals[0]
            before = frames[before_ordinal]
            after_ordinal = after_binding_ordinal or frame_ordinals[-1]
            after = frames[after_ordinal]
            has_after = after_ordinal > action_ordinal
            session.add(
                ActionEvent(
                    recording_id=recording.id,
                    recording_timestamp=10.0,
                    timestamp=11.5,
                    source_ordinal=action_ordinal,
                    name="click",
                    mouse_x=20.0,
                    mouse_y=20.0,
                    mouse_button_name="left",
                    mouse_pressed=False,
                    screenshot=before,
                    screenshot_timestamp=before.timestamp,
                    screenshot_source_ordinal=before_ordinal,
                    after_screenshot_timestamp=after.timestamp if has_after else None,
                    after_screenshot_source_ordinal=after_ordinal if has_after else None,
                )
            )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    seal_capture(
        capture_dir,
        session_id="desktop-session",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={
            "action": int(action_ordinal is not None),
            "screen": len(frame_ordinals),
            "window": len(window_ordinals),
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=max(
            (*frame_ordinals, *window_ordinals, action_ordinal or 0)
        )
        or None,
    )
    return capture_dir


def test_terminal_binds_canonical_manifest_bytes_including_newline(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    terminal = _seal(capture_dir)
    manifest_raw = (capture_dir / ARTIFACT_MANIFEST_FILENAME).read_bytes()

    assert manifest_raw.endswith(b"\n")
    assert terminal.artifact_manifest_size_bytes == len(manifest_raw)
    assert (
        terminal.artifact_manifest_sha256
        == hashlib.sha256(b"openadapt.capture-artifact-manifest.v1\0" + manifest_raw).hexdigest()
    )
    assert (capture_dir / CAPTURE_TERMINAL_FILENAME).read_bytes().endswith(b"\n")
    verified_terminal, manifest = verify_capture_artifacts(capture_dir)
    assert verified_terminal == terminal
    assert [artifact.path for artifact in manifest.artifacts] == [
        "artifact.bin",
        "capture-state.json",
        "recording.db",
    ]


def test_control_state_is_part_of_the_immutable_inventory(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    _seal(capture_dir)

    (capture_dir / "capture-state.json").write_text('{"phase":"complete"}\n')

    with pytest.raises(CaptureSealError, match="differs from its seal"):
        verify_capture_artifacts(capture_dir)


def test_terminal_rejects_artifact_tamper_and_uninventoried_files(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    _seal(capture_dir)
    (capture_dir / "artifact.bin").write_bytes(b"changed")
    with pytest.raises(CaptureSealError, match="differs from its seal"):
        verify_capture_artifacts(capture_dir)

    other = _capture_directory(tmp_path / "other")
    _seal(other)
    (other / "late.txt").write_text("late")
    with pytest.raises(CaptureSealError, match="sealed inventory"):
        verify_capture_artifacts(other)


def test_seal_does_not_return_complete_after_an_artifact_changes_during_inventory(
    tmp_path,
    monkeypatch,
) -> None:
    capture_dir = _capture_directory(tmp_path)
    original_hash = terminal_module._hash_relative_regular_file
    mutated = False

    def mutate_previous_artifact_then_hash(root, relative_path):
        nonlocal mutated
        if relative_path == "recording.db" and not mutated:
            mutated = True
            (capture_dir / "artifact.bin").write_bytes(b"changed after inventory")
        return original_hash(root, relative_path)

    monkeypatch.setattr(
        terminal_module,
        "_hash_relative_regular_file",
        mutate_previous_artifact_then_hash,
    )

    with pytest.raises(CaptureSealError, match="differs from its seal"):
        _seal(capture_dir)


def test_manifest_rejects_symbolic_links(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    (capture_dir / "linked.bin").symlink_to(capture_dir / "artifact.bin")

    with pytest.raises(CaptureSealError, match="not a regular file"):
        _seal(capture_dir)


def test_verifier_rejects_an_intermediate_directory_replaced_during_read(
    tmp_path,
    monkeypatch,
) -> None:
    capture_dir = _capture_directory(tmp_path)
    nested = capture_dir / "nested"
    nested.mkdir()
    (nested / "evidence.bin").write_bytes(b"evidence")
    _seal(capture_dir)
    moved = tmp_path / "moved-nested"
    original_hash = terminal_module._hash_relative_regular_file
    replaced = False

    def replace_then_hash(root, relative_path):
        nonlocal replaced
        if relative_path == "nested/evidence.bin" and not replaced:
            replaced = True
            nested.rename(moved)
            nested.symlink_to(moved, target_is_directory=True)
        return original_hash(root, relative_path)

    monkeypatch.setattr(
        terminal_module,
        "_hash_relative_regular_file",
        replace_then_hash,
    )

    with pytest.raises(CaptureSealError):
        verify_capture_artifacts(capture_dir)


def test_verified_loader_uses_a_private_snapshot_without_migrating_source(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    terminal = _seal(capture_dir)
    source_db = capture_dir / "recording.db"
    before = (source_db.stat().st_mtime_ns, hashlib.sha256(source_db.read_bytes()).hexdigest())

    with CaptureSession.load_verified(capture_dir) as capture:
        assert capture.task_description == "sealed capture"
        assert capture.capture_dir != capture_dir
        assert capture.capture_dir.parent != capture_dir.parent
        assert (
            json.loads((capture.capture_dir / CAPTURE_TERMINAL_FILENAME).read_text())[
                "terminal_sha256"
            ]
            == terminal.terminal_sha256
        )

    after = (source_db.stat().st_mtime_ns, hashlib.sha256(source_db.read_bytes()).hexdigest())
    assert after == before


def test_sealed_semantic_validation_copies_only_the_database(
    tmp_path,
    monkeypatch,
) -> None:
    capture_dir = _capture_directory(tmp_path)
    _seal(capture_dir)
    copied: list[str] = []
    original_copy = terminal_module._copy_verified_regular_file

    def track_copy(source_root, relative_path, destination, expected):
        copied.append(relative_path)
        return original_copy(source_root, relative_path, destination, expected)

    monkeypatch.setattr(
        terminal_module,
        "_copy_verified_regular_file",
        track_copy,
    )

    CaptureSession.validate_sealed(capture_dir)

    assert copied == ["recording.db"]


def test_verified_loader_rejects_terminal_counts_that_differ_from_database(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    seal_capture(
        capture_dir,
        session_id="session-1",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=12.0,
        event_counts={
            "action": 1,
            "screen": 0,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=None,
    )

    with pytest.raises(ValueError, match="action count"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_multiple_recordings(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    crud.insert_recording(
        session,
        {
            "timestamp": 20.0,
            "monitor_width": 800,
            "monitor_height": 600,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "test",
            "task_description": "second recording",
        },
    )
    session.close()
    engine.dispose()
    _seal(capture_dir)

    with pytest.raises(ValueError, match="exactly one recording"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_duplicate_source_ordinals(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path)
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    recording = session.query(crud.Recording).one()
    for timestamp in (11.0, 12.0):
        crud.insert_screenshot(
            session,
            recording,
            timestamp,
            {"source_ordinal": 1, "png_sha256": None},
        )
    session.close()
    engine.dispose()
    seal_capture(
        capture_dir,
        session_id="session-1",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=12.0,
        event_counts={
            "action": 0,
            "screen": 2,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=1,
    )

    with pytest.raises(ValueError, match="reuse a source ordinal"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_requires_a_v2_after_frame_for_every_action(tmp_path) -> None:
    capture_dir = _v2_capture_directory(tmp_path, frame_ordinals=(1,))

    with pytest.raises(ValueError, match="incomplete before/after frame binding"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_accepts_a_complete_v2_source_journal(tmp_path) -> None:
    capture_dir = _v2_capture_directory(tmp_path)

    with CaptureSession.load_verified(capture_dir) as capture:
        assert [frame.source_ordinal for frame in capture.frames()] == [1, 3]


def test_verified_loader_rejects_a_skipped_nearest_before_frame(tmp_path) -> None:
    capture_dir = _v2_capture_directory(
        tmp_path,
        frame_ordinals=(1, 2, 4),
        action_ordinal=3,
        before_binding_ordinal=1,
        after_binding_ordinal=4,
    )

    with pytest.raises(ValueError, match="nearest retained before frame"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_a_skipped_first_after_frame(tmp_path) -> None:
    capture_dir = _v2_capture_directory(
        tmp_path,
        frame_ordinals=(1, 3, 4),
        action_ordinal=2,
        before_binding_ordinal=1,
        after_binding_ordinal=4,
    )

    with pytest.raises(ValueError, match="first retained after frame"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_a_gap_in_the_v2_source_journal(tmp_path) -> None:
    capture_dir = _v2_capture_directory(
        tmp_path,
        frame_ordinals=(1, 3),
        action_ordinal=None,
    )

    with pytest.raises(ValueError, match="source journal has a missing ordinal"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_requires_a_desktop_after_frame_for_every_action(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(tmp_path, frame_ordinals=(1,))

    with pytest.raises(ValueError, match="incomplete before/after frame binding"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_accepts_a_complete_desktop_source_journal(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 2, 4),
        action_ordinal=3,
        before_binding_ordinal=2,
    )

    with CaptureSession.load_verified(capture_dir) as capture:
        assert [frame.source_ordinal for frame in capture.frames()] == [1, 2, 4]
        action = capture.raw_events()[0]
        assert action.source_ordinal == 3
        assert action.screenshot_source_ordinal == 2
        assert action.after_screenshot_source_ordinal == 4


def test_verified_loader_accepts_desktop_window_evidence_without_an_action(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 3),
        window_ordinals=(2,),
        action_ordinal=None,
    )

    with CaptureSession.load_verified(capture_dir) as capture:
        assert [frame.source_ordinal for frame in capture.frames()] == [1, 3]
        assert [event.source_ordinal for event in capture.window_events()] == [2]


def test_verified_loader_rejects_a_skipped_desktop_nearest_before_frame(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 2, 4),
        action_ordinal=3,
        before_binding_ordinal=1,
        after_binding_ordinal=4,
    )

    with pytest.raises(ValueError, match="nearest retained before frame"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_a_skipped_desktop_first_after_frame(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(1, 3, 4),
        action_ordinal=2,
        before_binding_ordinal=1,
        after_binding_ordinal=4,
    )

    with pytest.raises(ValueError, match="first retained after frame"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_recomputes_desktop_png_digest(tmp_path) -> None:
    capture_dir = _desktop_capture_directory(tmp_path, wrong_png_digest=True)

    with pytest.raises(ValueError, match="PNG digest differs"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_a_desktop_capture_without_a_retained_frame(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(
        tmp_path,
        frame_ordinals=(),
        action_ordinal=None,
    )

    with pytest.raises(ValueError, match="no retained frames"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_a_desktop_frame_without_a_pixel_carrier(
    tmp_path,
) -> None:
    capture_dir = _desktop_capture_directory(tmp_path, retain_png=False)

    with pytest.raises(ValueError, match="no retained PNG or exact MP4 carrier"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_recomputes_retained_v2_png_digest(tmp_path) -> None:
    capture_dir = _v2_capture_directory(tmp_path, wrong_png_digest=True)

    with pytest.raises(ValueError, match="PNG digest differs"):
        CaptureSession.load_verified(capture_dir)


@pytest.mark.parametrize(
    "timing",
    [
        (
            None,
            [(0, 0.0), (1, 1.0)],
            [(0, 11.0), (1, 12.0)],
            [(0, 1), (1, 4)],
        ),
        (
            None,
            [(0, 0.0), (1, 1.0)],
            [(0, 11.0), (1, 99.0)],
            [(0, 1), (1, 3)],
        ),
    ],
)
def test_verified_loader_joins_v2_mp4_bindings_to_database_frames(
    tmp_path,
    monkeypatch,
    timing,
) -> None:
    capture_dir = _v2_capture_directory(tmp_path, video=True)
    monkeypatch.setattr("openadapt_capture.video._read_timing_metadata", lambda _path: timing)

    with pytest.raises(ValueError, match="MP4 source bindings|capture-time bindings"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_accepts_v2_mp4_bindings_joined_to_database_frames(
    tmp_path,
    monkeypatch,
) -> None:
    capture_dir = _v2_capture_directory(tmp_path, video=True)
    timing = (
        None,
        [(0, 0.0), (1, 1.0)],
        [(0, 11.0), (1, 12.0)],
        [(0, 1), (1, 3)],
    )
    monkeypatch.setattr("openadapt_capture.video._read_timing_metadata", lambda _path: timing)

    with CaptureSession.load_verified(capture_dir) as capture:
        assert capture.video_path is not None


def test_verified_loader_rejects_an_extra_v2_mp4_capture_binding(
    tmp_path,
    monkeypatch,
) -> None:
    capture_dir = _v2_capture_directory(tmp_path, video=True)
    timing = (
        None,
        [(0, 0.0), (1, 1.0), (2, 2.0)],
        [(0, 11.0), (1, 12.0), (2, 13.0)],
        [(0, 1), (1, 3)],
    )
    monkeypatch.setattr("openadapt_capture.video._read_timing_metadata", lambda _path: timing)

    with pytest.raises(ValueError, match="capture-time bindings differ"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_rejects_multiple_recognized_video_artifacts(tmp_path) -> None:
    capture_dir = _v2_capture_directory(tmp_path, video=True)
    # Add the second recognized name before replacing the immutable seal.
    (capture_dir / CAPTURE_TERMINAL_FILENAME).unlink()
    (capture_dir / ARTIFACT_MANIFEST_FILENAME).unlink()
    (capture_dir / "oa_recording-10.mp4").write_bytes(b"fixture")
    seal_capture(
        capture_dir,
        session_id="v2-session",
        process_started_at=9.0,
        capture_started_at=10.0,
        capture_ended_at=14.0,
        event_counts={"action": 1, "screen": 2, "window": 2, "browser": 0, "video": 2},
        last_source_ordinal=3,
    )

    with pytest.raises(ValueError, match="multiple recognized MP4"):
        CaptureSession.load_verified(capture_dir)


def test_verified_loader_opens_an_encoded_immutable_database_uri(tmp_path) -> None:
    capture_dir = _capture_directory(tmp_path / "capture#fragment")
    _seal(capture_dir)

    with CaptureSession.load_verified(capture_dir) as capture:
        assert capture.task_description == "sealed capture"
