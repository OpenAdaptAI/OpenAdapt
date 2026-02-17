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
from openadapt_capture.config import RecordingConfig
from openadapt_capture.db.models import (
    ActionEvent as DBActionEvent,
)

# Database models (low-level)
from openadapt_capture.db.models import (
    Recording,
    Screenshot,
)
from openadapt_capture.db.models import (
    WindowEvent as DBWindowEvent,
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

# Recorder requires pynput which needs a display server (X11/Wayland/macOS/Windows).
# Make it optional so the package is importable in headless environments (CI, servers).
try:
    from openadapt_capture.recorder import Recorder
except ImportError:
    Recorder = None  # type: ignore[assignment,misc]

# Performance statistics
from openadapt_capture.stats import (
    CaptureStats,
    PerfStat,
    plot_capture_performance,
)

# Visualization
from openadapt_capture.visualize import create_demo, create_html

# Browser events and bridge (optional - requires websockets)
try:
    from openadapt_capture.browser_bridge import (
        BrowserBridge,
        BrowserEventRecord,
        BrowserMode,
        run_browser_bridge,
    )
    from openadapt_capture.browser_events import (
        BoundingBox,
        BrowserClickEvent,
        BrowserEvent,
        BrowserEventType,
        BrowserFocusEvent,
        BrowserInputEvent,
        BrowserKeyEvent,
        BrowserNavigationEvent,
        BrowserScrollEvent,
        DOMSnapshot,
        ElementState,
        NavigationType,
        SemanticElementRef,
        VisibleElement,
    )
    _BROWSER_BRIDGE_AVAILABLE = True
except ImportError:
    _BROWSER_BRIDGE_AVAILABLE = False

__all__ = [
    # Version
    "__version__",
    # High-level APIs
    "Recorder",
    "RecordingConfig",
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
    # Database models (low-level)
    "Recording",
    "DBActionEvent",
    "Screenshot",
    "DBWindowEvent",
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
    # Browser bridge (optional)
    "_BROWSER_BRIDGE_AVAILABLE",
    "BrowserBridge",
    "BrowserMode",
    "BrowserEventRecord",
    "run_browser_bridge",
    # Browser events
    "BrowserEventType",
    "BrowserEvent",
    "BrowserClickEvent",
    "BrowserKeyEvent",
    "BrowserScrollEvent",
    "BrowserInputEvent",
    "BrowserNavigationEvent",
    "BrowserFocusEvent",
    "SemanticElementRef",
    "BoundingBox",
    "ElementState",
    "DOMSnapshot",
    "VisibleElement",
    "NavigationType",
]
