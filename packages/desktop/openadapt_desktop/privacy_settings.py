"""Usage-stats consent and update-check settings for OpenAdapt Desktop.

Both settings live in the ``[privacy]`` table of the engine's non-secret
config file (``~/.openadapt/config.toml``, overridable with
``OPENADAPT_CONFIG_TOML``), next to the ``[hosted]`` settings:

.. code-block:: toml

    [privacy]
    usage_stats = "share"   # or "off"; absent until the person answers
    update_check = true     # absent means on

Environment variables always win over the file:

* ``DO_NOT_TRACK=1`` or ``OPENADAPT_TELEMETRY_ENABLED=false`` turns usage
  stats off and suppresses the first-launch question.
* ``OPENADAPT_TELEMETRY_ENABLED=true`` turns usage stats on without asking
  (for managed installs that decide centrally).
* ``OPENADAPT_UPDATE_CHECK=false`` turns the update check off.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

try:
    from loguru import logger
except ImportError:  # pragma: no cover - desktop runtime always has loguru
    import logging

    logger = logging.getLogger("openadapt_desktop.privacy_settings")

UsageStats = Literal["share", "off"]

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def config_path() -> Path:
    from openadapt_desktop.config import _config_toml_path

    return _config_toml_path()


def state_dir() -> Path:
    """Directory for local, non-setting state (install id, update cache)."""
    return config_path().parent


def _read_all() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_privacy() -> dict[str, Any]:
    privacy = _read_all().get("privacy")
    return privacy if isinstance(privacy, dict) else {}


def _write_privacy_key(key: str, value: Any) -> None:
    from openadapt_desktop.dispatch import _dumps_toml

    data = _read_all()
    privacy = data.get("privacy")
    if not isinstance(privacy, dict):
        privacy = {}
    privacy[key] = value
    data["privacy"] = privacy
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps_toml(data))


def usage_stats_env_override() -> UsageStats | None:
    """What the environment decides for usage stats, if anything."""
    if _env("DO_NOT_TRACK") in _TRUTHY:
        return "off"
    enabled = _env("OPENADAPT_TELEMETRY_ENABLED")
    if enabled in _FALSY:
        return "off"
    if enabled in _TRUTHY:
        return "share"
    return None


def stored_usage_stats() -> UsageStats | None:
    value = _read_privacy().get("usage_stats")
    return value if value in ("share", "off") else None


def usage_stats_choice() -> UsageStats | None:
    """The effective choice: environment first, then the saved answer."""
    return usage_stats_env_override() or stored_usage_stats()


def usage_stats_enabled() -> bool:
    return usage_stats_choice() == "share"


def should_ask_about_usage_stats() -> bool:
    """True only before the person has answered and nothing in the env decides."""
    return usage_stats_choice() is None


def set_usage_stats(share: bool) -> None:
    _write_privacy_key("usage_stats", "share" if share else "off")


def update_check_env_disabled() -> bool:
    return _env("OPENADAPT_UPDATE_CHECK") in _FALSY


def update_check_enabled() -> bool:
    if update_check_env_disabled():
        return False
    value = _read_privacy().get("update_check")
    return value if isinstance(value, bool) else True


def set_update_check(enabled: bool) -> None:
    _write_privacy_key("update_check", bool(enabled))


def status() -> dict[str, Any]:
    """The settings as the Desktop window reads them."""
    env_choice = usage_stats_env_override()
    return {
        "usage_stats": usage_stats_choice(),
        "ask_usage_stats": should_ask_about_usage_stats(),
        "usage_stats_managed_by_env": env_choice is not None,
        "update_check": update_check_enabled(),
        "update_check_managed_by_env": update_check_env_disabled(),
    }
