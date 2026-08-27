"""Privacy, provenance, and package contracts for the public demo captures."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from openadapt_capture.capture import CaptureSession
from openadapt_capture.terminal import CaptureSealError, verify_capture_artifacts
from scripts.generate_synthetic_captures import (
    GENERATED_FILENAMES,
    REQUIRED_SQLALCHEMY_VERSION,
    REQUIRED_SQLITE_VERSION,
    Canvas,
    canonical_json_bytes,
    check_generated,
)
from scripts.migrate_legacy_capture import migrate
from scripts.verify_distribution import verify_distribution

REPOSITORY = Path(__file__).resolve().parents[1]
CAPTURE_ROOT = REPOSITORY / "examples" / "captures"
GENERATOR = REPOSITORY / "scripts" / "generate_synthetic_captures.py"
EXPECTED_COUNTS = {
    "demo_new": 14,
    "turn-off-nightshift": 20,
}
PRIVATE_SOURCE_TOKENS = (
    b"/users/",
    b".local",
    b"abrichr",
    b"macbook",
    b"richard",
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _png_chunk_names(value: bytes) -> list[bytes]:
    assert value.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    names = []
    while offset < len(value):
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        name = value[offset + 4 : offset + 8]
        names.append(name)
        offset += 12 + length
    assert offset == len(value)
    return names


@pytest.mark.parametrize("fixture_id,expected_count", EXPECTED_COUNTS.items())
def test_public_fixture_is_synthetic_sealed_and_exactly_bound(
    fixture_id: str,
    expected_count: int,
) -> None:
    capture_dir = CAPTURE_ROOT / fixture_id
    assert {path.name for path in capture_dir.iterdir()} == GENERATED_FILENAMES

    terminal, manifest = verify_capture_artifacts(capture_dir)
    assert terminal.event_counts.action == expected_count
    assert terminal.event_counts.screen == expected_count
    assert terminal.last_source_ordinal == expected_count * 2
    assert [artifact.path for artifact in manifest.artifacts] == [
        "capture-state.json",
        "recording.db",
        "synthetic-provenance.json",
    ]

    provenance_raw = (capture_dir / "synthetic-provenance.json").read_bytes()
    provenance = json.loads(provenance_raw)
    assert provenance_raw.endswith(b"\n")
    assert provenance["schema_version"] == (
        "openadapt.capture.synthetic-fixture-provenance/v2"
    )
    assert provenance["fixture_id"] == fixture_id
    assert provenance["synthetic"] is True
    assert provenance["contains_human_recording"] is False
    assert provenance["contains_personal_data"] is False
    assert provenance["qualification_eligible"] is False
    assert provenance["timing"]["kind"] == "declared_synthetic"
    assert provenance["source"]["sha256"] == _sha256(
        (REPOSITORY / provenance["source"]["path"]).read_bytes()
    )
    assert provenance["generator"]["sha256"] == _sha256(GENERATOR.read_bytes())
    assert provenance["builder"] == {
        "database_normalization": "vacuum_4096_writer_version_zero",
        "sqlalchemy_version": REQUIRED_SQLALCHEMY_VERSION,
        "sqlite_version": REQUIRED_SQLITE_VERSION,
    }

    with CaptureSession.load_verified(capture_dir) as capture:
        recording = capture._recording
        fixture = recording.config["fixture"]
        assert fixture["schema_version"] == "openadapt.capture.synthetic-fixture/v1"
        assert fixture["synthetic"] is True
        assert fixture["contains_human_recording"] is False
        assert fixture["contains_personal_data"] is False
        assert fixture["qualification_eligible"] is False
        assert fixture["provenance"] == provenance

        screenshots = list(recording.screenshots)
        actions = list(recording.action_events)
        assert len(screenshots) == expected_count
        assert len(actions) == expected_count
        assert sorted(
            [row.source_ordinal for row in screenshots]
            + [row.source_ordinal for row in actions]
        ) == list(range(1, expected_count * 2 + 1))
        assert [round(row.timestamp * 1_000_000) for row in screenshots] == provenance[
            "timing"
        ]["frame_timestamps_us"]
        assert all(row.png_sha256 == _sha256(row.png_data) for row in screenshots)
        assert all(
            _png_chunk_names(row.png_data) == [b"IHDR", b"IDAT", b"IEND"]
            for row in screenshots
        )

        action_bindings = [
            {
                "source_ordinal": row.source_ordinal,
                "timestamp_us": round(row.timestamp * 1_000_000),
                "before_frame_source_ordinal": row.screenshot_source_ordinal,
                "after_frame_source_ordinal": row.after_screenshot_source_ordinal,
                "label": row.active_segment_description,
            }
            for row in actions
        ]
        assert action_bindings == provenance["action_bindings"]
        assert all(row.screenshot_source_ordinal is not None for row in actions)
        assert all(row.after_screenshot_source_ordinal is not None for row in actions)

    for artifact in capture_dir.iterdir():
        lowered = artifact.read_bytes().lower()
        assert all(token not in lowered for token in PRIVATE_SOURCE_TOKENS)


def _is_the_reference_builder() -> bool:
    """Report whether this interpreter regenerates the committed bytes exactly.

    Python 3.12 with the required SQLite and SQLAlchemy is the one environment
    that `test_public_fixtures_match_the_source_generator_byte_for_byte` proves
    reproduces every committed byte. A tamper test is only meaningful there: on
    any other builder a raised `generated bytes are stale` could report the
    builder rather than the tamper.
    """
    import sqlalchemy

    return sys.version_info[:2] == (3, 12) and (
        sqlite3.sqlite_version,
        sqlalchemy.__version__,
    ) == (REQUIRED_SQLITE_VERSION, REQUIRED_SQLALCHEMY_VERSION)


REFERENCE_BUILDER_ONLY = pytest.mark.skipif(
    not _is_the_reference_builder(),
    reason="only the reference builder regenerates the committed bytes exactly",
)


def _copy_committed_fixtures(destination: Path) -> Path:
    for fixture_id in EXPECTED_COUNTS:
        shutil.copytree(CAPTURE_ROOT / fixture_id, destination / fixture_id)
    return destination


def test_public_fixtures_match_the_source_generator_byte_for_byte() -> None:
    import sqlalchemy

    if sys.version_info[:2] != (3, 12):
        actual = (sqlite3.sqlite_version, sqlalchemy.__version__)
        expected = (REQUIRED_SQLITE_VERSION, REQUIRED_SQLALCHEMY_VERSION)
        if actual != expected:
            with pytest.raises(RuntimeError, match="synthetic capture generation requires"):
                check_generated(
                    spec_root=CAPTURE_ROOT / "specs",
                    output_root=CAPTURE_ROOT,
                    generator_path=GENERATOR,
                )
        return
    check_generated(
        spec_root=CAPTURE_ROOT / "specs",
        output_root=CAPTURE_ROOT,
        generator_path=GENERATOR,
    )


@REFERENCE_BUILDER_ONLY
def test_a_changed_source_spec_makes_the_committed_fixtures_stale(
    tmp_path: Path,
) -> None:
    """A source change reaches the fixture bytes, and the byte check reports it."""
    spec_root = tmp_path / "specs"
    spec_root.mkdir()
    for spec_path in (CAPTURE_ROOT / "specs").glob("*.json"):
        spec = json.loads(spec_path.read_bytes())
        if spec_path.stem == "demo_new":
            spec["actions"][0]["x"] += 1
        (spec_root / spec_path.name).write_bytes(canonical_json_bytes(spec))

    with pytest.raises(RuntimeError, match="generated bytes are stale"):
        check_generated(
            spec_root=spec_root,
            output_root=CAPTURE_ROOT,
            generator_path=GENERATOR,
        )


@REFERENCE_BUILDER_ONLY
@pytest.mark.parametrize(
    "artifact_name",
    sorted(GENERATED_FILENAMES),
)
def test_every_committed_fixture_artifact_is_byte_checked(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    """One flipped byte in any generated artifact fails the byte check."""
    output_root = _copy_committed_fixtures(tmp_path / "captures")
    target = output_root / "demo_new" / artifact_name
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0xFF
    target.write_bytes(bytes(raw))

    with pytest.raises(RuntimeError, match="generated bytes are stale"):
        check_generated(
            spec_root=CAPTURE_ROOT / "specs",
            output_root=output_root,
            generator_path=GENERATOR,
        )


def test_reconstructed_legacy_fixture_is_marked_unsealed_and_ineligible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    destination = tmp_path / "migrated"
    screenshots = source / "screenshots"
    screenshots.mkdir(parents=True)
    database = sqlite3.connect(source / "capture.db")
    database.execute(
        """
        CREATE TABLE capture (
            started_at REAL,
            ended_at REAL,
            screen_width INTEGER,
            screen_height INTEGER,
            pixel_ratio REAL,
            platform TEXT,
            task_description TEXT,
            double_click_interval_seconds REAL,
            double_click_distance_pixels REAL,
            video_start_time REAL
        )
        """
    )
    database.execute(
        "INSERT INTO capture VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (100.0, 102.0, 8, 8, 1.0, "legacy", "legacy fixture", 0.5, 5.0, 100.0),
    )
    database.execute("CREATE TABLE events (timestamp REAL, type TEXT, data TEXT)")
    database.execute(
        "INSERT INTO events VALUES (?, ?, ?)",
        (100.5, "mouse.down", '{"button":"left","x":2,"y":3}'),
    )
    database.commit()
    database.close()
    png = Canvas(8, 8, (12, 34, 56)).png()
    (screenshots / "fixture_step_1.png").write_bytes(png)
    (screenshots / "fixture_step_2.png").write_bytes(png)

    migrate(source, destination)

    with CaptureSession.load(destination) as capture:
        fixture = capture._recording.config["fixture"]
        assert fixture["schema_version"] == (
            "openadapt.capture.reconstructed-legacy-fixture/v1"
        )
        assert fixture["reconstructed"] is True
        assert fixture["sealed"] is False
        assert fixture["qualification_eligible"] is False
        assert fixture["frame_timing"] == (
            "evenly_distributed_over_retained_recording_window"
        )
        assert fixture["action_timing"] == "retained_from_legacy_event_rows"
        assert fixture["source_capture_db_sha256"] == _sha256(
            (source / "capture.db").read_bytes()
        )
        assert [row.source_ordinal for row in capture._recording.screenshots] == [1, 3]
        assert [row.source_ordinal for row in capture._recording.action_events] == [2]

    with pytest.raises(CaptureSealError):
        CaptureSession.load_verified(destination)


def test_distribution_validator_rejects_repository_demo_captures(tmp_path: Path) -> None:
    wheel = tmp_path / "openadapt_capture-1.2.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("examples/captures/demo_new/recording.db", b"repository only")
        archive.writestr("openadapt_capture-1.2.2.dist-info/licenses/LICENSE", "MIT\n")
        archive.writestr(
            "openadapt_capture-1.2.2.dist-info/METADATA",
            "Name: openadapt-capture\n",
        )

    with pytest.raises(AssertionError, match="repository-only path"):
        verify_distribution(wheel)
