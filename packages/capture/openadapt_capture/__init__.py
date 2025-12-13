"""OpenAdapt Capture - GUI interaction capture.

Platform-agnostic event streams with time-aligned media.
"""

__version__ = "0.1.0"

# High-level APIs (primary interface)
from openadapt_capture.capture import Action, Capture, CaptureSession

# Frame comparison utilities
from openadapt_capture.comparison import (
    ComparisonReport,
    FrameComparison,
    compare_frames,
    compare_video_to_images,
    plot_comparison,
)

# Event types
from openadapt_capture.events import (
    ActionEvent,
    AudioChunkEvent,
    AudioEvent,
    BaseEvent,
    Event,
    EventType,
    KeyDownEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
    ScreenEvent,
    ScreenFrameEvent,
)

# Event processing
from openadapt_capture.processing import (
    detect_drag_events,
    get_action_events,
    get_audio_events,
    get_screen_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)
from openadapt_capture.recorder import Recorder

# Performance statistics
from openadapt_capture.stats import (
    CaptureStats,
    PerfStat,
    plot_capture_performance,
)
from openadapt_capture.storage import Capture as CaptureMetadata

# Storage (low-level)
from openadapt_capture.storage import (
    CaptureStorage,
    Stream,
    create_capture,
    load_capture,
)

# Visualization
from openadapt_capture.visualize import create_demo, create_html

__all__ = [
    # Version
    "__version__",
    # High-level APIs
    "Recorder",
    "Capture",
    "CaptureSession",
    "Action",
    # Event types
    "EventType",
    "MouseButton",
    "BaseEvent",
    "Event",
    "ActionEvent",
    "ScreenEvent",
    "AudioEvent",
    # Mouse events
    "MouseMoveEvent",
    "MouseDownEvent",
    "MouseUpEvent",
    "MouseScrollEvent",
    "MouseClickEvent",
    "MouseDoubleClickEvent",
    "MouseDragEvent",
    # Keyboard events
    "KeyDownEvent",
    "KeyUpEvent",
    "KeyTypeEvent",
    # Screen/audio events
    "ScreenFrameEvent",
    "AudioChunkEvent",
    # Storage (low-level)
    "CaptureMetadata",
    "Stream",
    "CaptureStorage",
    "create_capture",
    "load_capture",
    # Processing
    "process_events",
    "remove_invalid_keyboard_events",
    "remove_redundant_mouse_move_events",
    "merge_consecutive_keyboard_events",
    "merge_consecutive_mouse_move_events",
    "merge_consecutive_mouse_scroll_events",
    "merge_consecutive_mouse_click_events",
    "detect_drag_events",
    "get_action_events",
    "get_screen_events",
    "get_audio_events",
    # Performance statistics
    "CaptureStats",
    "PerfStat",
    "plot_capture_performance",
    # Frame comparison
    "ComparisonReport",
    "FrameComparison",
    "compare_frames",
    "compare_video_to_images",
    "plot_comparison",
    # Visualization
    "create_demo",
    "create_html",
]
