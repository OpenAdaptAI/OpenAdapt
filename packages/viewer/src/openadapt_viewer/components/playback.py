"""Playback controls component for step-by-step navigation.

This component provides:
- Rewind/Previous/Play/Next/End buttons
- Playback speed selector
- Step counter display
- Keyboard shortcuts (via Alpine.js)
"""

from __future__ import annotations


def playback_controls(
    step_count: int = 1,
    initial_step: int = 0,
    speeds: list[float] | None = None,
    default_speed: float = 1.0,
    show_step_counter: bool = True,
    alpine_data_name: str = "playback",
    class_name: str = "",
) -> str:
    """Render playback controls for step navigation.

    Args:
        step_count: Total number of steps
        initial_step: Initial step index (0-based)
        speeds: Available playback speeds (default: [0.5, 1, 2, 4])
        default_speed: Default playback speed
        show_step_counter: Whether to show "Step X of Y" counter
        alpine_data_name: Alpine.js x-data variable name for binding
        class_name: Additional CSS classes

    Returns:
        HTML string for playback controls
    """
    speeds = speeds or [0.5, 1, 2, 4]
    extra_class = f" {class_name}" if class_name else ""

    # Generate speed options
    speed_options = "\n".join(
        f'<option value="{s}" {"selected" if s == default_speed else ""}>{s}x</option>'
        for s in speeds
    )

    # Step counter
    step_counter_html = ""
    if show_step_counter:
        step_counter_html = f'''
        <span class="oa-playback-counter"
              x-text="'Step ' + ({alpine_data_name}.currentStep + 1) + ' of ' + {step_count}">
            Step {initial_step + 1} of {step_count}
        </span>
        '''

    return f'''<div class="oa-playback-controls{extra_class}"
     x-data="{{
         currentStep: {initial_step},
         isPlaying: false,
         playbackSpeed: {default_speed},
         playbackInterval: null,
         totalSteps: {step_count},

         prevStep() {{
             if (this.currentStep > 0) this.currentStep--;
         }},
         nextStep() {{
             if (this.currentStep < this.totalSteps - 1) this.currentStep++;
         }},
         goToStart() {{
             this.currentStep = 0;
         }},
         goToEnd() {{
             this.currentStep = this.totalSteps - 1;
         }},
         togglePlayback() {{
             if (this.isPlaying) {{
                 this.stopPlayback();
             }} else {{
                 this.startPlayback();
             }}
         }},
         startPlayback() {{
             this.isPlaying = true;
             const interval = 1000 / this.playbackSpeed;
             this.playbackInterval = setInterval(() => {{
                 if (this.currentStep < this.totalSteps - 1) {{
                     this.currentStep++;
                 }} else {{
                     this.stopPlayback();
                 }}
             }}, interval);
         }},
         stopPlayback() {{
             this.isPlaying = false;
             if (this.playbackInterval) {{
                 clearInterval(this.playbackInterval);
                 this.playbackInterval = null;
             }}
         }}
     }}"
     @keydown.space.window.prevent="togglePlayback()"
     @keydown.left.window="prevStep()"
     @keydown.right.window="nextStep()"
     @keydown.home.window="goToStart()"
     @keydown.end.window="goToEnd()">

    <!-- Rewind -->
    <button class="oa-playback-btn" @click="goToStart()" :disabled="currentStep === 0" title="Go to start (Home)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/>
        </svg>
    </button>

    <!-- Previous -->
    <button class="oa-playback-btn" @click="prevStep()" :disabled="currentStep === 0" title="Previous step (Left arrow)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
        </svg>
    </button>

    <!-- Play/Pause -->
    <button class="oa-playback-btn oa-playback-btn-primary" @click="togglePlayback()" title="Play/Pause (Space)">
        <svg x-show="!isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M8 5v14l11-7z"/>
        </svg>
        <svg x-show="isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
        </svg>
    </button>

    <!-- Next -->
    <button class="oa-playback-btn" @click="nextStep()" :disabled="currentStep >= totalSteps - 1" title="Next step (Right arrow)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
        </svg>
    </button>

    <!-- End -->
    <button class="oa-playback-btn" @click="goToEnd()" :disabled="currentStep >= totalSteps - 1" title="Go to end (End)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M6 18l8.5-6L6 6v12zm2-12v12h2V6h-2z" transform="scale(-1, 1) translate(-24, 0)"/>
        </svg>
    </button>

    <!-- Step Counter -->
    {step_counter_html}

    <!-- Speed Selector -->
    <select class="oa-playback-speed"
            x-model.number="playbackSpeed"
            @change="if (isPlaying) {{ stopPlayback(); startPlayback(); }}"
            title="Playback speed">
        {speed_options}
    </select>
</div>'''
