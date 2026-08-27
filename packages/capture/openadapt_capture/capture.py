"""High-level capture loading and iteration API.

Provides time-aligned access to captured events with associated screenshots.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from openadapt_capture.browser_events import (
    BoundingBox,
    BrowserClickEvent,
    BrowserEventType,
    BrowserFocusEvent,
    BrowserInputEvent,
    BrowserKeyEvent,
    BrowserMouseMoveEvent,
    BrowserNavigationEvent,
    BrowserScrollEvent,
    ElementState,
    NavigationType,
    SemanticElementRef,
)
from openadapt_capture.events import (
    ActionEvent as PydanticActionEvent,
)
from openadapt_capture.events import (
    CapturedWindowEvent,
    KeyDownEvent,
    KeyShortcutEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseDownEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
    WindowCaptureStateV2,
)
from openadapt_capture.processing import process_events
from openadapt_capture.structural import StructuralObservation

if TYPE_CHECKING:
    from PIL import Image

    from openadapt_capture.browser_events import BrowserEvent


class InvalidCaptureEvent(ValueError):
    """A stored input event lacks the data required for deterministic replay."""


@dataclass(frozen=True)
class CapturedFrame:
    """Public identity of one retained source frame."""

    timestamp: float
    source_ordinal: int | None
    png_sha256: str | None


def _parse_structural_observation(raw: object) -> StructuralObservation | None:
    """Load optional structural evidence without breaking legacy recordings."""

    if raw is None:
        return None
    try:
        return StructuralObservation.model_validate(raw)
    except Exception:
        return None


def _required_event_value(db_event, field: str):
    value = getattr(db_event, field, None)
    if value is None:
        raise InvalidCaptureEvent(
            f"stored {getattr(db_event, 'name', 'unknown')!r} event is missing {field!r}"
        )
    return value


def _required_nonempty_string(db_event, field: str) -> str:
    value = _required_event_value(db_event, field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidCaptureEvent(
            f"stored {getattr(db_event, 'name', 'unknown')!r} event has invalid {field!r}"
        )
    return value


def _require_key_identity(db_event) -> None:
    fields = (
        "key_name",
        "key_char",
        "key_vk",
        "canonical_key_name",
        "canonical_key_char",
        "canonical_key_vk",
    )
    if not any(getattr(db_event, field, None) not in (None, "") for field in fields):
        raise InvalidCaptureEvent(
            f"stored {getattr(db_event, 'name', 'unknown')!r} event has no key identity"
        )


def _convert_action_event(db_event) -> PydanticActionEvent:
    """Convert a SQLAlchemy ActionEvent to a Pydantic event.

    Args:
        db_event: SQLAlchemy ActionEvent instance.

    Returns:
        A validated Pydantic event.

    Raises:
        InvalidCaptureEvent: If the stored event is unknown or lacks data that
            deterministic replay requires. Missing coordinates are never
            replaced with zero because zero is a valid screen position.
    """
    common = {
        "timestamp": db_event.timestamp,
        "source_ordinal": getattr(db_event, "source_ordinal", None),
        "structural_observation": _parse_structural_observation(
            getattr(db_event, "structural_observation", None)
        ),
        "screenshot_timestamp": getattr(db_event, "screenshot_timestamp", None),
        "screenshot_source_ordinal": getattr(db_event, "screenshot_source_ordinal", None),
        "after_screenshot_timestamp": getattr(
            db_event, "after_screenshot_timestamp", None
        ),
        "after_screenshot_source_ordinal": getattr(
            db_event, "after_screenshot_source_ordinal", None
        ),
        "window_event_timestamp": getattr(db_event, "window_event_timestamp", None),
        "window_event_source_ordinal": getattr(db_event, "window_event_source_ordinal", None),
        "after_window_event_timestamp": getattr(
            db_event, "after_window_event_timestamp", None
        ),
        "after_window_event_source_ordinal": getattr(
            db_event, "after_window_event_source_ordinal", None
        ),
        "window_geometry_generation": getattr(db_event, "window_geometry_generation", None),
        "after_window_geometry_generation": getattr(
            db_event, "after_window_geometry_generation", None
        ),
    }

    if db_event.name == "move":
        return MouseMoveEvent(
            **common,
            x=_required_event_value(db_event, "mouse_x"),
            y=_required_event_value(db_event, "mouse_y"),
        )
    elif db_event.name == "click":
        button = _required_nonempty_string(db_event, "mouse_button_name")

        if db_event.mouse_pressed is True:
            return MouseDownEvent(
                **common,
                x=_required_event_value(db_event, "mouse_x"),
                y=_required_event_value(db_event, "mouse_y"),
                button=button,
            )
        elif db_event.mouse_pressed is False:
            return MouseUpEvent(
                **common,
                x=_required_event_value(db_event, "mouse_x"),
                y=_required_event_value(db_event, "mouse_y"),
                button=button,
            )
        else:
            raise InvalidCaptureEvent("stored 'click' event has no pressed/released state")
    elif db_event.name == "scroll":
        return MouseScrollEvent(
            **common,
            x=_required_event_value(db_event, "mouse_x"),
            y=_required_event_value(db_event, "mouse_y"),
            dx=_required_event_value(db_event, "mouse_dx"),
            dy=_required_event_value(db_event, "mouse_dy"),
        )
    elif db_event.name == "press":
        _require_key_identity(db_event)
        return KeyDownEvent(
            **common,
            key_name=db_event.key_name,
            key_char=db_event.key_char,
            key_vk=db_event.key_vk,
            canonical_key_name=db_event.canonical_key_name,
            canonical_key_char=db_event.canonical_key_char,
            canonical_key_vk=db_event.canonical_key_vk,
        )
    elif db_event.name == "release":
        _require_key_identity(db_event)
        return KeyUpEvent(
            **common,
            key_name=db_event.key_name,
            key_char=db_event.key_char,
            key_vk=db_event.key_vk,
            canonical_key_name=db_event.canonical_key_name,
            canonical_key_char=db_event.canonical_key_char,
            canonical_key_vk=db_event.canonical_key_vk,
        )
    raise InvalidCaptureEvent(f"unknown stored action event name {db_event.name!r}")


def _parse_element_ref(raw: dict | None) -> SemanticElementRef | None:
    """Parse a raw element dict into a SemanticElementRef.

    Handles field name variations between the content-script format
    (e.g. ``dataId``, ``tagName``, ``classList``) and snake_case alternatives.
    """
    if not raw or not isinstance(raw, dict):
        return None

    bbox_raw = raw.get("bbox", {})
    bbox = BoundingBox(
        x=bbox_raw.get("x", 0),
        y=bbox_raw.get("y", 0),
        width=bbox_raw.get("width", 0),
        height=bbox_raw.get("height", 0),
    )

    state_raw = raw.get("state", {})
    state = (
        ElementState(
            enabled=state_raw.get("enabled", True),
            focused=state_raw.get("focused", False),
            visible=state_raw.get("visible", True),
            checked=state_raw.get("checked"),
            selected=state_raw.get("selected"),
            expanded=state_raw.get("expanded"),
            value=state_raw.get("value"),
        )
        if isinstance(state_raw, dict)
        else ElementState()
    )

    return SemanticElementRef(
        role=raw.get("role") or "",
        name=raw.get("name") or "",
        bbox=bbox,
        xpath=raw.get("xpath") or raw.get("dataId") or "",
        css_selector=raw.get("cssSelector") or raw.get("css_selector") or "",
        state=state,
        tag_name=raw.get("tagName") or raw.get("tag_name") or "",
        id=raw.get("id"),
        class_list=raw.get("classList") or raw.get("class_list") or [],
    )


def _convert_browser_event(db_event) -> "BrowserEvent | None":
    """Convert a SQLAlchemy BrowserEvent to a typed Pydantic browser event.

    The DB stores browser events as JSON in the `message` field.  The recorder
    wraps each raw WebSocket message as ``{"message": <raw_event>}``.

    Handles both flat (content-script) and payload-wrapped message formats.

    Args:
        db_event: SQLAlchemy BrowserEvent instance.

    Returns:
        Typed browser event or None if parsing fails.
    """
    msg = db_event.message
    if not isinstance(msg, dict):
        return None

    # Unwrap the recorder's {"message": <raw>} wrapper
    inner = msg.get("message", msg)
    if not isinstance(inner, dict):
        return None

    # Support both flat (content-script) and payload-wrapped (browser_bridge) formats
    payload = inner.get("payload", inner)

    raw_type = payload.get("eventType", inner.get("eventType", ""))
    try:
        event_type = BrowserEventType(raw_type)
    except ValueError:
        return None

    timestamp = db_event.timestamp or 0
    url = payload.get("url", inner.get("url", ""))
    tab_id = inner.get("tabId", payload.get("tab_id", 0))

    try:
        if event_type == BrowserEventType.CLICK:
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserClickEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                client_x=payload.get("clientX", 0),
                client_y=payload.get("clientY", 0),
                page_x=payload.get("pageX", payload.get("clientX", 0)),
                page_y=payload.get("pageY", payload.get("clientY", 0)),
                button=payload.get("button", 0),
                click_count=payload.get("clickCount", 1),
                element=elem,
            )
        elif event_type in (BrowserEventType.KEYDOWN, BrowserEventType.KEYUP):
            element = _parse_element_ref(payload.get("element"))
            return BrowserKeyEvent(
                timestamp=timestamp,
                type=event_type,
                url=url,
                tab_id=tab_id,
                key=payload.get("key", ""),
                code=payload.get("code", ""),
                key_code=payload.get("keyCode", 0),
                shift_key=payload.get("shiftKey", False),
                ctrl_key=payload.get("ctrlKey", False),
                alt_key=payload.get("altKey", False),
                meta_key=payload.get("metaKey", False),
                element=element,
            )
        elif event_type == BrowserEventType.SCROLL:
            return BrowserScrollEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                scroll_x=payload.get("scrollX", 0),
                scroll_y=payload.get("scrollY", 0),
                delta_x=payload.get("deltaX", payload.get("scrollDeltaX", 0)),
                delta_y=payload.get("deltaY", payload.get("scrollDeltaY", 0)),
            )
        elif event_type == BrowserEventType.INPUT:
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserInputEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                input_type=payload.get("inputType", ""),
                data=payload.get("data"),
                value=payload.get("value", ""),
                element=elem,
            )
        elif event_type == BrowserEventType.NAVIGATE:
            nav_type = payload.get("navigationType", "link")
            valid = [e.value for e in NavigationType]
            return BrowserNavigationEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                previous_url=payload.get("previousUrl", ""),
                navigation_type=(
                    NavigationType(nav_type) if nav_type in valid else NavigationType.LINK
                ),
            )
        elif event_type == BrowserEventType.MOUSEMOVE:
            element = _parse_element_ref(payload.get("element"))
            return BrowserMouseMoveEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                client_x=payload.get("clientX", 0),
                client_y=payload.get("clientY", 0),
                screen_x=payload.get("screenX", 0),
                screen_y=payload.get("screenY", 0),
                element=element,
            )
        elif event_type in (BrowserEventType.FOCUS, BrowserEventType.BLUR):
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserFocusEvent(
                timestamp=timestamp,
                type=event_type,
                url=url,
                tab_id=tab_id,
                element=elem,
            )
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("Failed to parse browser event: %s", e)
    return None


@dataclass
class Action:
    """A processed action event with associated screenshot.

    Represents a user action (click, type, drag, etc.) along with
    the screen state at the time of the action.
    """

    event: PydanticActionEvent
    _capture: "CaptureSession"

    @property
    def timestamp(self) -> float:
        """Unix timestamp of the action."""
        return self.event.timestamp

    @property
    def source_ordinal(self) -> int | None:
        """Return the action's terminal primitive source-journal position."""
        return self.event.source_ordinal

    @property
    def type(self) -> str:
        """Action type (e.g., 'mouse.singleclick', 'key.type')."""
        return self.event.type if isinstance(self.event.type, str) else self.event.type.value

    @property
    def x(self) -> float | None:
        """X coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "x"):
            return self.event.x
        return None

    @property
    def y(self) -> float | None:
        """Y coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "y"):
            return self.event.y
        return None

    @property
    def text(self) -> str | None:
        """Typed text for keyboard actions."""
        if isinstance(self.event, KeyTypeEvent):
            return self.event.text
        return None

    @property
    def keys(self) -> list[str] | None:
        """Key names for keyboard actions (useful when text is empty).

        Returns list of key names like ['ctrl', 'space'] or ['enter'].
        """
        if isinstance(self.event, KeyShortcutEvent):
            return [*self.event.modifiers, self.event.key]
        if isinstance(self.event, KeyTypeEvent):
            key_names = []
            seen = set()
            for child in self.event.children:
                if isinstance(child, KeyDownEvent):
                    # Get key identifier
                    key_id = (
                        child.canonical_key_name
                        or child.key_name
                        or child.canonical_key_char
                        or child.key_char
                        or child.canonical_key_vk
                        or child.key_vk
                    )
                    if key_id and key_id not in seen:
                        seen.add(key_id)
                        key_names.append(key_id)
            return key_names if key_names else None
        return None

    @property
    def dx(self) -> float | None:
        """Horizontal displacement for scroll/drag actions."""
        if hasattr(self.event, "dx"):
            return self.event.dx
        return None

    @property
    def dy(self) -> float | None:
        """Vertical displacement for scroll/drag actions."""
        if hasattr(self.event, "dy"):
            return self.event.dy
        return None

    @property
    def button(self) -> str | None:
        """Mouse button for click/drag actions."""
        if hasattr(self.event, "button"):
            btn = self.event.button
            return btn.value if hasattr(btn, "value") else str(btn)
        return None

    @property
    def structural_observation(self) -> StructuralObservation | None:
        """Accessibility evidence captured at this action, when available."""
        return self.event.structural_observation

    @property
    def screenshot_timestamp(self) -> float | None:
        """Exact retained frame timestamp bound to this action."""
        return self.event.screenshot_timestamp

    @property
    def screenshot_source_ordinal(self) -> int | None:
        """Return the exact source-journal position of the bound frame."""
        return self.event.screenshot_source_ordinal

    @property
    def after_screenshot_timestamp(self) -> float | None:
        """Exact retained after-frame timestamp bound to this action."""
        return self.event.after_screenshot_timestamp

    @property
    def after_screenshot_source_ordinal(self) -> int | None:
        """Return the source-journal position of the retained after frame."""
        return self.event.after_screenshot_source_ordinal

    @property
    def window_event_timestamp(self) -> float | None:
        """Exact atomic WindowEvent timestamp bound to this action."""
        return self.event.window_event_timestamp

    @property
    def window_event_source_ordinal(self) -> int | None:
        """Return the source-journal position of the bound native geometry."""
        return self.event.window_event_source_ordinal

    @property
    def after_window_event_timestamp(self) -> float | None:
        """Exact native geometry timestamp paired with the after frame."""
        return self.event.after_window_event_timestamp

    @property
    def after_window_event_source_ordinal(self) -> int | None:
        """Return the geometry source position paired with the after frame."""
        return self.event.after_window_event_source_ordinal

    @property
    def window_geometry_generation(self) -> int | None:
        """Exact native geometry generation bound to this action."""
        return self.event.window_geometry_generation

    @property
    def after_window_geometry_generation(self) -> int | None:
        """Exact native geometry generation paired with the after frame."""
        return self.event.after_window_geometry_generation

    @property
    def screenshot(self) -> "Image" | None:
        """Get the exact retained screen frame this action is bound to.

        The action's ``screenshot_timestamp`` names the one frame the recorder
        retained as this action's evidence; it is decoded exactly or this
        raises. Actions recorded before frame binding existed fall back to a
        nearest-frame lookup at the action timestamp.

        Returns:
            PIL Image of the bound frame.

        Raises:
            LookupError: If the bound frame cannot be resolved exactly.
        """
        bound = getattr(self.event, "screenshot_timestamp", None)
        if bound is not None:
            return self._capture.get_exact_frame(
                bound,
                source_ordinal=getattr(self.event, "screenshot_source_ordinal", None),
            )
        return self._capture.get_frame_at(self.timestamp)

    @property
    def after_screenshot(self) -> "Image" | None:
        """Get the exact first retained frame after this action."""
        bound = getattr(self.event, "after_screenshot_timestamp", None)
        if bound is None:
            return None
        return self._capture.get_exact_frame(
            bound,
            source_ordinal=getattr(
                self.event,
                "after_screenshot_source_ordinal",
                None,
            ),
        )


