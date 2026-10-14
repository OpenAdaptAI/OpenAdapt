"""Timeline component for step progress visualization.

This component provides:
- Progress bar showing current position
- Click-to-seek functionality
- Optional step markers
"""

from __future__ import annotations


def timeline(
    step_count: int = 1,
    current_step: int = 0,
    step_labels: list[str] | None = None,
    clickable: bool = True,
    show_markers: bool = False,
    alpine_data_name: str = "playback",
    class_name: str = "",
) -> str:
    """Render a timeline progress bar.

    Args:
        step_count: Total number of steps
        current_step: Current step index (0-based)
        step_labels: Optional labels for start/end markers
        clickable: Whether clicking seeks to that position
        show_markers: Whether to show start/end markers
        alpine_data_name: Alpine.js x-data variable name for binding
        class_name: Additional CSS classes

    Returns:
        HTML string for the timeline
    """
    extra_class = f" {class_name}" if class_name else ""

    # Calculate progress percentage
    progress = ((current_step + 1) / step_count * 100) if step_count > 0 else 0

    # Click handler for seeking
    click_handler = ""
    if clickable:
        click_handler = f'''@click="(e) => {{
            const rect = $el.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const percent = clickX / rect.width;
            {alpine_data_name}.currentStep = Math.floor(percent * {alpine_data_name}.totalSteps);
            if ({alpine_data_name}.currentStep >= {alpine_data_name}.totalSteps) {{
                {alpine_data_name}.currentStep = {alpine_data_name}.totalSteps - 1;
            }}
        }}"'''

    # Markers
    markers_html = ""
    if show_markers:
        start_label = step_labels[0] if step_labels and len(step_labels) > 0 else "1"
        end_label = step_labels[-1] if step_labels and len(step_labels) > 1 else str(step_count)
        markers_html = f'''
        <div class="oa-timeline-markers">
            <span>{start_label}</span>
            <span>{end_label}</span>
        </div>
        '''

    return f'''<div class="oa-timeline{extra_class}">
    <div class="oa-timeline-track" {click_handler}
         style="cursor: {'pointer' if clickable else 'default'};">
        <div class="oa-timeline-progress"
             :style="'width: ' + (({alpine_data_name}.currentStep + 1) / {alpine_data_name}.totalSteps * 100) + '%'"
             style="width: {progress}%;"></div>
    </div>
    {markers_html}
</div>'''
