"""
Automatic discovery and scanning of OpenAdapt recordings and results.

This module scans directories to find:
- Recordings from openadapt-capture (directories with recording.db)
- Segmentation results from openadapt-ml (JSON files with episodes)
- Episode data for indexing

Every read of a recording database goes through
:mod:`openadapt_viewer.recording_db`, which owns the file layout, the column
names and the refusal to read the pre-2026-07-17 ``capture.db``. This module
decides what to do with what that reader returns: index it, or name the
directory in a warning and move on.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Recording, RecordingCatalog, SegmentationResult
from .recording_db import (
    LEGACY_DB_NAME,
    LEGACY_HINT,
    RECORDING_DB_NAME,
    is_legacy_capture,
    read_recording_metadata,
)


class RecordingScanner:
    """Scanner for discovering and indexing OpenAdapt data."""

    def __init__(self, catalog: RecordingCatalog):
        """
        Initialize the scanner.

        Args:
            catalog: The RecordingCatalog instance to populate
        """
        self.catalog = catalog

    def scan_recording_directory(
        self,
        base_path: str,
        recursive: bool = False
    ) -> list[Recording]:
        """
        Scan a directory for recordings (directories containing recording.db).

        A directory holding only the legacy ``capture.db`` is named in a
        warning and skipped. Reporting it is the point: the two formats share
        no table, so registering one would produce a catalog entry with no
        date, no duration and no counts.

        Args:
            base_path: Path to scan for recordings
            recursive: If True, scan subdirectories recursively

        Returns:
            List of newly registered Recording objects
        """
        base_path = Path(base_path).resolve()
        recordings = []

        if not base_path.exists():
            raise FileNotFoundError(f"Directory not found: {base_path}")

        prefix = "**/" if recursive else "*/"

        for legacy_db in base_path.glob(f"{prefix}{LEGACY_DB_NAME}"):
            if not is_legacy_capture(legacy_db.parent):
                # Already converted in place; the current database wins.
                continue
            print(f"Warning: {legacy_db.parent} " + LEGACY_HINT.format(legacy=LEGACY_DB_NAME))

        for recording_db in base_path.glob(f"{prefix}{RECORDING_DB_NAME}"):
            recording_dir = recording_db.parent
            recording_id = recording_dir.name

            try:
                recording = self._extract_recording_info(recording_dir, recording_id)
                # register_recording needs positional args, then kwargs
                registered = self.catalog.register_recording(
                    recording_id=recording.id,
                    name=recording.name,
                    path=recording.path,
                    created_at=recording.created_at,
                    duration_seconds=recording.duration_seconds,
                    frame_count=recording.frame_count,
                    event_count=recording.event_count,
                    task_description=recording.task_description,
                    tags=recording.tags,
                    metadata=recording.metadata,
                )
                recordings.append(registered)
            except (OSError, sqlite3.Error, ValueError) as e:
                # Narrow on purpose: an unreadable directory, a corrupt
                # recording.db or a row that fails Recording validation should
                # skip that recording, not the whole scan. Anything else is a
                # bug in this module and must surface.
                print(f"Warning: Failed to index {recording_dir}: {e}")
                continue

        return recordings

    def _extract_recording_info(
        self,
        recording_dir: Path,
        recording_id: str
    ) -> Recording:
        """
        Extract recording metadata from a recording directory.

        Args:
            recording_dir: Path to the recording directory
            recording_id: Identifier for the recording (usually directory name)

        Returns:
            Recording object with extracted metadata

        Raises:
            FileNotFoundError: If the directory holds no recording.db. A legacy
                capture.db is named as such, with the conversion command.
        """
        recording_dir = Path(recording_dir)
        screenshots_dir = recording_dir / "screenshots"

        metadata = {}
        created_at = None
        duration_seconds = None
        task_description = None
        event_count = None
        frame_count = None

        # A FileNotFoundError from the reader is deliberately not caught. It
        # means the directory holds no recording.db, and for a legacy capture
        # its message carries the conversion command. Returning a hollow
        # Recording instead is what made a legacy directory look indexable: no
        # date, no duration, no counts, no complaint.
        try:
            read = read_recording_metadata(recording_dir)
        except (sqlite3.Error, IndexError, TypeError, ValueError) as e:
            # sqlite3.Error covers a corrupt database and a missing table;
            # IndexError is sqlite3.Row's "no such column"; TypeError and
            # ValueError are a NULL or a non-numeric value in a column read as
            # a number. Each means "no event metadata", not "abort", so the
            # directory is still indexed by name and mtime.
            print(f"Warning: Could not read {recording_dir / RECORDING_DB_NAME}: {e}")
        else:
            created_at = read.started_at
            duration_seconds = read.duration_seconds
            task_description = read.task_description
            event_count = read.event_count
            frame_count = read.frame_count
            metadata.update({
                "platform": read.platform,
                "screen_width": read.screen_width,
                "screen_height": read.screen_height,
                "pixel_ratio": read.pixel_ratio,
            })

        # Some capture directories still carry PNGs under screenshots/. Read
        # them only when the database could not answer, so a readable database
        # reporting zero retained frames is reported as zero, not overwritten.
        if frame_count is None and screenshots_dir.is_dir():
            frame_count = len(list(screenshots_dir.glob("*.png")))

        if created_at is None:
            created_at = recording_dir.stat().st_mtime

        return Recording(
            id=recording_id,
            name=recording_id.replace("_", " ").replace("-", " ").title(),
            path=str(recording_dir),
            created_at=created_at,
            duration_seconds=duration_seconds,
            frame_count=frame_count,
            event_count=event_count,
            task_description=task_description,
            metadata=metadata,
        )

    def scan_segmentation_results(
        self,
        segmentation_dir: str
    ) -> list[SegmentationResult]:
        """
        Scan directory for segmentation result JSON files.

        Looks for files matching pattern: {recording_id}_episodes.json

        Args:
            segmentation_dir: Path to segmentation output directory

        Returns:
            List of newly registered SegmentationResult objects
        """
        segmentation_dir = Path(segmentation_dir).resolve()
        results = []

        if not segmentation_dir.exists():
            raise FileNotFoundError(f"Directory not found: {segmentation_dir}")

        # Find all *_episodes.json files
        for episodes_file in segmentation_dir.glob("*_episodes.json"):
            try:
                result = self._extract_segmentation_info(episodes_file)
                registered = self.catalog.register_segmentation(
                    segmentation_id=result.id,
                    recording_id=result.recording_id,
                    path=result.path,
                    created_at=result.created_at,
                    episode_count=result.episode_count,
                    boundary_count=result.boundary_count,
                    status=result.status,
                    llm_model=result.llm_model,
                    metadata=result.metadata,
                )

                # Also index episodes
                self._index_episodes_from_file(episodes_file, registered.id, registered.recording_id)

                results.append(registered)
            except (OSError, ValueError, KeyError, TypeError) as e:
                # ValueError covers json.JSONDecodeError and SegmentationResult
                # validation; KeyError/TypeError cover an episodes file whose
                # shape is not what this scanner expects.
                print(f"Warning: Failed to index {episodes_file}: {e}")
                continue

        return results

    def _extract_segmentation_info(
        self,
        episodes_file: Path
    ) -> SegmentationResult:
        """
        Extract segmentation result metadata from JSON file.

        Args:
            episodes_file: Path to {recording_id}_episodes.json

        Returns:
            SegmentationResult object
        """
        with open(episodes_file) as f:
            data = json.load(f)

        # Extract recording ID from filename
        recording_id = episodes_file.stem.replace("_episodes", "")
        segmentation_id = (
            f"{recording_id}_segmentation_"
            f"{int(datetime.now(timezone.utc).timestamp())}"
        )

        created_at = episodes_file.stat().st_mtime

        # Parse processing timestamp if present
        if "processing_timestamp" in data:
            try:
                dt = datetime.fromisoformat(data["processing_timestamp"])
                created_at = dt.timestamp()
            except (ValueError, TypeError) as e:
                # Falling back to the file mtime is deliberate, but say so. A
                # bare `except: pass` here also swallowed KeyboardInterrupt and
                # made an unparseable timestamp indistinguishable from an
                # absent one -- the catalog then showed the file's mtime as the
                # segmentation date with no indication it was a guess.
                print(
                    f"Warning: Could not parse processing_timestamp "
                    f"{data['processing_timestamp']!r} in {episodes_file}; "
                    f"using file mtime instead ({e})"
                )

        episode_count = len(data.get("episodes", []))
        boundary_count = len(data.get("boundaries", []))

        return SegmentationResult(
            id=segmentation_id,
            recording_id=recording_id,
            path=str(episodes_file),
            created_at=created_at,
            episode_count=episode_count,
            boundary_count=boundary_count,
            status="complete" if episode_count > 0 else "partial",
            llm_model=data.get("llm_model"),
            metadata={
                "coverage": data.get("coverage"),
                "avg_confidence": data.get("avg_confidence"),
            },
        )

    def _index_episodes_from_file(
        self,
        episodes_file: Path,
        segmentation_result_id: str,
        recording_id: str
    ):
        """
        Index all episodes from a segmentation result file.

        Args:
            episodes_file: Path to episodes JSON file
            segmentation_result_id: ID of the parent segmentation result
            recording_id: ID of the source recording
        """
        with open(episodes_file) as f:
            data = json.load(f)

        episodes = data.get("episodes", [])

        for idx, episode_data in enumerate(episodes):
            episode_id = f"{recording_id}_episode_{idx}"

            self.catalog.register_episode(
                episode_id=episode_id,
                segmentation_result_id=segmentation_result_id,
                recording_id=recording_id,
                name=episode_data.get("name"),
                description=episode_data.get("description"),
                start_time=episode_data.get("start_time"),
                end_time=episode_data.get("end_time"),
                start_frame=episode_data.get("start_frame"),
                end_frame=episode_data.get("end_frame"),
                confidence=episode_data.get("confidence"),
                metadata=episode_data,
            )

    def scan_all(
        self,
        capture_dirs: list[str] | None = None,
        segmentation_dirs: list[str] | None = None
    ) -> dict[str, int]:
        """
        Scan multiple directories for recordings and segmentation results.

        Args:
            capture_dirs: List of directories to scan for recordings
            segmentation_dirs: List of directories to scan for segmentation results

        Returns:
            Dict with counts of newly indexed items
        """
        counts = {
            "recordings": 0,
            "segmentations": 0,
        }

        # Default paths if not specified
        if capture_dirs is None:
            # Try common locations
            capture_dirs = []
            possible_paths = [
                Path.home() / "oa" / "src" / "openadapt-capture",
                Path.cwd() / "recordings",
                Path.home() / ".openadapt" / "recordings",
            ]
            for path in possible_paths:
                if path.exists():
                    capture_dirs.append(str(path))

        if segmentation_dirs is None:
            segmentation_dirs = []
            possible_paths = [
                Path.home() / "oa" / "src" / "openadapt-ml" / "segmentation_output",
                Path.cwd() / "segmentation_output",
                Path.home() / ".openadapt" / "segmentation_output",
            ]
            for path in possible_paths:
                if path.exists():
                    segmentation_dirs.append(str(path))

        # Scan recordings
        for capture_dir in capture_dirs:
            try:
                recordings = self.scan_recording_directory(capture_dir, recursive=False)
                counts["recordings"] += len(recordings)
                print(f"Found {len(recordings)} recordings in {capture_dir}")
            except (OSError, sqlite3.Error, ValueError) as e:
                print(f"Warning: Failed to scan {capture_dir}: {e}")

        # Scan segmentation results
        for seg_dir in segmentation_dirs:
            try:
                results = self.scan_segmentation_results(seg_dir)
                counts["segmentations"] += len(results)
                print(f"Found {len(results)} segmentation results in {seg_dir}")
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"Warning: Failed to scan {seg_dir}: {e}")

        return counts


def scan_and_update_catalog(
    catalog: RecordingCatalog | None = None,
    capture_dirs: list[str] | None = None,
    segmentation_dirs: list[str] | None = None
) -> dict[str, int]:
    """
    Convenience function to scan and update the catalog.

    Args:
        catalog: RecordingCatalog instance (uses default if None)
        capture_dirs: List of directories to scan for recordings
        segmentation_dirs: List of directories to scan for segmentation results

    Returns:
        Dict with counts of newly indexed items
    """
    from .catalog import get_catalog

    if catalog is None:
        catalog = get_catalog()

    scanner = RecordingScanner(catalog)
    return scanner.scan_all(capture_dirs, segmentation_dirs)