def _source_order(rows: list) -> list:
    """Order current journal rows by ordinal and legacy rows by timestamp."""
    return sorted(
        rows,
        key=lambda row: (
            getattr(row, "source_ordinal", None) is None,
            getattr(row, "source_ordinal", None)
            if getattr(row, "source_ordinal", None) is not None
            else row.timestamp,
        ),
    )


def _validate_database_contract(
    capture: "CaptureSession",
    *,
    event_counts: dict[str, int],
    last_source_ordinal: int | None,
) -> None:
    """Validate the sealed terminal claims against one immutable database."""
    recording = capture._recording
    rows_by_kind = {
        "action": list(recording.action_events),
        "screen": list(recording.screenshots),
        "window": list(recording.window_events),
        "browser": list(recording.browser_events),
    }
    actual_counts = {kind: len(rows) for kind, rows in rows_by_kind.items()}
    for kind, actual in actual_counts.items():
        if event_counts.get(kind) != actual:
            raise InvalidCaptureEvent(
                f"sealed {kind} count {event_counts.get(kind)!r} differs from database {actual}"
            )
    for event in rows_by_kind["browser"]:
        if _convert_browser_event(event) is None:
            raise InvalidCaptureEvent("sealed capture has an invalid browser event")

    ordinals_by_kind: dict[str, set[int]] = {}
    all_ordinals: list[int] = []
    for kind, rows in rows_by_kind.items():
        ordinals = [getattr(row, "source_ordinal", None) for row in rows]
        if any(ordinal is None or ordinal <= 0 for ordinal in ordinals):
            raise InvalidCaptureEvent(f"sealed {kind} rows have missing source ordinals")
        typed_ordinals = [int(ordinal) for ordinal in ordinals]
        if len(typed_ordinals) != len(set(typed_ordinals)):
            raise InvalidCaptureEvent(f"sealed {kind} rows reuse a source ordinal")
        ordinals_by_kind[kind] = set(typed_ordinals)
        all_ordinals.extend(typed_ordinals)

    calculated_last = max(all_ordinals, default=None)
    if calculated_last != last_source_ordinal:
        raise InvalidCaptureEvent("sealed last source ordinal differs from the immutable database")

    screenshots_by_ordinal = {
        row.source_ordinal: row for row in rows_by_kind["screen"]
    }
    windows_rows_by_ordinal = {
        row.source_ordinal: row for row in rows_by_kind["window"]
    }
    for event in rows_by_kind["action"]:
        _convert_action_event(event)
        if event.screenshot_id is None or event.screenshot is None:
            raise InvalidCaptureEvent("sealed action has no retained screenshot relationship")
        if (
            event.screenshot_source_ordinal != event.screenshot.source_ordinal
            or event.screenshot_timestamp != event.screenshot.timestamp
            or event.source_ordinal <= event.screenshot_source_ordinal
        ):
            raise InvalidCaptureEvent("sealed action screenshot relationship is inconsistent")
        if event.window_event_source_ordinal is not None:
            if event.window_event_id is None or event.window_event is None:
                raise InvalidCaptureEvent("sealed action has no retained window relationship")
            if (
                event.window_event_source_ordinal != event.window_event.source_ordinal
                or event.window_event_timestamp != event.window_event.timestamp
            ):
                raise InvalidCaptureEvent("sealed action window relationship is inconsistent")
        if event.after_screenshot_source_ordinal is not None:
            after_screenshot = screenshots_by_ordinal.get(
                event.after_screenshot_source_ordinal
            )
            if (
                after_screenshot is None
                or event.after_screenshot_timestamp != after_screenshot.timestamp
                or event.source_ordinal >= event.after_screenshot_source_ordinal
            ):
                raise InvalidCaptureEvent(
                    "sealed action after-screenshot binding is inconsistent"
                )
        if event.after_window_event_source_ordinal is not None:
            after_window = windows_rows_by_ordinal.get(
                event.after_window_event_source_ordinal
            )
            if (
                after_window is None
                or event.after_window_event_timestamp != after_window.timestamp
            ):
                raise InvalidCaptureEvent(
                    "sealed action after-window binding is inconsistent"
                )
    retained_frame_sizes: dict[int, tuple[int, int]] = {}
    for screenshot in rows_by_kind["screen"]:
        if screenshot.png_data:
            from PIL import Image

            retained_sha256 = hashlib.sha256(screenshot.png_data).hexdigest()
            if screenshot.png_sha256 != retained_sha256:
                raise InvalidCaptureEvent(
                    "sealed frame PNG digest differs from its retained bytes"
                )
            try:
                with Image.open(io.BytesIO(screenshot.png_data)) as retained:
                    retained.load()
                    retained_frame_sizes[int(screenshot.source_ordinal)] = retained.size
            except Exception as exc:
                raise InvalidCaptureEvent("sealed frame PNG is invalid") from exc
        elif screenshot.png_sha256 is not None:
            raise InvalidCaptureEvent(
                "sealed frame has a PNG digest without retained PNG bytes"
            )

    metadata = capture.window_capture
    is_v2 = (
        isinstance(metadata, dict)
        and metadata.get("schema_version") == "openadapt.capture.window-scoped/v2"
    )
    desktop_metadata = capture.desktop_capture
    is_desktop_v1 = (
        isinstance(desktop_metadata, dict)
        and desktop_metadata.get("schema_version")
        == "openadapt.capture.display-topology/v1"
        and desktop_metadata.get("coordinate_space") == "virtual_desktop_pixels"
    )
    is_native_scoped = is_v2 or is_desktop_v1
    if is_native_scoped and not rows_by_kind["screen"]:
        raise InvalidCaptureEvent("sealed native capture has no retained frames")
    if is_desktop_v1:
        viewport = desktop_metadata.get("viewport")
        if (
            not isinstance(viewport, list)
            or len(viewport) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in viewport)
            or any(value <= 0 for value in viewport)
        ):
            raise InvalidCaptureEvent("sealed desktop capture has an invalid viewport")
        expected_size = tuple(viewport)
        if any(size != expected_size for size in retained_frame_sizes.values()):
            raise InvalidCaptureEvent(
                "sealed desktop frame dimensions differ from its capture viewport"
            )

    if is_native_scoped:
        frame_ordinals = set(screenshots_by_ordinal)
        for action in rows_by_kind["action"]:
            if action.after_screenshot_source_ordinal is None:
                raise InvalidCaptureEvent(
                    "sealed native action has an incomplete before/after frame binding"
                )
            before_candidates = [
                ordinal for ordinal in frame_ordinals if ordinal < action.source_ordinal
            ]
            after_candidates = [
                ordinal for ordinal in frame_ordinals if ordinal > action.source_ordinal
            ]
            if (
                not before_candidates
                or action.screenshot_source_ordinal != max(before_candidates)
            ):
                raise InvalidCaptureEvent(
                    "sealed native action does not bind its nearest retained before frame"
                )
            if (
                not after_candidates
                or action.after_screenshot_source_ordinal != min(after_candidates)
            ):
                raise InvalidCaptureEvent(
                    "sealed native action does not bind its first retained after frame"
                )

    allowed_pair_ordinals: set[int] = set()
    if is_v2:
        window_events = capture.window_capture_events_v2()
        windows_by_ordinal = {event.source_ordinal: event for event in window_events}
        screenshots_by_ordinal = {row.source_ordinal: row for row in rows_by_kind["screen"]}
        if set(windows_by_ordinal) != set(screenshots_by_ordinal):
            raise InvalidCaptureEvent("sealed v2 frame/window pairs are not a bijection")
        allowed_pair_ordinals = set(windows_by_ordinal)
        identity = None
        topology_sha256 = None
        generation_to_epoch: dict[int, str] = {}
        previous_generation = 0
        for ordinal in sorted(allowed_pair_ordinals):
            window_event = windows_by_ordinal[ordinal]
            state = window_event.window_capture_v2
            assert state is not None
            screenshot = screenshots_by_ordinal[ordinal]
            if screenshot.timestamp != window_event.timestamp:
                raise InvalidCaptureEvent("sealed v2 frame and geometry timestamps differ")
            retained_size = retained_frame_sizes.get(int(screenshot.source_ordinal))
            if retained_size is not None and retained_size != state.viewport:
                raise InvalidCaptureEvent(
                    "sealed v2 frame dimensions differ from its geometry viewport"
                )
            current_identity = (
                state.window_id,
                state.pid,
                state.process_start_time,
                state.owner.casefold(),
            )
            if identity is None:
                identity = current_identity
                topology_sha256 = state.display_topology_sha256
            elif current_identity != identity:
                raise InvalidCaptureEvent("sealed v2 capture changed window identity")
            if state.display_topology_sha256 != topology_sha256:
                raise InvalidCaptureEvent("sealed v2 capture changed display topology")
            known_epoch = generation_to_epoch.setdefault(
                state.geometry_generation,
                state.geometry_epoch_sha256,
            )
            if known_epoch != state.geometry_epoch_sha256:
                raise InvalidCaptureEvent("one v2 generation names different geometry epochs")
            if state.geometry_generation < previous_generation:
                raise InvalidCaptureEvent("sealed v2 geometry generations move backwards")
            previous_generation = state.geometry_generation
        for action in rows_by_kind["action"]:
            if (
                action.window_geometry_generation is None
                or action.screenshot_source_ordinal != action.window_event_source_ordinal
                or action.after_screenshot_source_ordinal is None
                or action.after_window_event_source_ordinal is None
                or action.after_screenshot_source_ordinal
                != action.after_window_event_source_ordinal
                or action.after_screenshot_timestamp
                != action.after_window_event_timestamp
                or action.after_window_geometry_generation is None
            ):
                raise InvalidCaptureEvent(
                    "sealed v2 action has an incomplete before/after frame binding"
                )
            bound = windows_by_ordinal.get(action.window_event_source_ordinal)
            if bound is None or (
                bound.window_capture_v2.geometry_generation != action.window_geometry_generation
            ):
                raise InvalidCaptureEvent("sealed v2 action names the wrong geometry epoch")
            after_bound = windows_by_ordinal.get(
                action.after_window_event_source_ordinal
            )
            if after_bound is None or (
                after_bound.window_capture_v2.geometry_generation
                != action.after_window_geometry_generation
            ):
                raise InvalidCaptureEvent(
                    "sealed v2 action names the wrong after-frame geometry epoch"
                )
    owners: dict[int, set[str]] = {}
    for kind, ordinals in ordinals_by_kind.items():
        for ordinal in ordinals:
            owners.setdefault(ordinal, set()).add(kind)
    for ordinal, kinds in owners.items():
        if len(kinds) > 1 and not (
            ordinal in allowed_pair_ordinals and kinds == {"screen", "window"}
        ):
            raise InvalidCaptureEvent("sealed journal reuses a source ordinal across events")
    if is_native_scoped and sorted(owners) != list(
        range(1, (last_source_ordinal or 0) + 1)
    ):
        raise InvalidCaptureEvent("sealed native source journal has a missing ordinal")

    expected_video_count = event_counts.get("video")
    if not isinstance(expected_video_count, int) or expected_video_count < 0:
        raise InvalidCaptureEvent("sealed video count is invalid")
    if (
        is_native_scoped
        and not expected_video_count
        and any(not screenshot.png_data for screenshot in rows_by_kind["screen"])
    ):
        raise InvalidCaptureEvent(
            "sealed native frame has no retained PNG or exact MP4 carrier"
        )
    if expected_video_count:
        from openadapt_capture.video import _read_timing_metadata

        video_path = capture.video_path
        if video_path is None:
            raise InvalidCaptureEvent("sealed capture claims video frames without an MP4")
        timing = _read_timing_metadata(video_path)
        if timing is None or timing[3] is None:
            raise InvalidCaptureEvent("sealed MP4 has no source-ordinal frame bindings")
        if len(timing[3]) != expected_video_count:
            raise InvalidCaptureEvent("sealed video count differs from its MP4 bindings")
        if is_native_scoped:
            video_source_ordinals = [source_ordinal for _, source_ordinal in timing[3]]
            expected_source_ordinals = sorted(ordinals_by_kind["screen"])
            if video_source_ordinals != expected_source_ordinals:
                raise InvalidCaptureEvent(
                    "sealed native MP4 source bindings differ from retained frame ordinals"
                )
            capture_bindings = timing[2]
            if capture_bindings is None:
                raise InvalidCaptureEvent(
                    "sealed native MP4 has no exact capture-time frame bindings"
                )
            screenshots_by_ordinal = {
                int(row.source_ordinal): row for row in rows_by_kind["screen"]
            }
            expected_capture_bindings = [
                (encoded_index, screenshots_by_ordinal[source_ordinal].timestamp)
                for encoded_index, source_ordinal in timing[3]
            ]
            if capture_bindings != expected_capture_bindings:
                raise InvalidCaptureEvent(
                    "sealed native MP4 source and capture-time bindings differ "
                    "from the retained frame"
                )
    elif capture.video_path is not None:
        raise InvalidCaptureEvent("sealed capture inventories an MP4 but claims no video frames")

    list(capture.actions(include_moves=True))


