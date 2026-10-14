"""Reading the recording database that openadapt-capture writes.

The recorder writes one SQLAlchemy database per capture, at
``<recording>/recording.db``. Its tables are ``recording``, ``action_event``,
``screenshot``, ``window_event``, ``browser_event``, ``audio_info``,
``performance_stat`` and ``memory_stat``. The columns read here are listed
beside each query below.

This module is the one place in the package that knows the file layout and the
column names. The catalog scanner and the benchmark loader both read through
it, so a schema change is a change to one reader rather than two that drift
apart.

It reads the database with ``sqlite3`` rather than through
``openadapt_capture.CaptureSession.load``. Both callers need a few scalars and
two row counts, and installing ``openadapt-capture`` to get them would pull
mss, sounddevice, soundfile, matplotlib, sqlalchemy, alembic, numpy and the
platform accessibility stack into an HTML generator. It is therefore not a
dependency of this package. Anything that needs decoded frames or replayable
actions should import ``CaptureSession`` rather than extend this module.

It does not read the pre-2026-07-17 ``capture.db``. That format held a single
``capture`` row and a generic ``events`` table; openadapt-capture PR #28
replaced it, and current code cannot load it at all. openadapt-capture owns
that format and ships ``scripts/migrate_legacy_capture.py`` to convert it, so a
legacy directory is reported with the conversion command rather than translated
a second time here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

#: Filename the current recorder writes, one per capture directory.
RECORDING_DB_NAME = "recording.db"

#: Filename the pre-2026-07-17 recorder wrote. Detected, never read.
LEGACY_DB_NAME = "capture.db"

#: Printed or raised when a directory holds only a legacy capture.
LEGACY_HINT = (
    "holds the legacy {legacy} format, which the current viewer cannot read. "
    "Convert it with openadapt-capture: "
    "python scripts/migrate_legacy_capture.py <src> <dest>"
)

#: Event tables whose timestamps bound the recording's duration.
_TIMESTAMPED_TABLES = ("screenshot", "action_event", "window_event", "browser_event")


class LegacyCaptureError(FileNotFoundError):
    """Raised for a directory holding a pre-#28 ``capture.db`` and nothing else.

    A subclass of ``FileNotFoundError`` because that is what the absence of a
    readable ``recording.db`` is, and because callers that only want to report
    "this directory is not loadable" need no new except clause. The CLI's
    top-level handler and the scanner's per-directory handler both rely on
    that.

    The cost of the inheritance is that a caller which catches
    ``FileNotFoundError`` to mean "try a different format" swallows this too,
    and the message it carries is the only place the conversion command
    appears. Such a caller must re-raise this class ahead of the broad clause.
    """


@dataclass(frozen=True)
class RecordingMetadata:
    """The recording-level facts both viewers need, read from one database.

    Field names follow this package's vocabulary rather than the recorder's:
    the catalog has always called the display size ``screen_width`` and
    ``screen_height``, where the recorder's columns are ``monitor_width`` and
    ``monitor_height``.
    """

    #: Epoch seconds at which recording started, the ``recording.timestamp``.
    started_at: float
    #: Epoch seconds of the newest observation, or None when there is none.
    ended_at: float | None
    platform: str | None
    screen_width: int | None
    screen_height: int | None
    pixel_ratio: float | None
    task_description: str | None
    #: Rows in ``action_event``: one mouse or key event each.
    event_count: int
    #: Rows in ``screenshot``: frames are PNG blobs in the database now.
    frame_count: int

    @property
    def duration_seconds(self) -> float | None:
        """Seconds from the start of the recording to its last observation.

        Returns:
            The duration, or None when no event bounds it.
        """
        if self.ended_at is None:
            return None
        return float(self.ended_at) - float(self.started_at)


def is_legacy_capture(recording_dir: Path) -> bool:
    """Report whether a directory holds a legacy capture and no current one.

    Args:
        recording_dir: A capture directory.

    Returns:
        True if only the pre-#28 ``capture.db`` is present.
    """
    recording_dir = Path(recording_dir)
    return (recording_dir / LEGACY_DB_NAME).is_file() and not (
        recording_dir / RECORDING_DB_NAME
    ).is_file()


def find_recording_db(recording_dir: Path) -> Path:
    """Return the path to a capture directory's recording database.

    Args:
        recording_dir: A capture directory.

    Returns:
        The path to its ``recording.db``.

    Raises:
        LegacyCaptureError: If the directory holds only a legacy ``capture.db``.
            The message carries the conversion command.
        FileNotFoundError: If the directory holds neither database.
    """
    recording_dir = Path(recording_dir)
    recording_db = recording_dir / RECORDING_DB_NAME

    # is_file(), not exists(): sqlite3.connect CREATES an empty database at any
    # path it is handed, so an unchecked connect turns "this directory has no
    # recording" into a zero-byte recording.db left behind in the user's
    # capture directory.
    if recording_db.is_file():
        return recording_db

    if is_legacy_capture(recording_dir):
        raise LegacyCaptureError(
            f"{recording_dir} " + LEGACY_HINT.format(legacy=LEGACY_DB_NAME)
        )
    raise FileNotFoundError(f"No {RECORDING_DB_NAME} in {recording_dir}")


def read_recording_metadata(recording_dir: Path) -> RecordingMetadata:
    """Read one capture directory's recording-level metadata.

    Args:
        recording_dir: A capture directory holding a ``recording.db``.

    Returns:
        The metadata the database states.

    Raises:
        LegacyCaptureError: If the directory holds only a legacy ``capture.db``.
        FileNotFoundError: If the directory holds neither database.
        sqlite3.Error: If the database is corrupt or missing a table.
        IndexError: If a queried column is absent, which sqlite3.Row raises.
    """
    recording_db = find_recording_db(recording_dir)

    with sqlite3.connect(str(recording_db)) as conn:
        conn.row_factory = sqlite3.Row

        # One row per capture directory, matching CaptureSession.load's
        # `session.query(Recording).first()`. Columns: timestamp (epoch seconds
        # at record start), monitor_width, monitor_height, pixel_ratio,
        # platform, task_description.
        row = conn.execute("SELECT * FROM recording ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise sqlite3.DatabaseError(f"recording table is empty in {recording_db}")

        if row["timestamp"] is None:
            raise sqlite3.DatabaseError(f"recording.timestamp is NULL in {recording_db}")

        started_at = float(row["timestamp"])
        # An observation older than the recording's own start cannot bound its
        # end, and reporting one would produce a negative duration.
        ended_at = _last_observation(conn)
        if ended_at is not None and ended_at < started_at:
            ended_at = None

        return RecordingMetadata(
            started_at=started_at,
            ended_at=ended_at,
            platform=row["platform"],
            screen_width=row["monitor_width"],
            screen_height=row["monitor_height"],
            pixel_ratio=row["pixel_ratio"],
            task_description=row["task_description"],
            event_count=conn.execute("SELECT COUNT(*) FROM action_event").fetchone()[0],
            frame_count=conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0],
        )


def _last_observation(conn: sqlite3.Connection) -> float | None:
    """Return the newest event timestamp in a recording database.

    There is no ``ended_at`` column. The recording ends at its last
    observation, so the end is the newest timestamp across the event tables
    that carry one.

    Args:
        conn: An open connection to a recording.db.

    Returns:
        The newest timestamp in epoch seconds, or None if no event carries one.
    """
    latest = None

    for table in _TIMESTAMPED_TABLES:
        try:
            value = conn.execute(f"SELECT MAX(timestamp) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            # An optional table this build does not have. Other tables can
            # still bound the duration.
            continue
        if value is not None and (latest is None or value > latest):
            latest = value

    return None if latest is None else float(latest)
