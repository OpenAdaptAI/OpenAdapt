"""Locating the recordings openadapt-capture commits, for tests to read.

openadapt-capture commits two recordings under ``examples/captures``,
regenerated and byte-compared by its own CI. Tests that exercise a reader of
the recording format read those, rather than a database written here to match
this repository's idea of the schema. A test that builds its own input can only
prove the reader agrees with the test author, which is how the format drifted
unnoticed for six weeks.

Two readers depend on this now -- the catalog scanner and the benchmark
loader -- so the lookup lives here rather than in either test module.

The legacy format has no committed example, because openadapt-capture stopped
writing it. ``write_legacy_capture`` below builds one, which is sound for the
opposite reason: the only thing read from a legacy directory is that it holds a
``capture.db`` this viewer refuses.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

#: Set by CI to the checked-out openadapt-capture's examples/captures.
EXAMPLES_ENV = "OPENADAPT_CAPTURE_EXAMPLES"

#: The recordings openadapt-capture commits, with what its specs declare.
REAL_RECORDINGS = ("demo_new", "turn-off-nightshift")


def examples_dir() -> Path | None:
    """Return the directory holding the committed example recordings, or None.

    Honours ``$OPENADAPT_CAPTURE_EXAMPLES`` first, then looks for an
    openadapt-capture checkout beside this one. No absolute path is written
    here: this repository is public and one developer's home directory is not
    a location any other user has.

    Returns:
        The directory, or None when neither source is present.
    """
    named = os.environ.get(EXAMPLES_ENV)
    if named:
        return Path(named).expanduser()

    sibling = REPO_ROOT.parent / "openadapt-capture" / "examples" / "captures"
    return sibling if sibling.is_dir() else None


def require_examples() -> Path:
    """Return the examples directory, skipping or failing with a reason.

    Returns:
        The directory holding the committed example recordings.
    """
    directory = examples_dir()
    if directory is None:
        pytest.skip(
            "No openadapt-capture checkout found. Clone it beside this "
            f"repository, or set ${EXAMPLES_ENV} to its examples/captures."
        )
    if not directory.is_dir():
        # The variable was set on purpose, so an absent directory is an error
        # in the caller's setup, not a reason to quietly pass.
        pytest.fail(f"${EXAMPLES_ENV} names {directory}, which is not a directory")
    missing = [
        name
        for name in REAL_RECORDINGS
        if not (directory / name / "recording.db").is_file()
    ]
    if missing:
        pytest.fail(f"{directory} is missing recording.db for: {', '.join(missing)}")
    return directory


def write_legacy_capture(directory: Path) -> Path:
    """Write a pre-#28 capture directory, in the format this viewer refuses.

    The shape is the one openadapt-capture's ``migrate_legacy_capture.py``
    reads: one ``capture`` row and a generic ``events`` table.

    Args:
        directory: Where to write it. Created if it does not exist.

    Returns:
        The directory, now holding a ``capture.db`` and no ``recording.db``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / "capture.db") as conn:
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
    return directory
