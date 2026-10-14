"""The benchmark loader must read what the current recorder writes.

``load_real_capture_data`` opened ``capture.db`` and selected from a ``capture``
table. openadapt-capture PR #28 replaced that format on 2026-07-17 with
``recording.db``, whose ``recording`` table shares no column with it, so the
loader raised ``FileNotFoundError`` on every recording made since. These tests
load the two recordings openadapt-capture commits under ``examples/captures``
and assert the values the loader reports, queried here from the same file.

What these tests do NOT cover, stated plainly so nobody reads more into a green
run than it earns: the episodes half. ``load_real_capture_data`` also requires
an ``episodes.json`` beside the recording, which is openadapt-ml segmentation
output, and neither committed recording carries one. The file used below is
``test_episodes.json``, a fixture committed to this repository. It is not
openadapt-ml output: that pipeline serialises ``EpisodeExtractionResult``, whose
``steps`` are objects rather than strings, whose ``episode_id`` is a UUID, and
which has no ``screenshots`` key at all. So every assertion here about tasks and
steps proves only that the fixture reaches the run unchanged. Every assertion
about timing, geometry and counts is checked against ``recording.db``, and that
is the half these tests are for.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from openadapt_viewer.recording_db import (
    LEGACY_DB_NAME,
    RECORDING_DB_NAME,
    LegacyCaptureError,
)
from openadapt_viewer.viewers.benchmark.real_data_loader import load_real_capture_data

from .capture_examples import REAL_RECORDINGS, REPO_ROOT, require_examples

#: A segmentation fixture committed to this repository. See the module
#: docstring: it is not openadapt-ml output, and nothing here treats it as such.
EPISODES_FIXTURE = REPO_ROOT / "test_episodes.json"


@pytest.fixture
def loadable_recording(tmp_path, request):
    """Copy one committed recording somewhere an episodes.json can sit beside it.

    The recording is copied rather than read in place because the loader wants
    both files in one directory, and openadapt-capture's checkout is not this
    test's to write into.
    """
    name = request.param
    source = require_examples() / name
    directory = tmp_path / name
    directory.mkdir()
    shutil.copy(source / RECORDING_DB_NAME, directory / RECORDING_DB_NAME)
    shutil.copy(EPISODES_FIXTURE, directory / "episodes.json")
    return directory


def _legacy_capture(directory: Path) -> Path:
    """Write a pre-#28 capture directory, in the format this viewer refuses.

    The shape is the one openadapt-capture's ``migrate_legacy_capture.py``
    reads: one ``capture`` row and a generic ``events`` table.

    Args:
        directory: Where to write it.

    Returns:
        The same directory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / LEGACY_DB_NAME) as conn:
        conn.execute(
            "CREATE TABLE capture (id INTEGER PRIMARY KEY, started_at REAL, "
            "ended_at REAL, platform TEXT, screen_width INTEGER, "
            "screen_height INTEGER, pixel_ratio REAL, task_description TEXT)"
        )
        conn.execute(
            "INSERT INTO capture VALUES (1, 1000.0, 1012.0, 'darwin', 1920, 1080, 2.0, 'old')"
        )
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp REAL, "
            "type TEXT, data TEXT, parent_id INTEGER)"
        )
        conn.execute("INSERT INTO events VALUES (1, 1000.5, 'mouse.move', '{}', NULL)")
    (directory / "episodes.json").write_text(EPISODES_FIXTURE.read_text())
    return directory


