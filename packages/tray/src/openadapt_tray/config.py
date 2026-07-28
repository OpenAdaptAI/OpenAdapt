"""Configuration management for OpenAdapt Tray."""

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from openadapt_tray import keychain
from openadapt_tray.shortcuts import HotkeyConfig

# Sane bounds for the cloud needs-attention poll (spec §3c).
MIN_POLL_INTERVAL_S = 30
DEFAULT_POLL_INTERVAL_S = 60
OFFLINE_POLL_INTERVAL_S = 300


class ConfigLoadError(Exception):
    """``tray.json`` exists but could not be read.

    Distinct from "no config file", which legitimately means defaults.
    """

    def __init__(self, path: Path, cause: Exception):
        """Record which file failed and why.

        Args:
            path: The configuration file that could not be read.
            cause: The underlying parse/IO error.
        """
        super().__init__(f"Could not read {path}: {cause}")
        self.path = path
        self.cause = cause

    @property
    def defaults(self) -> "TrayConfig":
        """The default configuration, for a caller that chooses to continue."""
        return TrayConfig()


@dataclass
class TrayConfig:
    """Tray application configuration.

    Non-secret UI/behaviour prefs persist to the platform ``tray.json``. The
    hosted host + deployment lane are ALSO surfaced here so all local surfaces
    agree, but the ingest **token** is NEVER stored here — it is read from the
    OS keychain (see :mod:`openadapt_tray.keychain`).
    """

    # Hotkeys
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    # Paths
    captures_directory: str = "~/openadapt/captures"

    # Desktop app (local loopback IPC). The port is normally discovered from
    # ``~/.openadapt/desktop_ipc.json``; this is an optional override/fallback.
    dashboard_port: int = 8080
    auto_launch_dashboard: bool = True
    desktop_ipc_port: int | None = None

    # Hosted control plane (cloud). ``deployment_lane`` drives lane-aware
    # break-click routing (cloud → dashboard; byoc → local teach).
    hosted_url: str = "https://app.openadapt.ai"
    deployment_lane: str = "cloud"  # "cloud" | "byoc"
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S

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
        """Load configuration from file.

        A file that is not there means "this user has no saved preferences",
        and defaults are the right answer. A file that IS there but cannot be
        read is a different thing entirely, and defaults are the WRONG answer:
        ``deployment_lane`` defaults to ``"cloud"``, so silently substituting
        defaults would move a ``byoc`` install -- where the fix must stay local
        -- onto the hosted route without telling anybody.

        Returns:
            The saved configuration, or defaults when no file exists.

        Raises:
            ConfigLoadError: The file exists but could not be read or parsed.
                It carries ``defaults`` so a caller that chooses to continue
                does so knowingly.
        """
        path = cls.config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls._from_dict(data)
        except Exception as e:
            raise ConfigLoadError(path, e) from e

    @classmethod
    def load_or_defaults(cls) -> tuple["TrayConfig", "ConfigLoadError | None"]:
        """Load the configuration, reporting an unreadable file instead of hiding it.

        Returns:
            ``(config, error)``. ``error`` is ``None`` on a clean load. When it
            is set, ``config`` is the default configuration AND the caller is
            obliged to surface the error -- that is the whole point of handing
            both back instead of one silently-defaulted object.
        """
        try:
            return cls.load(), None
        except ConfigLoadError as e:
            return e.defaults, e

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "TrayConfig":
        """Create a TrayConfig from a dictionary.

        Unknown/stale keys (e.g. the retired ``training_output_directory``) are
        ignored so upgrading over an old ``tray.json`` never crashes.
        """
        data = dict(data)  # don't mutate caller's dict
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

        known = {f.name for f in fields(cls) if f.name != "hotkeys"}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(hotkeys=hotkeys, **filtered)

    def save(self) -> None:
        """Save configuration to file."""
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = self.to_dict()
        path.write_text(json.dumps(data, indent=2))

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to a dictionary (non-secret prefs only)."""
        return {
            "hotkeys": {
                "toggle_recording": self.hotkeys.toggle_recording,
                "open_dashboard": self.hotkeys.open_dashboard,
                "stop_recording": self.hotkeys.stop_recording,
            },
            "captures_directory": self.captures_directory,
            "dashboard_port": self.dashboard_port,
            "auto_launch_dashboard": self.auto_launch_dashboard,
            "desktop_ipc_port": self.desktop_ipc_port,
            "hosted_url": self.hosted_url,
            "deployment_lane": self.deployment_lane,
            "poll_interval_s": self.poll_interval_s,
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

    def effective_poll_interval_s(self) -> int:
        """Return the configured poll interval, clamped to the safe minimum."""
        return max(MIN_POLL_INTERVAL_S, int(self.poll_interval_s))

    def get_ingest_token(self) -> str | None:
        """Resolve the hosted ingest token from env/keychain (never the file)."""
        return keychain.get_ingest_token(self.hosted_url)
