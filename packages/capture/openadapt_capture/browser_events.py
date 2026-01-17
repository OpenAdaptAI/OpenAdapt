"""Browser event schemas for Chrome extension communication.

This module defines Pydantic models for all browser event types captured by
the Chrome extension. These events include DOM-level interactions with rich
semantic element references.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# =============================================================================
# Browser Event Types
# =============================================================================


class BrowserEventType(str, Enum):
    """Browser event type identifiers."""

    # User interaction events
    CLICK = "browser.click"
    KEYDOWN = "browser.keydown"
    KEYUP = "browser.keyup"
    SCROLL = "browser.scroll"
    INPUT = "browser.input"
    FOCUS = "browser.focus"
    BLUR = "browser.blur"

    # Navigation events
    NAVIGATE = "browser.navigate"

    # Unknown/generic
    UNKNOWN = "browser.unknown"


class NavigationType(str, Enum):
    """Types of browser navigation."""

    LINK = "link"
    TYPED = "typed"
    RELOAD = "reload"
    BACK_FORWARD = "back_forward"
    FORM_SUBMIT = "form_submit"


# =============================================================================
# Semantic Element Reference
# =============================================================================


class BoundingBox(BaseModel):
    """Bounding box for an element in page coordinates."""

    x: float = Field(description="Left edge in viewport pixels")
    y: float = Field(description="Top edge in viewport pixels")
    width: float = Field(description="Element width")
    height: float = Field(description="Element height")


class ElementState(BaseModel):
    """State information for a DOM element."""

    enabled: bool = Field(default=True, description="Not disabled")
    focused: bool = Field(default=False, description="Has focus")
    visible: bool = Field(default=True, description="Computed visibility")
    checked: bool | None = Field(default=None, description="For checkboxes/radios")
    selected: bool | None = Field(default=None, description="For options")
    expanded: bool | None = Field(default=None, description="For expandable elements")
    value: str | None = Field(default=None, description="Current value (inputs)")


class SemanticElementRef(BaseModel):
    """Semantic reference to a DOM element.

    Contains rich information for element identification including:
    - Identity (role, accessible name)
    - Location (bounding box)
    - Selectors (xpath, css for replay)
    - State (enabled, focused, etc.)
    """

    # Identity
    role: str = Field(description="ARIA role or inferred from tag")
    name: str = Field(default="", description="Accessible name")

    # Location
    bbox: BoundingBox = Field(description="Bounding box in page coordinates")

    # Selectors for replay
    xpath: str = Field(description="Absolute XPath")
    css_selector: str = Field(default="", description="Minimal unique CSS selector")

    # State
    state: ElementState = Field(default_factory=ElementState)

    # Optional metadata
    tag_name: str = Field(default="", description="HTML tag name")
    id: str | None = Field(default=None, description="Element ID if present")
    class_list: list[str] = Field(default_factory=list, description="CSS classes")


# =============================================================================
# Base Browser Event
# =============================================================================


class BaseBrowserEvent(BaseModel):
    """Base class for all browser events.

    All browser events have a timestamp, type, and URL.
    """

    timestamp: float = Field(description="Unix timestamp in seconds")
    type: BrowserEventType = Field(description="Event type identifier")
    url: str = Field(description="Current page URL")
    tab_id: int = Field(default=0, description="Chrome tab ID")

    model_config = {"use_enum_values": True}


# =============================================================================
# Click Events
# =============================================================================


class BrowserClickEvent(BaseBrowserEvent):
    """Mouse click event in browser.

    Captures click position, button, and target element reference.
    """

    type: Literal[BrowserEventType.CLICK] = BrowserEventType.CLICK

    # Coordinates
    client_x: float = Field(description="Viewport X")
    client_y: float = Field(description="Viewport Y")
    page_x: float = Field(description="Page X (with scroll)")
    page_y: float = Field(description="Page Y (with scroll)")

    # Click details
    button: int = Field(default=0, description="0=left, 1=middle, 2=right")
    click_count: int = Field(default=1, description="1=single, 2=double")

    # Target element
    element: SemanticElementRef = Field(description="Target element reference")


# =============================================================================
# Keyboard Events
# =============================================================================


class BrowserKeyEvent(BaseBrowserEvent):
    """Keyboard event in browser."""

    type: Literal[BrowserEventType.KEYDOWN, BrowserEventType.KEYUP]

    # Key identification
    key: str = Field(description="Logical key value (e.g., 'a', 'Enter', 'Shift')")
    code: str = Field(description="Physical key code (e.g., 'KeyA', 'Enter', 'ShiftLeft')")
    key_code: int = Field(default=0, description="Legacy key code")

    # Modifiers
    shift_key: bool = Field(default=False)
    ctrl_key: bool = Field(default=False)
    alt_key: bool = Field(default=False)
    meta_key: bool = Field(default=False)

    # Target element (if focused)
    element: SemanticElementRef | None = Field(default=None)


# =============================================================================
# Scroll Events
# =============================================================================


class BrowserScrollEvent(BaseBrowserEvent):
    """Scroll event in browser."""

    type: Literal[BrowserEventType.SCROLL] = BrowserEventType.SCROLL

    # Scroll position
    scroll_x: float = Field(description="Horizontal scroll offset")
    scroll_y: float = Field(description="Vertical scroll offset")

    # Scroll delta
    delta_x: float = Field(description="Horizontal scroll change")
    delta_y: float = Field(description="Vertical scroll change")

    # Target (window or element)
    target: str | SemanticElementRef = Field(
        default="window",
        description="'window' or element reference"
    )


# =============================================================================
# Form Input Events
# =============================================================================


class BrowserInputEvent(BaseBrowserEvent):
    """Form input change event in browser."""

    type: Literal[BrowserEventType.INPUT] = BrowserEventType.INPUT

    # Input details
    input_type: str = Field(
        description="Input type (e.g., 'insertText', 'deleteContentBackward')"
    )
    data: str | None = Field(default=None, description="Inserted text")
    value: str = Field(description="Current field value")

    # Target element
    element: SemanticElementRef = Field(description="Target input element")


# =============================================================================
# Navigation Events
# =============================================================================


class BrowserNavigationEvent(BaseBrowserEvent):
    """Page navigation event in browser."""

    type: Literal[BrowserEventType.NAVIGATE] = BrowserEventType.NAVIGATE

    # Navigation details
    previous_url: str = Field(default="", description="Previous URL")
    navigation_type: NavigationType = Field(
        default=NavigationType.LINK,
        description="Type of navigation"
    )


# =============================================================================
# Focus Events
# =============================================================================


class BrowserFocusEvent(BaseBrowserEvent):
    """Element focus/blur event in browser."""

    type: Literal[BrowserEventType.FOCUS, BrowserEventType.BLUR]

    # Target element
    element: SemanticElementRef = Field(description="Focused/blurred element")


# =============================================================================
# DOM Snapshot
# =============================================================================


class VisibleElement(BaseModel):
    """A visible interactive element in the DOM snapshot."""

    element: SemanticElementRef = Field(description="Element reference")
    center_x: float = Field(description="Center X coordinate")
    center_y: float = Field(description="Center Y coordinate")
    som_id: int | None = Field(default=None, description="Set-of-Marks ID if assigned")


class DOMSnapshot(BaseModel):
    """Full DOM snapshot from browser.

    Captured on navigation or periodically for Set-of-Marks mode.
    """

    timestamp: float = Field(description="Unix timestamp in seconds")
    url: str = Field(description="Current page URL")
    title: str = Field(description="Page title")
    tab_id: int = Field(default=0, description="Chrome tab ID")
    html: str | None = Field(default=None, description="Full HTML content")
    visible_elements: list[VisibleElement] = Field(
        default_factory=list,
        description="List of visible interactive elements"
    )


# =============================================================================
# Union type for all browser events
# =============================================================================

BrowserEvent = (
    BrowserClickEvent
    | BrowserKeyEvent
    | BrowserScrollEvent
    | BrowserInputEvent
    | BrowserNavigationEvent
    | BrowserFocusEvent
)
