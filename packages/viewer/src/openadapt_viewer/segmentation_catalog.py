"""
Generate catalog data for segmentation viewer's automatic file discovery.

This module scans for available episode library and segmentation result files,
then generates JavaScript code that can be embedded in the segmentation viewer
for automatic discovery and selection.
"""

import json
from datetime import datetime
from pathlib import Path

from .catalog import RecordingCatalog, get_catalog


class SegmentationCatalogEntry:
    """Represents an available segmentation result file for the viewer."""

    def __init__(
        self,
        file_path: str,
        recording_name: str,
        recording_id: str,
        created_at: float,
        episode_count: int,
        file_type: str,  # "episode_library" or "episodes"
    ):
        self.file_path = file_path
        self.recording_name = recording_name
        self.recording_id = recording_id
        self.created_at = created_at
        self.episode_count = episode_count
        self.file_type = file_type

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "file_path": self.file_path,
            "recording_name": self.recording_name,
            "recording_id": self.recording_id,
            "created_at": self.created_at,
            # .astimezone() attaches the local zone without shifting the wall
            # clock, so this rendered string is unchanged; it is what the
            # user recorded on this machine, not UTC.
            "created_at_formatted": datetime.fromtimestamp(self.created_at)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
            "episode_count": self.episode_count,
            "file_type": self.file_type,
        }


def discover_episode_files(
    segmentation_dirs: list[str] | None = None,
    catalog: RecordingCatalog | None = None
) -> list[SegmentationCatalogEntry]:
    """
    Discover all available episode files (episode_library.json and *_episodes.json).

    Args:
        segmentation_dirs: Directories to scan for episode files
        catalog: RecordingCatalog instance for metadata lookups

    Returns:
        List of SegmentationCatalogEntry objects sorted by creation time (newest first)
    """
    if catalog is None:
        catalog = get_catalog()

    if segmentation_dirs is None:
        # Default locations
        segmentation_dirs = []
        possible_paths = [
            Path.home() / "oa" / "src" / "openadapt-ml" / "segmentation_output",
            Path.cwd() / "segmentation_output",
            Path.home() / ".openadapt" / "segmentation_output",
        ]
        segmentation_dirs = [str(p) for p in possible_paths if p.exists()]

    entries = []

    for seg_dir in segmentation_dirs:
        seg_path = Path(seg_dir)
        if not seg_path.exists():
            continue

        # Find episode_library.json (consolidated episodes from all recordings)
        library_file = seg_path / "episode_library.json"
        if library_file.exists():
            try:
                with open(library_file) as f:
                    data = json.load(f)

                episode_count = len(data.get("episodes", []))
                recording_count = len({
                    rid
                    for ep in data.get("episodes", [])
                    for rid in ep.get("source_recordings", [])
                })

                entries.append(SegmentationCatalogEntry(
                    file_path=str(library_file),
                    recording_name=f"Library ({recording_count} recordings)",
                    recording_id="episode_library",
                    created_at=library_file.stat().st_mtime,
                    episode_count=episode_count,
                    file_type="episode_library",
                ))
            except (OSError, ValueError, TypeError, AttributeError) as e:
                # ValueError covers json.JSONDecodeError; TypeError and
                # AttributeError cover a JSON document whose top level is not
                # the mapping this reader expects. A bad file is skipped; a
                # bug in this module is not.
                print(f"Warning: Failed to read {library_file}: {e}")

        # Find individual *_episodes.json files
        for episodes_file in seg_path.glob("*_episodes.json"):
            try:
                with open(episodes_file) as f:
                    data = json.load(f)

                recording_id = episodes_file.stem.replace("_episodes", "")
                episode_count = len(data.get("episodes", []))

                # Try to get recording name from catalog
                recording = catalog.get_recording(recording_id)
                recording_name = recording.name if recording else recording_id.replace("-", " ").replace("_", " ").title()

                entries.append(SegmentationCatalogEntry(
                    file_path=str(episodes_file),
                    recording_name=recording_name,
                    recording_id=recording_id,
                    created_at=episodes_file.stat().st_mtime,
                    episode_count=episode_count,
                    file_type="episodes",
                ))
            except (OSError, ValueError, TypeError, AttributeError) as e:
                # ValueError covers json.JSONDecodeError; TypeError and
                # AttributeError cover a JSON document whose top level is not
                # the mapping this reader expects. A bad file is skipped; a
                # bug in this module is not.
                print(f"Warning: Failed to read {episodes_file}: {e}")

    # Sort by creation time (newest first)
    entries.sort(key=lambda e: e.created_at, reverse=True)

    return entries


