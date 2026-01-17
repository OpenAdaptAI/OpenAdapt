"""Icon management for OpenAdapt Tray."""

from pathlib import Path
from typing import Optional, Dict
import sys

from PIL import Image

from openadapt_tray.state import TrayState


class IconManager:
    """Manages loading and caching of tray icons."""

    # State to icon filename mapping
    STATE_ICONS: Dict[TrayState, str] = {
        TrayState.IDLE: "idle.png",
        TrayState.RECORDING_STARTING: "recording.png",
        TrayState.RECORDING: "recording.png",
        TrayState.RECORDING_STOPPING: "recording.png",
        TrayState.TRAINING: "training.png",
        TrayState.TRAINING_PAUSED: "training.png",
        TrayState.ERROR: "error.png",
    }

    # Icon colors for fallback generation
    STATE_COLORS: Dict[TrayState, str] = {
        TrayState.IDLE: "#4A90D9",  # Blue
        TrayState.RECORDING_STARTING: "#F5A623",  # Yellow/Orange
        TrayState.RECORDING: "#D0021B",  # Red
        TrayState.RECORDING_STOPPING: "#F5A623",  # Yellow/Orange
        TrayState.TRAINING: "#7B68EE",  # Purple
        TrayState.TRAINING_PAUSED: "#9B59B6",  # Lighter Purple
        TrayState.ERROR: "#D0021B",  # Red
    }

    def __init__(self, assets_dir: Optional[Path] = None):
        """Initialize the icon manager.

        Args:
            assets_dir: Path to the assets directory. If None, uses package assets.
        """
        if assets_dir is None:
            # Find assets directory relative to this module
            module_dir = Path(__file__).parent
            assets_dir = module_dir.parent.parent / "assets" / "icons"

        self.assets_dir = assets_dir
        self._cache: Dict[str, Image.Image] = {}
        self._retina_scale = self._detect_retina()

    def _detect_retina(self) -> int:
        """Detect if we're on a retina/HiDPI display."""
        if sys.platform == "darwin":
            # On macOS, assume retina by default for newer systems
            return 2
        return 1

    def get(self, state: TrayState) -> Image.Image:
        """Get the icon for a given state.

        Args:
            state: The application state.

        Returns:
            PIL Image for the icon.
        """
        icon_name = self.STATE_ICONS.get(state, "idle.png")

        # Check cache first
        cache_key = f"{icon_name}_{self._retina_scale}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Try to load icon file
        icon = self._load_icon(icon_name)
        if icon is None:
            # Generate fallback icon
            icon = self._generate_fallback(state)

        self._cache[cache_key] = icon
        return icon

    def _load_icon(self, icon_name: str) -> Optional[Image.Image]:
        """Try to load an icon from the assets directory.

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
                except Exception:
                    pass

        # Try regular version
        icon_path = self.assets_dir / icon_name
        if icon_path.exists():
            try:
                return Image.open(icon_path)
            except Exception:
                pass

        return None

    def _generate_fallback(self, state: TrayState, size: int = 64) -> Image.Image:
        """Generate a simple colored square icon as fallback.

        Args:
            state: The application state.
            size: The icon size in pixels.

        Returns:
            Generated PIL Image.
        """
        color = self.STATE_COLORS.get(state, "#4A90D9")
        return self.create_colored_icon(color, size)

    @staticmethod
    def create_colored_icon(color: str, size: int = 64) -> Image.Image:
        """Create a simple colored square icon.

        Args:
            color: Hex color string (e.g., "#FF0000").
            size: Icon size in pixels.

        Returns:
            PIL Image with the specified color.
        """
        # Create RGBA image with rounded corners effect
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))

        # Parse hex color
        if color.startswith("#"):
            color = color[1:]
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)

        # Draw a filled circle for a cleaner look
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)

        # Draw circle with slight padding
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
