"""Configuration management for OpenAdapt Tray."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any, Dict
import json
import os

from openadapt_tray.shortcuts import HotkeyConfig


@dataclass
class TrayConfig:
    """Tray application configuration."""

    # Hotkeys
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    # Paths
    captures_directory: str = "~/openadapt/captures"
    training_output_directory: str = "~/openadapt/training"

    # Dashboard
    dashboard_port: int = 8080
    auto_launch_dashboard: bool = True

    # Behavior
    auto_start_on_login: bool = False
    minimize_to_tray: bool = True
    show_notifications: bool = True
    notification_duration_ms: int = 5000

    # Recording
    default_record_audio: bool = True
    default_transcribe: bool = True
    stop_on_triple_ctrl: bool = True

    # Appearance
    use_native_dialogs: bool = True

    @classmethod
    def config_path(cls) -> Path:
        """Get configuration file path."""
        # Use XDG_CONFIG_HOME on Linux, ~/Library/Application Support on macOS,
        # %APPDATA% on Windows
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif os.name == "posix":
            if "darwin" in os.sys.platform:
                base = Path.home() / "Library" / "Application Support"
            else:
                base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        else:
            base = Path.home() / ".config"

        return base / "openadapt" / "tray.json"

    @classmethod
    def load(cls) -> "TrayConfig":
        """Load configuration from file."""
        path = cls.config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return cls._from_dict(data)
            except Exception as e:
                print(f"Warning: Could not load config: {e}")
        return cls()

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "TrayConfig":
        """Create a TrayConfig from a dictionary."""
        hotkeys_data = data.pop("hotkeys", {})
        hotkeys = HotkeyConfig(
            toggle_recording=hotkeys_data.get(
                "toggle_recording", HotkeyConfig.toggle_recording
            ),
            open_dashboard=hotkeys_data.get(
                "open_dashboard", HotkeyConfig.open_dashboard
            ),
            stop_recording=hotkeys_data.get(
                "stop_recording", HotkeyConfig.stop_recording
            ),
        )

        return cls(hotkeys=hotkeys, **data)

    def save(self) -> None:
        """Save configuration to file."""
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = self.to_dict()
        path.write_text(json.dumps(data, indent=2))

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to a dictionary."""
        return {
            "hotkeys": {
                "toggle_recording": self.hotkeys.toggle_recording,
                "open_dashboard": self.hotkeys.open_dashboard,
                "stop_recording": self.hotkeys.stop_recording,
            },
            "captures_directory": self.captures_directory,
            "training_output_directory": self.training_output_directory,
            "dashboard_port": self.dashboard_port,
            "auto_launch_dashboard": self.auto_launch_dashboard,
            "auto_start_on_login": self.auto_start_on_login,
            "minimize_to_tray": self.minimize_to_tray,
            "show_notifications": self.show_notifications,
            "notification_duration_ms": self.notification_duration_ms,
            "default_record_audio": self.default_record_audio,
            "default_transcribe": self.default_transcribe,
            "stop_on_triple_ctrl": self.stop_on_triple_ctrl,
            "use_native_dialogs": self.use_native_dialogs,
        }

    def get_captures_path(self) -> Path:
        """Get the expanded captures directory path."""
        return Path(self.captures_directory).expanduser()

    def get_training_path(self) -> Path:
        """Get the expanded training output directory path."""
        return Path(self.training_output_directory).expanduser()