def generate_catalog_javascript(
    segmentation_dirs: list[str] | None = None,
    catalog: RecordingCatalog | None = None
) -> str:
    """
    Generate JavaScript code that embeds episode file catalog data.

    This generates a global variable window.SEGMENTATION_CATALOG with:
    - List of available episode files
    - Metadata (recording name, date, episode count)
    - Helper functions for accessing the catalog

    Args:
        segmentation_dirs: Directories to scan for episode files
        catalog: RecordingCatalog instance for metadata lookups

    Returns:
        JavaScript code string
    """
    entries = discover_episode_files(segmentation_dirs, catalog)

    catalog_data = {
        "files": [entry.to_dict() for entry in entries],
        "generated_at": datetime.now().astimezone().isoformat(),
        "total_files": len(entries),
    }

    js_code = f"""
// Segmentation File Catalog (auto-generated)
// Generated at: {catalog_data['generated_at']}
window.SEGMENTATION_CATALOG = {json.dumps(catalog_data, indent=2)};

// Helper functions for catalog access
window.SegmentationCatalog = {{
    /**
     * Get all available episode files, sorted by creation time (newest first)
     */
    getFiles: function() {{
        return window.SEGMENTATION_CATALOG.files;
    }},

    /**
     * Get the latest episode file (most recently created)
     */
    getLatest: function() {{
        const files = this.getFiles();
        return files.length > 0 ? files[0] : null;
    }},

    /**
     * Get episode file by recording ID
     */
    getByRecordingId: function(recordingId) {{
        return this.getFiles().find(f => f.recording_id === recordingId);
    }},

    /**
     * Get episode library file (consolidated episodes)
     */
    getLibrary: function() {{
        return this.getFiles().find(f => f.file_type === 'episode_library');
    }},

    /**
     * Get individual recording episode files (not the library)
     */
    getRecordingFiles: function() {{
        return this.getFiles().filter(f => f.file_type === 'episodes');
    }},

    /**
     * Get total number of files available
     */
    getCount: function() {{
        return window.SEGMENTATION_CATALOG.total_files;
    }}
}};

console.log('Segmentation Catalog loaded:', window.SEGMENTATION_CATALOG.total_files, 'files available');
"""

    return js_code


def generate_catalog_json(
    output_path: str,
    segmentation_dirs: list[str] | None = None,
    catalog: RecordingCatalog | None = None
):
    """
    Generate a standalone JSON catalog file.

    This is an alternative to JavaScript embedding for environments
    that need pure JSON.

    Args:
        output_path: Path to write the JSON file
        segmentation_dirs: Directories to scan for episode files
        catalog: RecordingCatalog instance for metadata lookups
    """
    entries = discover_episode_files(segmentation_dirs, catalog)

    catalog_data = {
        "files": [entry.to_dict() for entry in entries],
        "generated_at": datetime.now().astimezone().isoformat(),
        "total_files": len(entries),
    }

    with open(output_path, "w") as f:
        json.dump(catalog_data, f, indent=2)

    print(f"Catalog written to {output_path}")
    print(f"Total files: {len(entries)}")


if __name__ == "__main__":
    # CLI for generating catalog files
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate segmentation file catalog for viewer"
    )
    parser.add_argument(
        "--output",
        default="segmentation_catalog.js",
        help="Output file path (JSON or JS)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of JavaScript",
    )
    parser.add_argument(
        "--scan-dir",
        action="append",
        dest="scan_dirs",
        help="Additional directory to scan for episode files",
    )

    args = parser.parse_args()

    if args.json:
        generate_catalog_json(args.output, segmentation_dirs=args.scan_dirs)
    else:
        js_code = generate_catalog_javascript(segmentation_dirs=args.scan_dirs)
        with open(args.output, "w") as f:
            f.write(js_code)
        print(f"JavaScript catalog written to {args.output}")
