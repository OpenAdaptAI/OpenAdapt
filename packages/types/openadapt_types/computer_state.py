"""Computer state representation for GUI automation agents.

Converges three existing schemas into one canonical model:

- ``openadapt_ml.schema.episode.Observation`` (Pydantic, focused_element only)
- ``openadapt_evals.adapters.base.BenchmarkObservation`` (dataclass, flat fields)
- ``omnimcp.types.ScreenState`` (dataclass, element list + dimensions)

Design principles:

- **Pixels + structure**: always capture both visual and semantic state.
- **Node graph**: full element tree, not just focused element.
- **Platform-agnostic**: same schema for Windows, macOS, Linux, web.
- **Extension-friendly**: ``raw`` and ``attributes`` fields for source-specific data.

Schema version: 0.1.0
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


SCHEMA_VERSION = "0.1.0"


# ── Element roles ────────────────────────────────────────────────────


class ElementRole(str, Enum):
    """Normalized UI element roles across platforms.

    Covers Windows UIA, macOS AX, web ARIA, and OCR-detected elements.
    """

    BUTTON = "button"
    TEXT_INPUT = "text_input"
    TEXT_STATIC = "text_static"
    LABEL = "label"
    LINK = "link"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    COMBOBOX = "combobox"
    LIST_ITEM = "list_item"
    MENU = "menu"
    MENU_ITEM = "menu_item"
    TAB = "tab"
    TREE_ITEM = "tree_item"
    IMAGE = "image"
    ICON = "icon"
    TOOLBAR = "toolbar"
    SCROLLBAR = "scrollbar"
    SLIDER = "slider"
    WINDOW = "window"
    DIALOG = "dialog"
    GROUP = "group"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    TABLE_ROW = "table_row"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    UNKNOWN = "unknown"


# ── Bounding box ─────────────────────────────────────────────────────


class BoundingBox(BaseModel):
    """Bounding box in pixel coordinates.

    Converges:
    - ``openadapt_ml.schema.episode.BoundingBox`` (x, y, width, height)
    - ``openadapt_evals.adapters.base.UIElement.bbox`` (x1, y1, x2, y2 tuple)
    - ``omnimcp.types.Bounds`` (x, y, w, h normalized tuple)

    We use pixel coordinates as the canonical form. Normalized coordinates
    can be computed from ``viewport`` on :class:`ComputerState`.
    """

    x: int = Field(description="Left edge, pixels")
    y: int = Field(description="Top edge, pixels")
    width: int = Field(ge=0, description="Width in pixels")
    height: int = Field(ge=0, description="Height in pixels")

    @property
    def center(self) -> tuple[int, int]:
        """Center point as ``(x, y)`` pixel coordinates."""
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def x2(self) -> int:
        """Right edge."""
        return self.x + self.width

    @property
    def y2(self) -> int:
        """Bottom edge."""
        return self.y + self.height

    def normalized(self, viewport: tuple[int, int]) -> tuple[float, float, float, float]:
        """Return ``(x, y, w, h)`` normalized to ``[0, 1]`` given *viewport* ``(w, h)``."""
        vw, vh = viewport
        return (
            self.x / vw if vw else 0.0,
            self.y / vh if vh else 0.0,
            self.width / vw if vw else 0.0,
            self.height / vh if vh else 0.0,
        )


# ── UI Node ──────────────────────────────────────────────────────────


class UINode(BaseModel):
    """A single node in the UI element graph.

    Converges:
    - ``openadapt_ml.schema.episode.UIElement``
      (role, name, value, bounds, element_id, xpath, selector, automation_id)
    - ``openadapt_evals.adapters.base.UIElement``
      (node_id, role, name, bbox, text, value, children, attributes)
    - ``omnimcp.types.UIElement``
      (id, type, content, bounds, confidence, attributes)
    """

    # Identification
    node_id: str = Field(description="Stable ID within this state snapshot (e.g., 'n0', '42')")
    role: str = Field(
        default="unknown",
        description="Element role — use ElementRole values when possible",
    )
    name: Optional[str] = Field(None, description="Accessible name / label")
    value: Optional[str] = Field(None, description="Current value (for inputs)")
    text: Optional[str] = Field(None, description="Visible text content")

    # Geometry
    bbox: Optional[BoundingBox] = Field(None, description="Bounding box in pixels")
    confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="Detection confidence (1.0 = ground truth)"
    )

    # Hierarchy
    parent_id: Optional[str] = Field(None, description="Parent node_id")
    children_ids: list[str] = Field(default_factory=list, description="Child node_ids")

    # Platform-specific anchors
    automation_id: Optional[str] = Field(None, description="Windows UIA AutomationId")
    xpath: Optional[str] = Field(None, description="XPath (web / a11y tree)")
    css_selector: Optional[str] = Field(None, description="CSS selector (web)")

    # State flags
    is_enabled: bool = Field(True)
    is_focused: bool = Field(False)
    is_visible: bool = Field(True)
    is_editable: bool = Field(False)
    is_selected: bool = Field(False)

    # Extension point
    attributes: dict[str, Any] = Field(default_factory=dict)


# ── Process / window info ────────────────────────────────────────────


class ProcessInfo(BaseModel):
    """Information about a running process / window."""

    pid: Optional[int] = None
    app_name: Optional[str] = None
    window_title: Optional[str] = None
    window_class: Optional[str] = None
    is_foreground: bool = False
    bounds: Optional[BoundingBox] = None


# ── Computer State ───────────────────────────────────────────────────


class ComputerState(BaseModel):
    """Unified representation of computer state at a point in time.

    This is THE canonical schema that all OpenAdapt components produce
    and consume.

    Converges:
    - ``openadapt_ml.schema.episode.Observation``
    - ``openadapt_evals.adapters.base.BenchmarkObservation``
    - ``omnimcp.types.ScreenState``
    """

    schema_version: str = Field(default=SCHEMA_VERSION)

    # ── Visual ──
    screenshot_png: Optional[bytes] = Field(None, description="Raw PNG bytes")
    screenshot_path: Optional[str] = Field(None, description="Path to screenshot file")
    screenshot_base64: Optional[str] = Field(None, description="Base64-encoded PNG")
    viewport: Optional[tuple[int, int]] = Field(
        None, description="Screen dimensions (width, height) in pixels"
    )

    # ── UI element graph ──
    nodes: list[UINode] = Field(
        default_factory=list, description="All detected UI elements"
    )
    focused_node_id: Optional[str] = Field(None, description="Currently focused element")
    root_node_id: Optional[str] = Field(None, description="Root of the element tree")

    # ── Context ──
    active_window: Optional[ProcessInfo] = Field(None, description="Foreground window")
    open_windows: list[ProcessInfo] = Field(default_factory=list)
    url: Optional[str] = Field(None, description="Current URL (web contexts)")
    clipboard_text: Optional[str] = Field(None, description="Clipboard contents")

    # ── Raw platform data ──
    accessibility_tree_raw: Optional[dict[str, Any]] = Field(
        None, description="Raw a11y tree (UIA / AXTree / DOM)"
    )
    dom_html: Optional[str] = Field(None, description="Raw HTML (web)")

    # ── Metadata ──
    timestamp: Optional[float] = Field(None, description="Unix timestamp")
    platform: Optional[str] = Field(
        None, description="'windows' | 'macos' | 'linux' | 'web'"
    )
    source: Optional[str] = Field(
        None, description="Source system (e.g., 'waa', 'osworld', 'omnimcp')"
    )
    raw: Optional[dict[str, Any]] = Field(None, description="Source-specific raw data")

    # ── Helpers ──

    def get_node(self, node_id: str) -> Optional[UINode]:
        """Look up a node by ID.  Returns ``None`` if not found."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def get_children(self, node_id: str) -> list[UINode]:
        """Return direct children of *node_id*."""
        node = self.get_node(node_id)
        if not node:
            return []
        return [n for n in self.nodes if n.node_id in node.children_ids]

    def to_text_tree(self, max_depth: int = 5) -> str:
        """Render the element graph as an indented text tree for LLM prompts."""
        lines: list[str] = []

        def _render(nid: str, depth: int) -> None:
            if depth > max_depth:
                return
            node = self.get_node(nid)
            if not node:
                return
            indent = "  " * depth
            label = f"[{node.node_id}] {node.role}"
            if node.name:
                label += f": {node.name}"
            if node.text:
                preview = node.text[:30].replace("\n", " ")
                label += f" '{preview}'"
            lines.append(f"{indent}{label}")
            for cid in node.children_ids:
                _render(cid, depth + 1)

        # Start from roots (nodes with no parent)
        if self.root_node_id:
            _render(self.root_node_id, 0)
        else:
            for node in self.nodes:
                if node.parent_id is None:
                    _render(node.node_id, 0)

        return "\n".join(lines)
