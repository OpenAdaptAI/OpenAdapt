"""The scanner must find and read what the current recorder writes.

The scanner globbed ``capture.db`` and queried a ``capture`` table and an
``events`` table. openadapt-capture PR #28 replaced that bespoke format on
2026-07-17 with a SQLAlchemy database named ``recording.db`` whose tables are
``recording``, ``action_event`` and ``screenshot``. Nothing a user records
today matched the glob, so ``openadapt-viewer catalog scan`` found zero
recordings on a machine full of them.

Renaming the glob alone would not have fixed it. The two formats share no
table and no column, so a path-only change registers a directory with no date,
no duration and no counts. These tests therefore assert the values, not the
count of rows returned.

They run against the recordings openadapt-capture commits under
``examples/captures/``, not against a database written here to match this
module's own assumptions. A test that builds its own input can only prove the
reader agrees with the test author. ``tests/capture_examples.py`` locates them.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from openadapt_viewer.catalog import RecordingCatalog
from openadapt_viewer.cli import main
from openadapt_viewer.scanner import RecordingScanner

from .capture_examples import (
    EXAMPLES_ENV,
    REAL_RECORDINGS,
    examples_dir,
    require_examples,
    write_legacy_capture,
)


@pytest.fixture
def scanner(tmp_path):
    return RecordingScanner(RecordingCatalog(db_path=str(tmp_path / "catalog.db")))


class TestCommittedRecordingsAreFound:
    """The defect, stated as the user sees it: scan finds nothing."""

    def test_scan_registers_every_committed_recording(self, scanner):
        examples = require_examples()

        found = scanner.scan_recording_directory(str(examples))

        assert sorted(recording.id for recording in found) == sorted(REAL_RECORDINGS)

    def test_scanned_recordings_reach_the_catalog(self, scanner):
        examples = require_examples()

        scanner.scan_recording_directory(str(examples))

        listed = scanner.catalog.get_all_recordings()
        assert sorted(recording.id for recording in listed) == sorted(REAL_RECORDINGS)

    def test_recursive_scan_finds_a_nested_recording(self, scanner, tmp_path):
        examples = require_examples()
        nested = tmp_path / "runs" / "monday" / "demo_new"
        nested.mkdir(parents=True)
        (nested / "recording.db").write_bytes(
            (examples / "demo_new" / "recording.db").read_bytes()
        )

        assert scanner.scan_recording_directory(str(tmp_path)) == []

        found = scanner.scan_recording_directory(str(tmp_path), recursive=True)
        assert [recording.id for recording in found] == ["demo_new"]


@pytest.mark.parametrize("name", REAL_RECORDINGS)
class TestCommittedRecordingsAreRead:
    """A found recording must carry the file's values, not fallbacks.

    Every expectation below is queried from the same recording.db the scanner
    read, with the query written out here rather than shared with the scanner.
    The subject is whether the catalog reports what the file says.
    """

    def test_created_at_is_the_recording_timestamp(self, scanner, name):
        directory = require_examples() / name
        expected = _scalar(directory, "SELECT timestamp FROM recording ORDER BY id LIMIT 1")

        recording = scanner._extract_recording_info(directory, name)

        assert recording.created_at == pytest.approx(expected)
        # The old reader fell back to the directory's mtime whenever the query
        # failed, which is a plausible-looking date that means nothing.
        assert recording.created_at != directory.stat().st_mtime

    def test_event_count_is_the_action_event_rows(self, scanner, name):
        directory = require_examples() / name
        expected = _scalar(directory, "SELECT COUNT(*) FROM action_event")

        recording = scanner._extract_recording_info(directory, name)

        assert expected > 0
        assert recording.event_count == expected

    def test_frame_count_is_the_screenshot_rows(self, scanner, name):
        directory = require_examples() / name
        expected = _scalar(directory, "SELECT COUNT(*) FROM screenshot")

        recording = scanner._extract_recording_info(directory, name)

        # Frames are blobs in the database now. The old reader counted PNG
        # files under screenshots/, a directory these recordings do not have,
        # and so reported None.
        assert expected > 0
        assert not (directory / "screenshots").exists()
        assert recording.frame_count == expected

    def test_duration_spans_start_to_last_observation(self, scanner, name):
        directory = require_examples() / name
        start = _scalar(directory, "SELECT timestamp FROM recording ORDER BY id LIMIT 1")
        last = max(
            _scalar(directory, "SELECT MAX(timestamp) FROM screenshot"),
            _scalar(directory, "SELECT MAX(timestamp) FROM action_event"),
        )

        recording = scanner._extract_recording_info(directory, name)

        # There is no ended_at column, so the duration is derived.
        assert recording.duration_seconds == pytest.approx(last - start)
        assert recording.duration_seconds > 0

    def test_task_description_and_display_metadata_are_read(self, scanner, name):
        directory = require_examples() / name
        row = _row(directory, "SELECT * FROM recording ORDER BY id LIMIT 1")

        recording = scanner._extract_recording_info(directory, name)

        assert recording.task_description == row["task_description"]
        assert recording.task_description
        assert recording.metadata == {
            "platform": row["platform"],
            "screen_width": row["monitor_width"],
            "screen_height": row["monitor_height"],
            "pixel_ratio": row["pixel_ratio"],
        }
        assert recording.metadata["screen_width"] > 0


class TestLegacyCapturesAreReportedNotIndexed:
    """A capture.db cannot be read, so say so instead of indexing a shell."""

    def test_scan_skips_a_legacy_directory_and_names_the_migration(
        self, scanner, tmp_path, capsys
    ):
        write_legacy_capture(tmp_path / "old-recording")

        found = scanner.scan_recording_directory(str(tmp_path))

        assert found == []
        out = capsys.readouterr().out
        assert "old-recording" in out
        assert "capture.db" in out
        assert "migrate_legacy_capture.py" in out

    def test_extract_refuses_a_legacy_directory(self, scanner, tmp_path):
        directory = write_legacy_capture(tmp_path / "old-recording")

        with pytest.raises(FileNotFoundError, match="migrate_legacy_capture.py"):
            scanner._extract_recording_info(directory, "old-recording")

    def test_a_converted_directory_is_indexed_without_a_legacy_warning(
        self, scanner, tmp_path, capsys
    ):
        examples = require_examples()
        directory = write_legacy_capture(tmp_path / "demo_new")
        (directory / "recording.db").write_bytes(
            (examples / "demo_new" / "recording.db").read_bytes()
        )

        found = scanner.scan_recording_directory(str(tmp_path))

        assert [recording.id for recording in found] == ["demo_new"]
        assert "migrate_legacy_capture.py" not in capsys.readouterr().out

    def test_extract_refuses_a_directory_holding_no_database(self, scanner, tmp_path):
        empty = tmp_path / "not-a-recording"
        empty.mkdir()

        with pytest.raises(FileNotFoundError, match="recording.db"):
            scanner._extract_recording_info(empty, "not-a-recording")


class TestCorruptRecordingDatabase:
    """An unreadable database degrades to a named entry, it does not abort."""

    def test_scan_continues_past_a_corrupt_database(self, scanner, tmp_path, capsys):
        examples = require_examples()
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "recording.db").write_text("this is not a database")
        good = tmp_path / "demo_new"
        good.mkdir()
        (good / "recording.db").write_bytes(
            (examples / "demo_new" / "recording.db").read_bytes()
        )

        found = scanner.scan_recording_directory(str(tmp_path))

        assert "demo_new" in [recording.id for recording in found]
        assert "Warning" in capsys.readouterr().out


class TestCatalogRegisterCommand:
    """`openadapt-viewer catalog register` is the manual path around scan."""

    def test_register_indexes_a_real_recording(self, tmp_path, monkeypatch, capsys):
        directory = require_examples() / "demo_new"
        _isolate_catalog_home(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(
            sys, "argv", ["openadapt-viewer", "catalog", "register", str(directory)]
        )

        main()

        out = capsys.readouterr().out
        assert "Successfully registered" in out
        # Recording.model_dump() names the field `id` and register_recording
        # takes `recording_id`, so splatting it raised an uncaught TypeError.
        assert "Frames: 14" in out
        assert "Events: 14" in out

    def test_register_rejects_a_legacy_directory(self, tmp_path, monkeypatch, capsys):
        directory = write_legacy_capture(tmp_path / "captures" / "old-recording")
        _isolate_catalog_home(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(
            sys, "argv", ["openadapt-viewer", "catalog", "register", str(directory)]
        )

        with pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        assert "migrate_legacy_capture.py" in capsys.readouterr().err


def test_committed_recordings_must_be_present_in_ci():
    """CI must not go green on a suite that skipped every real recording."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Only enforced on CI, which checks openadapt-capture out")

    assert examples_dir() is not None, (
        f"CI must set ${EXAMPLES_ENV}. Without it every test in this module "
        "skips and the scanner is verified against nothing."
    )


def _isolate_catalog_home(monkeypatch, home: Path) -> None:
    """Point the default catalog (~/.openadapt/catalog.db) at a temp home."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _connect(directory: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(directory / "recording.db")
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(directory: Path, query: str):
    with _connect(directory) as conn:
        return conn.execute(query).fetchone()[0]


def _row(directory: Path, query: str) -> sqlite3.Row:
    with _connect(directory) as conn:
        return conn.execute(query).fetchone()
