"""
API generation for catalog data to be embedded in HTML viewers.

Since viewers are static HTML files (file:// protocol), we generate JavaScript
data that embeds catalog information directly into the HTML.
"""

import json

from .catalog import RecordingCatalog, get_catalog


def generate_catalog_javascript(
    catalog: RecordingCatalog | None = None,
    include_segmentations: bool = True
) -> str:
    """
    Generate JavaScript code that embeds catalog data.

    Args:
        catalog: RecordingCatalog instance (uses default if None)
        include_segmentations: Include segmentation results for each recording

    Returns:
        JavaScript code defining window.OPENADAPT_CATALOG
    """
    if catalog is None:
        catalog = get_catalog()

    recordings = catalog.get_all_recordings()

    # Build catalog data structure
    catalog_data = {
        "recordings": [],
        "stats": catalog.get_stats(),
    }

    for recording in recordings:
        rec_data = {
            "id": recording.id,
            "name": recording.name,
            "description": recording.description,
            "path": recording.path,
            "created_at": recording.created_at,
            "duration_seconds": recording.duration_seconds,
            "frame_count": recording.frame_count,
            "event_count": recording.event_count,
            "task_description": recording.task_description,
            "tags": recording.tags,
        }

        if include_segmentations:
            seg_results = catalog.get_segmentation_results(recording.id)
            rec_data["segmentations"] = []

            for seg in seg_results:
                rec_data["segmentations"].append({
                    "id": seg.id,
                    "path": seg.path,
                    "created_at": seg.created_at,
                    "episode_count": seg.episode_count,
                    "boundary_count": seg.boundary_count,
                    "status": seg.status,
                    "llm_model": seg.llm_model,
                })

        catalog_data["recordings"].append(rec_data)

    # Generate JavaScript
    js_code = f"""
// OpenAdapt Catalog Data (auto-generated)
window.OPENADAPT_CATALOG = {json.dumps(catalog_data, indent=2)};

// Helper functions for accessing catalog
window.OpenAdaptCatalog = {{
    getRecordings: function() {{
        return window.OPENADAPT_CATALOG.recordings;
    }},

    getRecording: function(recordingId) {{
        return window.OPENADAPT_CATALOG.recordings.find(r => r.id === recordingId);
    }},

    getRecordingsWithSegmentations: function() {{
        return window.OPENADAPT_CATALOG.recordings.filter(r =>
            r.segmentations && r.segmentations.length > 0
        );
    }},

    getSegmentationResults: function(recordingId) {{
        const recording = this.getRecording(recordingId);
        return recording ? recording.segmentations : [];
    }},

    getStats: function() {{
        return window.OPENADAPT_CATALOG.stats;
    }}
}};
"""

    return js_code


def generate_recording_dropdown_html(
    catalog: RecordingCatalog | None = None,
    selected_id: str | None = None,
    filter_with_segmentations: bool = True
) -> str:
    """
    Generate HTML for a recording selection dropdown.

    Args:
        catalog: RecordingCatalog instance (uses default if None)
        selected_id: ID of recording to select by default
        filter_with_segmentations: Only show recordings that have segmentation results

    Returns:
        HTML string for a <select> element
    """
    if catalog is None:
        catalog = get_catalog()

    recordings = catalog.get_all_recordings()

    html_parts = ['<select id="recording-select" class="recording-dropdown">']
    html_parts.append('  <option value="">Select a recording...</option>')

    for recording in recordings:
        if filter_with_segmentations:
            seg_results = catalog.get_segmentation_results(recording.id)
            if not seg_results:
                continue

        selected = ' selected' if recording.id == selected_id else ''
        duration = f" ({recording.duration_seconds:.1f}s)" if recording.duration_seconds else ""
        html_parts.append(
            f'  <option value="{recording.id}"{selected}>'
            f'{recording.name}{duration}'
            f'</option>'
        )

    html_parts.append('</select>')

    return '\n'.join(html_parts)


def get_catalog_summary() -> dict:
    """
    Get a summary of the catalog for display in viewers.

    Returns:
        Dict with recording count, recent recordings, etc.
    """
    catalog = get_catalog()
    stats = catalog.get_stats()
    recordings = catalog.get_all_recordings()

    # Get recent recordings (last 5)
    recent = []
    for rec in recordings[:5]:
        recent.append({
            "id": rec.id,
            "name": rec.name,
            "created_at": rec.created_at,
            "duration_seconds": rec.duration_seconds,
        })

    return {
        "total_recordings": stats["recording_count"],
        "total_segmentations": stats["segmentation_count"],
        "total_episodes": stats["episode_count"],
        "recent_recordings": recent,
        "db_path": stats["db_path"],
    }
