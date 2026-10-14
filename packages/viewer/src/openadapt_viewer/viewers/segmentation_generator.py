"""
Generate enhanced segmentation viewer with catalog integration.

This module creates an HTML viewer that automatically discovers recordings
from the catalog database instead of requiring manual file selection.
"""

import json
from pathlib import Path

from ..catalog import RecordingCatalog, get_catalog
from ..catalog_api import generate_catalog_javascript, generate_recording_dropdown_html


def generate_segmentation_viewer(
    output_path: str = "segmentation_viewer_catalog.html",
    catalog: RecordingCatalog | None = None,
    auto_load_recording: str | None = None,
) -> str:
    """
    Generate segmentation viewer with automatic catalog integration.

    Args:
        output_path: Path to output HTML file
        catalog: RecordingCatalog instance (uses default if None)
        auto_load_recording: Recording ID to auto-load on page load

    Returns:
        Path to generated HTML file
    """
    if catalog is None:
        catalog = get_catalog()

    # Generate catalog data and dropdown
    catalog_js = generate_catalog_javascript(catalog, include_segmentations=True)
    recording_dropdown = generate_recording_dropdown_html(
        catalog,
        selected_id=auto_load_recording,
        filter_with_segmentations=True
    )

    # If auto-load is specified, embed the segmentation data
    embedded_data_js = ""
    if auto_load_recording:
        recording = catalog.get_recording(auto_load_recording)
        if recording:
            seg_results = catalog.get_segmentation_results(recording.id)
            if seg_results:
                latest_seg = seg_results[0]
                try:
                    with open(latest_seg.path) as f:
                        seg_data = json.load(f)
                    embedded_data_js = f"""
    // Embedded segmentation data for auto-load
    window.OPENADAPT_EMBEDDED_DATA = {{
        '{auto_load_recording}': {json.dumps(seg_data, indent=2)}
    }};
"""
                except (OSError, ValueError) as e:
                    # OSError: the catalogued segmentation file has moved or is
                    # unreadable. ValueError covers json.JSONDecodeError. The
                    # viewer is still generated, just without pre-loaded data.
                    print(f"Warning: Could not embed segmentation data: {e}")

    # Read base segmentation viewer template
    template_dir = Path(__file__).parent.parent.parent.parent
    base_viewer_path = template_dir / "segmentation_viewer.html"

    with open(base_viewer_path) as f:
        html_content = f.read()

    # Replace file input section with catalog dropdown
    # Find and replace the file input section
    import re

    # Pattern to match the file input section
    file_input_pattern = re.compile(
        r'<section class="file-input-section">.*?<div class="file-input-area">.*?<input[^>]*id="file-input"[^>]*>',
        re.DOTALL
    )

    catalog_dropdown_section = f"""<section class="file-input-section">
            <h2>Select Recording</h2>
            <p style="color: #b8b8d1; margin-bottom: 15px;">
                Choose from automatically discovered recordings with segmentation results
            </p>

            <div class="file-input-area">
                {recording_dropdown}
                <button id="refresh-catalog-btn" class="secondary-btn" style="margin-left: 10px;">🔄 Refresh Catalog</button>
                <div class="catalog-info" style="margin-left: auto; color: #b8b8d1; font-size: 0.9em;">
                    <span id="catalog-stats">Loading...</span>
                </div>
                <input type="file" id="file-input" accept=".json" style="display:none;">"""

    html_content = file_input_pattern.sub(catalog_dropdown_section, html_content)

    # Inject catalog JavaScript before the main script
    catalog_script = f"""
    <script>
{embedded_data_js}
{catalog_js}

    // Update catalog stats on load
    document.addEventListener('DOMContentLoaded', function() {{
        const stats = window.OpenAdaptCatalog.getStats();
        const statsEl = document.getElementById('catalog-stats');
        if (statsEl) {{
            statsEl.textContent = `${{stats.recording_count}} recordings • ${{stats.segmentation_count}} segmentations`;
        }}

        // Auto-load recording if specified
        const autoLoadId = '{auto_load_recording or ""}';
        if (autoLoadId) {{
            const recording = window.OpenAdaptCatalog.getRecording(autoLoadId);
            if (recording && recording.segmentations && recording.segmentations.length > 0) {{
                loadRecordingFromCatalog(autoLoadId);
            }}
        }}
    }});

    // Handle recording selection
    const recordingSelect = document.getElementById('recording-select');
    if (recordingSelect) {{
        recordingSelect.addEventListener('change', function() {{
            const recordingId = this.value;
            if (recordingId) {{
                loadRecordingFromCatalog(recordingId);
            }}
        }});
    }}

    // Handle refresh button
    const refreshBtn = document.getElementById('refresh-catalog-btn');
    if (refreshBtn) {{
        refreshBtn.addEventListener('click', function() {{
            alert('To refresh the catalog, run: openadapt-viewer catalog scan\\n\\nThen reload this page.');
        }});
    }}

    // Load recording from catalog
    async function loadRecordingFromCatalog(recordingId) {{
        const recording = window.OpenAdaptCatalog.getRecording(recordingId);
        if (!recording) {{
            alert('Recording not found in catalog');
            return;
        }}

        const segmentations = recording.segmentations;
        if (!segmentations || segmentations.length === 0) {{
            alert('No segmentation results found for this recording');
            return;
        }}

        // Use the most recent segmentation
        const latestSeg = segmentations[0];

        // For file:// protocol, we need to show a file picker or embed the data
        // For now, show instructions to the user
        const message = `To load this recording's segmentation results:\\n\\n` +
            `1. Click the file input button below\\n` +
            `2. Navigate to: ${{latestSeg.path}}\\n` +
            `3. Select the episodes JSON file\\n\\n` +
            `Or use: openadapt-viewer segmentation --auto-load ${{recordingId}}\\n` +
            `to generate a viewer with the data pre-embedded.`;

        // Better approach: Try to load via FileReader API if the browser supports it
        try {{
            // Check if we have pre-embedded data for this recording
            if (window.OPENADAPT_EMBEDDED_DATA && window.OPENADAPT_EMBEDDED_DATA[recordingId]) {{
                const data = window.OPENADAPT_EMBEDDED_DATA[recordingId];
                if (typeof loadAndDisplayData === 'function') {{
                    loadAndDisplayData(data);
                }}
                return;
            }}

            // Otherwise, prompt user to select the file
            alert(message);

            // Trigger the file input (if it still exists)
            const fileInput = document.getElementById('file-input');
            if (fileInput) {{
                fileInput.click();
            }}
        }} catch (error) {{
            alert('Error: ' + error.message);
            console.error('Load error:', error);
        }}
    }}
    </script>
"""

    # Insert catalog script before closing </body> tag
    html_content = html_content.replace('</body>', f'{catalog_script}</body>')

    # Write output file
    output_path = Path(output_path)
    with open(output_path, 'w') as f:
        f.write(html_content)

    return str(output_path.absolute())


if __name__ == "__main__":
    # Generate viewer with catalog integration
    output_path = generate_segmentation_viewer()
    print(f"Generated catalog-enabled segmentation viewer: {output_path}")
