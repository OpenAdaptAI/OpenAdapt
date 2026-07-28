"""Icon management for OpenAdapt Tray.

The tray icon is the OpenAdapt mark (the chat/robot face), color-stylized per
recording-lifecycle state so the state stays readable at a glance in the menu
bar / system tray:

* idle            -> brand blue
* recording start -> amber (spinning up)
* recording       -> red
* recording stop  -> amber (winding down)
* compiling       -> purple (transforming a recording into a workflow)
* error           -> red

The mark is stored once as a high-resolution transparent master
(``assets/mark-master.png``, shipped inside the package). Every state icon is
produced by tinting that master's alpha silhouette with the state colour, so all
states render as the same recognizable mark and never drift apart. Pre-rendered
PNGs committed under the repo ``assets/icons`` directory are used when present
(dev/website), and are the same tinted marks produced at runtime.
"""

import sys
from pathlib import Path
from typing import ClassVar

from PIL import Image, ImageDraw

from openadapt_tray.state import TrayState


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Parse a ``#RRGGBB`` (or ``RRGGBB``) hex string into an RGB tuple."""
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


class IconManager:
    """Manages loading, tinting and caching of tray icons.

    Every icon is the OpenAdapt mark tinted with the state colour. The mark
    silhouette comes from a packaged high-res master so the icon ships in the
    wheel and renders identically whether or not pre-rendered PNGs are present.
    """

    # State to icon filename mapping (one file per state; produced by
    # ``scripts/generate_icons.py`` from the same master + colours used at
    # runtime, so file-backed and runtime-tinted icons are identical).
    STATE_ICONS: ClassVar[dict[TrayState, str]] = {
        TrayState.IDLE: "idle.png",
        TrayState.RECORDING_STARTING: "recording_starting.png",
        TrayState.RECORDING: "recording.png",
        TrayState.RECORDING_STOPPING: "recording_stopping.png",
        TrayState.COMPILING: "compiling.png",
        TrayState.ERROR: "error.png",
    }

    # Per-state mark tint. Keeps the historical state->colour signalling.
    STATE_COLORS: ClassVar[dict[TrayState, str]] = {
        TrayState.IDLE: "#4A90D9",  # Brand blue
        TrayState.RECORDING_STARTING: "#F5A623",  # Amber (spinning up)
        TrayState.RECORDING: "#D0021B",  # Red
        TrayState.RECORDING_STOPPING: "#F5A623",  # Amber (winding down)
        TrayState.COMPILING: "#7B68EE",  # Purple (transforming a recording)
        TrayState.ERROR: "#D0021B",  # Red
    }

    # Colour of the break/needs-attention badge dot.
    BADGE_COLOR = "#D0021B"  # Red

    # Packaged mark master (transparent silhouette). Ships in the wheel.
    MASTER_PATH = Path(__file__).parent / "assets" / "mark-master.png"

    def __init__(self, assets_dir: Path | None = None):
        """Initialize the icon manager.

        Args:
            assets_dir: Path to the pre-rendered icons directory. If None, uses
                the repo ``assets/icons`` directory when present (dev). Runtime
                tinting of the packaged master is the source of truth whenever a
                file is missing, so a bare wheel still shows the mark.
        """
        if assets_dir is None:
            module_dir = Path(__file__).parent
            assets_dir = module_dir.parent.parent / "assets" / "icons"

        self.assets_dir = assets_dir
        self._cache: dict[str, Image.Image] = {}
        self._master: Image.Image | None = None
        self._retina_scale = self._detect_retina()

    def _detect_retina(self) -> int:
        """Detect if we're on a retina/HiDPI display."""
        if sys.platform == "darwin":
            # On macOS, assume retina by default for newer systems
            return 2
        return 1

    def _load_master(self) -> Image.Image | None:
        """Load (and cache) the transparent mark master, if available."""
        if self._master is not None:
            return self._master
        if self.MASTER_PATH.exists():
            try:
                self._master = Image.open(self.MASTER_PATH).convert("RGBA")
            except Exception:
                self._master = None
        return self._master

    def get(self, state: TrayState, break_count: int = 0) -> Image.Image:
        """Get the icon for a given state, optionally with a break badge.

        Args:
            state: The recording-lifecycle state.
            break_count: Number of automations needing attention. When > 0 a
                small red badge dot is overlaid on the icon.

        Returns:
            PIL Image for the icon.
        """
        icon_name = self.STATE_ICONS.get(state, "idle.png")

        # Badge presence is part of the cache key so badged/unbadged variants
        # don't collide.
        badge = break_count > 0
        cache_key = f"{icon_name}_{self._retina_scale}_badge={badge}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Prefer a pre-rendered file (dev/website); otherwise tint the mark.
        icon = self._load_icon(icon_name)
        if icon is None:
            icon = self._generate_fallback(state)

        if badge:
            icon = self._apply_badge(icon)

        self._cache[cache_key] = icon
        return icon

    def _apply_badge(self, icon: Image.Image) -> Image.Image:
        """Overlay a small badge dot on the top-right of an icon.

        Args:
            icon: The base icon image.

        Returns:
            A new image with the badge composited on top.
        """
        base = icon.convert("RGBA").copy()
        w, _h = base.size
        draw = ImageDraw.Draw(base)

        # Badge sized relative to the icon, anchored top-right.
        radius = max(4, w // 4)
        cx, cy = w - radius, radius
        r, g, b = _hex_to_rgb(self.BADGE_COLOR)
        # Punch a small transparent ring so the dot reads against the mark.
        ring = max(1, radius // 3)
        draw.ellipse(
            [cx - radius - ring, cy - radius - ring,
             cx + radius + ring, cy + radius + ring],
            fill=(0, 0, 0, 0),
        )
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=(r, g, b, 255),
        )
        return base

    def _load_icon(self, icon_name: str) -> Image.Image | None:
        """Try to load a pre-rendered icon from the assets directory.

        Args:
            icon_name: The icon filename.

        Returns:
            PIL Image if found, None otherwise.
        """
        if not self.assets_dir.exists():
            return None

        # Try retina version first on HiDPI displays
        if self._retina_scale > 1:
            retina_name = icon_name.replace(".png", "@2x.png")
            retina_path = self.assets_dir / retina_name
            if retina_path.exists():
                try:
                    return Image.open(retina_path)
                except OSError as e:
                    # Present but unreadable/corrupt. Fall through to the 1x file
                    # and then to runtime tinting, but say why -- silently
                    # ignoring a bad PNG made a broken asset indistinguishable
                    # from a missing one.
                    print(f"Could not read icon {retina_path}: {e}")

        # Try regular version
        icon_path = self.assets_dir / icon_name
        if icon_path.exists():
            try:
                return Image.open(icon_path)
            except OSError as e:
                # Fall through to runtime tinting of the packaged master.
                print(f"Could not read icon {icon_path}: {e}")

        return None

    def _generate_fallback(self, state: TrayState, size: int = 64) -> Image.Image:
        """Render the mark tinted for a state (used when no PNG file exists).

        Args:
            state: The application state.
            size: The icon size in pixels.

        Returns:
            Generated PIL Image.
        """
        return self.render_state_icon(state, size)

    def render_state_icon(self, state: TrayState, size: int = 64) -> Image.Image:
        """Render the tinted mark for a state at an explicit size.

        Public helper shared by ``scripts/generate_icons.py`` so committed PNGs
        and runtime icons come from one implementation.
        """
        color = self.STATE_COLORS.get(state, self.STATE_COLORS[TrayState.IDLE])
        master = self._load_master()
        if master is None:
            # Last-resort fallback if the packaged master is somehow missing.
            return self.create_colored_icon(color, size)
        return self.tint_mark(master, color, size)

    @staticmethod
    def tint_mark(master: Image.Image, color: str, size: int) -> Image.Image:
        """Tint the mark master's silhouette with ``color`` at ``size`` px.

        The master is a transparent image whose alpha encodes the mark (eyes and
        smile are negative space). We keep that alpha and replace the colour, so
        every state is the same recognizable mark in its own colour.

        Args:
            master: The transparent mark master image (RGBA).
            color: Hex colour string, e.g. ``"#4A90D9"``.
            size: Output width/height in pixels.

        Returns:
            A square RGBA image of the tinted mark.
        """
        r, g, b = _hex_to_rgb(color)
        scaled = master.convert("RGBA").resize((size, size), Image.LANCZOS)
        alpha = scaled.split()[3]
        solid = Image.new("RGBA", (size, size), (r, g, b, 255))
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(solid, (0, 0), alpha)
        return out

    @staticmethod
    def create_colored_icon(color: str, size: int = 64) -> Image.Image:
        """Create a simple colored circle icon (legacy last-resort fallback).

        Args:
            color: Hex color string (e.g., "#FF0000").
            size: Icon size in pixels.

        Returns:
            PIL Image with the specified color.
        """
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        r, g, b = _hex_to_rgb(color)
        draw = ImageDraw.Draw(image)
        padding = size // 8
        draw.ellipse(
            [padding, padding, size - padding, size - padding],
            fill=(r, g, b, 255),
        )
        return image

    def clear_cache(self) -> None:
        """Clear the icon cache."""
        self._cache.clear()

    def preload_all(self) -> None:
        """Preload all state icons into cache."""
        for state in TrayState:
            self.get(state)