@pytest.mark.parametrize("loadable_recording", REAL_RECORDINGS, indirect=True)
class TestCommittedRecordingsLoad:
    """The defect as a user meets it: every current recording fails to load."""

    def test_a_current_recording_loads(self, loadable_recording):
        run = load_real_capture_data(loadable_recording)

        # Before this reader existed the call raised FileNotFoundError for a
        # capture.db that no recorder has written since 2026-07-17.
        assert not (loadable_recording / LEGACY_DB_NAME).exists()
        assert run.run_id

    def test_start_time_is_the_recording_timestamp(self, loadable_recording):
        expected = _scalar(
            loadable_recording, "SELECT timestamp FROM recording ORDER BY id LIMIT 1"
        )

        run = load_real_capture_data(loadable_recording)

        assert run.start_time.timestamp() == pytest.approx(expected)

    def test_end_time_is_the_last_observation(self, loadable_recording):
        # There is no ended_at column, so the end is derived from the events.
        expected = max(
            _scalar(loadable_recording, "SELECT MAX(timestamp) FROM screenshot"),
            _scalar(loadable_recording, "SELECT MAX(timestamp) FROM action_event"),
        )

        run = load_real_capture_data(loadable_recording)

        assert run.end_time.timestamp() == pytest.approx(expected)
        assert run.config["duration"] > 0

    def test_frame_count_is_the_screenshot_rows(self, loadable_recording):
        expected = _scalar(loadable_recording, "SELECT COUNT(*) FROM screenshot")

        run = load_real_capture_data(loadable_recording)

        # Frames are png_data blobs in the database. There is no screenshots/
        # directory to count files in.
        assert expected > 0
        assert not (loadable_recording / "screenshots").exists()
        assert run.config["frame_count"] == expected

    def test_event_count_is_the_action_event_rows(self, loadable_recording):
        expected = _scalar(loadable_recording, "SELECT COUNT(*) FROM action_event")

        run = load_real_capture_data(loadable_recording)

        assert expected > 0
        assert run.config["event_count"] == expected

    def test_display_metadata_comes_from_the_monitor_columns(self, loadable_recording):
        row = _row(loadable_recording, "SELECT * FROM recording ORDER BY id LIMIT 1")

        run = load_real_capture_data(loadable_recording)

        # The recorder names these monitor_width and monitor_height. The old
        # reader asked for screen_width and screen_height, which do not exist.
        assert run.config["screen_size"] == f"{row['monitor_width']}x{row['monitor_height']}"
        assert row["monitor_width"] > 0
        assert run.config["platform"] == row["platform"]
        assert run.config["task_description"] == row["task_description"]
        assert run.config["task_description"]

    def test_episodes_reach_the_run_unchanged(self, loadable_recording):
        """The fixture passes through. This says nothing about real segmentation."""
        fixture = json.loads(EPISODES_FIXTURE.read_text())

        run = load_real_capture_data(loadable_recording)

        assert run.config["episode_count"] == len(fixture["episodes"])
        assert [task.task_id for task in run.tasks] == [
            episode["episode_id"] for episode in fixture["episodes"]
        ]


class TestTheCountsTheseRecordingsDeclare:
    """Name the numbers, so a reader can check them against the fixtures."""

    @pytest.mark.parametrize(
        ("name", "frames", "events"),
        [("demo_new", 14, 14), ("turn-off-nightshift", 20, 20)],
    )
    def test_the_loader_reports_the_declared_counts(self, tmp_path, name, frames, events):
        directory = tmp_path / name
        directory.mkdir()
        shutil.copy(
            require_examples() / name / RECORDING_DB_NAME, directory / RECORDING_DB_NAME
        )
        shutil.copy(EPISODES_FIXTURE, directory / "episodes.json")

        run = load_real_capture_data(directory)

        assert run.config["frame_count"] == frames
        assert run.config["event_count"] == events


class TestLegacyCapturesAreRefused:
    """A capture.db cannot be read, so say so instead of loading a shell."""

    def test_a_legacy_directory_names_the_migration(self, tmp_path):
        directory = _legacy_capture(tmp_path / "old-recording")

        with pytest.raises(LegacyCaptureError, match="migrate_legacy_capture.py"):
            load_real_capture_data(directory)

    def test_a_legacy_directory_is_refused_before_episodes_are_read(self, tmp_path):
        directory = _legacy_capture(tmp_path / "old-recording")
        (directory / "episodes.json").unlink()

        # Both files are missing as far as this loader is concerned. The one
        # worth naming is the one with a fix attached.
        with pytest.raises(LegacyCaptureError, match="migrate_legacy_capture.py"):
            load_real_capture_data(directory)

    def test_a_directory_with_no_database_names_recording_db(self, tmp_path):
        directory = tmp_path / "not-a-recording"
        directory.mkdir()

        with pytest.raises(FileNotFoundError, match=RECORDING_DB_NAME):
            load_real_capture_data(directory)


class TestNoDatabaseIsCreatedAsASideEffect:
    """sqlite3.connect creates the file it is given. Never connect blind."""

    def test_a_refused_directory_gains_no_recording_db(self, tmp_path):
        directory = _legacy_capture(tmp_path / "old-recording")

        with pytest.raises(FileNotFoundError):
            load_real_capture_data(directory)

        assert not (directory / RECORDING_DB_NAME).exists()

    def test_an_empty_directory_gains_no_recording_db(self, tmp_path):
        directory = tmp_path / "not-a-recording"
        directory.mkdir()

        with pytest.raises(FileNotFoundError):
            load_real_capture_data(directory)

        assert list(directory.iterdir()) == []


def _connect(directory: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(directory / RECORDING_DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(directory: Path, query: str):
    with _connect(directory) as conn:
        return conn.execute(query).fetchone()[0]


def _row(directory: Path, query: str) -> sqlite3.Row:
    with _connect(directory) as conn:
        return conn.execute(query).fetchone()