class CaptureSession:
    """A loaded capture session for analysis and replay.

    Provides access to time-aligned events and screenshots.
    Reads from the SQLAlchemy-based per-capture database (recording.db).

    Usage:
        capture = CaptureSession.load("./my_capture")

        for action in capture.actions():
            print(f"{action.type} at {action.timestamp}")
            img = action.screenshot
    """

    def __init__(
        self,
        capture_dir: str | Path,
        session,
        recording,
    ) -> None:
        """Initialize capture session.

        Use CaptureSession.load() instead of calling this directly.
        """
        self.capture_dir = Path(capture_dir)
        self._session = session
        self._recording = recording
        self._verified_tempdir = None
        self._verified_terminal = None

    @classmethod
    def load(cls, capture_dir: str | Path) -> "CaptureSession":
        """Load a capture from disk.

        Args:
            capture_dir: Path to capture directory.

        Returns:
            CaptureSession instance.

        Raises:
            FileNotFoundError: If capture doesn't exist.
        """
        capture_dir = Path(capture_dir)
        db_path = capture_dir / "recording.db"

        if not db_path.exists():
            raise FileNotFoundError(f"Capture not found: {capture_dir}")

        from openadapt_capture.db import get_session_for_path
        from openadapt_capture.db.models import Recording

        session = get_session_for_path(str(db_path))

        def _discard_session() -> None:
            # Close the session AND dispose its engine: the pool otherwise
            # keeps the SQLite file handle open, which on Windows leaves
            # recording.db locked (WinError 32 when deleting the directory).
            bind = session.get_bind()
            session.close()
            if bind is not None:
                bind.dispose()

        try:
            recording = session.query(Recording).first()
        except Exception:
            _discard_session()
            raise

        if recording is None:
            _discard_session()
            raise FileNotFoundError(f"Invalid capture (no recording found): {capture_dir}")

        return cls(capture_dir, session, recording)

    @classmethod
    def load_verified(cls, capture_dir: str | Path) -> "CaptureSession":
        """Verify a completed capture, snapshot it, and open its DB immutable."""
        from openadapt_capture.db import get_immutable_session_for_path
        from openadapt_capture.db.models import Recording
        from openadapt_capture.terminal import copy_verified_capture

        temporary, snapshot_dir, terminal = copy_verified_capture(capture_dir)
        session = get_immutable_session_for_path(str(snapshot_dir / "recording.db"))

        def _discard() -> None:
            bind = session.get_bind()
            session.close()
            if bind is not None:
                bind.dispose()
            temporary.cleanup()

        try:
            recordings = session.query(Recording).all()
        except Exception:
            _discard()
            raise
        if len(recordings) != 1:
            _discard()
            raise InvalidCaptureEvent(
                f"verified capture must contain exactly one recording, found {len(recordings)}"
            )
        recording = recordings[0]
        result = cls(snapshot_dir, session, recording)
        result._verified_tempdir = temporary
        result._verified_terminal = terminal
        try:
            _validate_database_contract(
                result,
                event_counts=terminal.event_counts.model_dump(mode="python"),
                last_source_ordinal=terminal.last_source_ordinal,
            )
        except BaseException:
            result.close()
            raise
        return result

    @classmethod
    def validate_sealed(cls, capture_dir: str | Path) -> None:
        """Validate one seal with a database-only temporary snapshot."""
        from openadapt_capture.db import get_immutable_session_for_path
        from openadapt_capture.db.models import Recording
        from openadapt_capture.terminal import (
            CaptureSealError,
            copy_verified_database,
            verify_capture_artifacts,
        )

        source = Path(capture_dir).resolve()
        temporary, database_path, terminal, manifest = copy_verified_database(source)
        session = None
        try:
            session = get_immutable_session_for_path(str(database_path))
            recordings = session.query(Recording).all()
            if len(recordings) != 1:
                raise InvalidCaptureEvent(
                    "verified capture must contain exactly one recording, "
                    f"found {len(recordings)}"
                )
            capture = cls(source, session, recordings[0])
            capture._verified_terminal = terminal
            _validate_database_contract(
                capture,
                event_counts=terminal.event_counts.model_dump(mode="python"),
                last_source_ordinal=terminal.last_source_ordinal,
            )
        finally:
            if session is not None:
                bind = session.get_bind()
                session.close()
                if bind is not None:
                    bind.dispose()
            temporary.cleanup()

        final_terminal, final_manifest = verify_capture_artifacts(source)
        if final_terminal != terminal or final_manifest != manifest:
            raise CaptureSealError("capture seal changed during semantic validation")

    @property
    def terminal(self):
        """Return the verified immutable terminal, or None for a legacy load."""
        return self._verified_terminal

    @property
    def id(self) -> str:
        """Capture ID."""
        return str(self._recording.id)

    @property
    def started_at(self) -> float:
        """Start timestamp."""
        return self._recording.timestamp

    @property
    def ended_at(self) -> float | None:
        """End timestamp (from last action event)."""
        if self._recording.action_events:
            return self._recording.action_events[-1].timestamp
        return None

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        ended = self.ended_at
        if ended is not None:
            return ended - self._recording.timestamp
        return None

    @property
    def platform(self) -> str:
        """Platform (darwin, win32, linux)."""
        return self._recording.platform or ""

    @property
    def screen_size(self) -> tuple[int, int]:
        """Screen dimensions (width, height) in physical pixels."""
        return (
            self._recording.monitor_width or 0,
            self._recording.monitor_height or 0,
        )

    @property
    def task_description(self) -> str | None:
        """Task description."""
        return self._recording.task_description

    @property
    def video_path(self) -> Path | None:
        """Return the only recognized video file, or refuse ambiguity."""
        candidates = sorted(self.capture_dir.glob("oa_recording-*.mp4"))
        fallback = self.capture_dir / "video.mp4"
        if fallback.exists():
            candidates.append(fallback)
        if len(candidates) > 1:
            raise InvalidCaptureEvent(
                "capture has multiple recognized MP4 artifacts; exact frame bindings are ambiguous"
            )
        return candidates[0] if candidates else None

    @property
    def audio_path(self) -> Path | None:
        """Path to audio file if exists."""
        audio_path = self.capture_dir / "audio.flac"
        return audio_path if audio_path.exists() else None

    @property
    def pixel_ratio(self) -> float:
        """Display pixel ratio (physical/logical), e.g. 2.0 for Retina.

        Raises ``DisplayMetricsUnavailable`` when no measurement was retained.
        """
        from math import isfinite

        from openadapt_capture.platform import DisplayMetricsUnavailable

        def validate(value: object, source: str) -> float:
            if isinstance(value, bool):
                raise DisplayMetricsUnavailable(
                    f"The capture has an invalid {source} display pixel ratio."
                )
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise DisplayMetricsUnavailable(
                    f"The capture has an invalid {source} display pixel ratio."
                ) from exc
            if not isfinite(parsed) or parsed <= 0:
                raise DisplayMetricsUnavailable(
                    f"The capture has an invalid {source} display pixel ratio."
                )
            return parsed

        # Check if the Recording model has a pixel_ratio column
        ratio = getattr(self._recording, "pixel_ratio", None)
        if ratio is not None:
            return validate(ratio, "stored")
        # Check the config JSON for pixel_ratio
        config = getattr(self._recording, "config", None)
        if isinstance(config, dict) and "pixel_ratio" in config:
            return validate(config["pixel_ratio"], "legacy")

        raise DisplayMetricsUnavailable(
            "The capture has no measured display pixel ratio. Supply an exact "
            "pixel_ratio in the legacy capture config or record the workflow again."
        )

    @property
    def window_capture(self) -> dict | None:
        """Window-scoping metadata for a window-scoped recording, else None.

        When not None, the session was recorded scoped to ONE window (see
        ``window_capture.py``): frames are that window's pixels and action
        coordinates are ALREADY in the captured frame's pixel space
        (``coordinate_space == "window_pixels"`` — do NOT rescale them by
        ``pixel_ratio``). Includes the target owner/title, resolved window id,
        initial bounds, capture scale, and viewport; the per-frame bounds
        timeline is in the recording's window events.
        """
        config = getattr(self._recording, "config", None)
        if isinstance(config, dict):
            return config.get("capture_window")
        return None

    @property
    def desktop_capture(self) -> dict | None:
        """Combined-monitor geometry for a full-screen recording, else None.

        New full-screen sessions translate native global input into MSS monitor
        zero, the captured virtual-desktop frame. The metadata retains its
        origin, viewport, monitor count, and privacy-safe monitor rectangles so
        converters must not apply the legacy display-ratio scale again.
        """

        config = getattr(self._recording, "config", None)
        if isinstance(config, dict):
            return config.get("capture_desktop")
        return None

    @property
    def audio_start_time(self) -> float | None:
        """Start timestamp of the audio recording, or None if unavailable."""
        # Check the AudioInfo relationship for the timestamp
        audio_infos = getattr(self._recording, "audio_info", None)
        if audio_infos:
            first = audio_infos[0] if isinstance(audio_infos, list) else audio_infos
            ts = getattr(first, "timestamp", None)
            if ts is not None:
                return float(ts)
        return None

    def raw_events(self) -> list[PydanticActionEvent]:
        """Get all raw action events (unprocessed).

        Converts SQLAlchemy ActionEvent models to Pydantic events.

        Returns:
            List of raw mouse and keyboard events.
        """
        events = []
        for db_event in _source_order(list(self._recording.action_events)):
            if getattr(db_event, "disabled", False):
                continue
            events.append(_convert_action_event(db_event))
        return events

    def window_events(self) -> list[CapturedWindowEvent]:
        """Return stored window rows through the public validated event view."""
        result: list[CapturedWindowEvent] = []
        for row in _source_order(list(self._recording.window_events)):
            state = getattr(row, "state", None)
            if not isinstance(state, dict):
                raise InvalidCaptureEvent(
                    f"stored WindowEvent at {row.timestamp!r} has invalid state"
                )
            result.append(
                CapturedWindowEvent(
                    timestamp=row.timestamp,
                    source_ordinal=getattr(row, "source_ordinal", None),
                    title=row.title,
                    left=row.left,
                    top=row.top,
                    width=row.width,
                    height=row.height,
                    window_id=row.window_id,
                    state=state,
                )
            )
        return result

    def frames(self) -> list[CapturedFrame]:
        """Return the retained frame timeline without exposing database rows."""
        return [
            CapturedFrame(
                timestamp=row.timestamp,
                source_ordinal=getattr(row, "source_ordinal", None),
                png_sha256=getattr(row, "png_sha256", None),
            )
            for row in _source_order(list(self._recording.screenshots))
        ]

    def window_capture_events_v2(self) -> list[CapturedWindowEvent]:
        """Return all rows from a declared v2 window-scoped session."""
        metadata = self.window_capture
        if metadata is None:
            return []
        if metadata.get("schema_version") != "openadapt.capture.window-scoped/v2":
            raise InvalidCaptureEvent(
                "window-scoped capture does not declare the supported v2 schema"
            )
        result = self.window_events()
        if not result:
            raise InvalidCaptureEvent("v2 window-scoped capture has no window events")
        for event in result:
            state: WindowCaptureStateV2 | None = event.window_capture_v2
            if state is None:
                raise InvalidCaptureEvent("v2 window-scoped capture contains a non-v2 window event")
            if event.window_id != state.window_id:
                raise InvalidCaptureEvent("stored WindowEvent identity differs from its v2 state")
            x, y, width, height = state.bounds
            if (
                event.left != int(x)
                or event.top != int(y)
                or event.width != int(width)
                or event.height != int(height)
            ):
                raise InvalidCaptureEvent("stored WindowEvent columns differ from their v2 bounds")
            if not state.on_screen:
                raise InvalidCaptureEvent("v2 window event retained an off-screen target")
        return result

    def actions(self, include_moves: bool = False) -> Iterator[Action]:
        """Iterate over processed actions.

        Yields time-ordered actions (clicks, drags, typed text) with
        associated screenshots.

        Args:
            include_moves: Whether to include mouse move events.

        Yields:
            Action objects with event data and screenshot access.
        """
        # Get and process raw events
        raw_events = self.raw_events()
        processed = process_events(
            raw_events,
            double_click_interval=self._recording.double_click_interval_seconds or 0.5,
            double_click_distance=self._recording.double_click_distance_pixels or 5,
        )

        # Filter out moves if not requested
        for event in processed:
            if not include_moves and isinstance(event, MouseMoveEvent):
                continue
            yield Action(event=event, _capture=self)

    def browser_events(self) -> list["BrowserEvent"]:
        """Get all browser events as typed Pydantic models.

        Parses the JSON message field from each stored BrowserEvent into
        the appropriate typed event (BrowserClickEvent, BrowserKeyEvent, etc.).

        Returns:
            List of typed browser events, ordered by timestamp.
        """
        events: list[BrowserEvent] = []
        for db_event in self._recording.browser_events:
            parsed = _convert_browser_event(db_event)
            if parsed is not None:
                events.append(parsed)
        return events

    @property
    def browser_event_count(self) -> int:
        """Number of browser events in this capture."""
        return len(self._recording.browser_events)

    def get_frame_at(self, timestamp: float, tolerance: float = 0.5) -> "Image" | None:
        """Get the screen frame closest to a timestamp.

        This is the lenient legacy lookup. Evidence consumers should prefer
        :meth:`get_exact_frame`, which resolves one exact retained frame and
        fails closed when it cannot.

        Args:
            timestamp: Unix timestamp.
            tolerance: Maximum time difference in seconds.

        Returns:
            PIL Image or None if not available.
        """
        video_path = self.video_path
        if video_path is None:
            return None

        try:
            from openadapt_capture.video import extract_frame

            # Convert to video-relative timestamp
            video_start = self._recording.video_start_time or self._recording.timestamp
            video_timestamp = timestamp - video_start

            if video_timestamp < 0:
                video_timestamp = 0

            return extract_frame(video_path, video_timestamp, tolerance=tolerance)
        except Exception:
            return None

    def get_exact_frame(
        self,
        capture_timestamp: float,
        *,
        source_ordinal: int | None = None,
    ) -> "Image":
        """Decode THE retained frame bound to this exact capture timestamp.

        Prefers the video's capture-timeline binding; for image-only captures
        (no video stream) the retained PNG row with that exact timestamp is
        the bound frame.

        Raises:
            LookupError: If no frame was retained at exactly this timestamp,
                or the binding metadata is unavailable (fail-closed).
        """
        from PIL import Image

        video_path = self.video_path
        if video_path is not None:
            from openadapt_capture.video import extract_exact_frame

            return extract_exact_frame(
                video_path,
                capture_timestamp,
                source_ordinal=source_ordinal,
            )
        for screenshot in self._recording.screenshots:
            ordinal_matches = (
                source_ordinal is None
                or getattr(screenshot, "source_ordinal", None) == source_ordinal
            )
            if screenshot.timestamp == capture_timestamp and ordinal_matches:
                if not screenshot.png_data:
                    raise LookupError(
                        f"the screenshot retained at {capture_timestamp!r} has no image data"
                    )
                return Image.open(io.BytesIO(screenshot.png_data)).convert("RGB")
        raise LookupError(
            f"no frame was retained at exactly {capture_timestamp!r}"
            + (f" with source ordinal {source_ordinal} " if source_ordinal is not None else " ")
            + "(fail-closed; refusing a nearest-frame substitute)"
        )

    def close(self) -> None:
        """Close the capture and release resources.

        Disposes the session's engine as well: SQLAlchemy's connection pool
        keeps the SQLite file handle open after ``Session.close()``, which on
        Windows leaves ``recording.db`` locked — deleting the capture
        directory would fail with WinError 32 (sharing violation).
        """
        if self._session is not None:
            bind = self._session.get_bind()
            self._session.close()
            if bind is not None:
                bind.dispose()
            self._session = None
        if self._verified_tempdir is not None:
            self._verified_tempdir.cleanup()
            self._verified_tempdir = None

    def __enter__(self) -> "CaptureSession":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# Alias for simpler import
Capture = CaptureSession
