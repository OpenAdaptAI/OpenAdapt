"""Opt-in product analytics for OpenAdapt Desktop.

Aligned with docs/design/telemetry-design.md and packages/desktop/DESIGN.md:
telemetry stays **off** unless explicitly enabled. No screenshots, paths with
usernames, free-text task descriptions, secrets, or PHI are ever sent.

Enable with one of:
  OPENADAPT_TELEMETRY_ENABLED=true
  ~/.openadapt/telemetry.json  {"enabled": true}

Disable always wins:
  DO_NOT_TRACK=1
  OPENADAPT_TELEMETRY_ENABLED=false

Events use the shared PostHog project when OPENADAPT_POSTHOG_KEY is set
(same publishable key family as the marketing site). Without a key the
helper is a silent no-op even when enabled.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import uuid
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover - desktop runtime always has loguru
    import logging

    logger = logging.getLogger("openadapt_desktop.telemetry")

_ALLOWED_EVENTS = frozenset(
    {
        "app_opened",
        "recording_started",
        "recording_stopped",
        "recording_failed",
        "replay_started",
        "replay_stopped",
        "replay_failed",
        "teach_started",
        "teach_completed",
        "install_wizard_step",
        "error_shown",
    }
)

# Closed property keys only. Values must be scalars or short closed enums.
_ALLOWED_PROP_KEYS = frozenset(
    {
        "os",
        "os_release",
        "python_version",
        "app_version",
        "package",
        "internal",
        "outcome",
        "step",
        "mode",
        "error_code",
        "surface",
    }
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}


def _config_path() -> Path:
    override = os.environ.get("OPENADAPT_TELEMETRY_CONFIG", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".openadapt" / "telemetry.json"


def _read_file_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def telemetry_enabled() -> bool:
    """Return True only when the operator has opted in and not opted out."""
    if _truthy(os.environ.get("DO_NOT_TRACK")):
        return False
    if _falsy(os.environ.get("OPENADAPT_TELEMETRY_ENABLED")):
        return False
    if _truthy(os.environ.get("OPENADAPT_TELEMETRY_ENABLED")):
        return True
    file_cfg = _read_file_config()
    enabled = file_cfg.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    return False


def is_internal_usage() -> bool:
    if _truthy(os.environ.get("OPENADAPT_INTERNAL")) or _truthy(os.environ.get("OPENADAPT_DEV")):
        return True
    if any(os.environ.get(var) for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL")):
        return True
    return False


def _distinct_id() -> str:
    """Stable anonymous machine id under ~/.openadapt (not a person profile)."""
    path = Path.home() / ".openadapt" / "telemetry_id"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            value = path.read_text().strip()
            if value:
                return value
        value = uuid.uuid4().hex
        path.write_text(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        return uuid.uuid4().hex


def _sanitize(properties: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "python_version": platform.python_version(),
        "package": "openadapt-desktop",
        "internal": is_internal_usage(),
    }
    if not properties:
        return out
    for key, value in properties.items():
        if key not in _ALLOWED_PROP_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 64:
            out[key] = value
    return out


def _posthog_endpoint() -> tuple[str, str] | None:
    key = (
        os.environ.get("OPENADAPT_POSTHOG_KEY", "").strip()
        or _read_file_config().get("posthog_key")
        or ""
    )
    if not isinstance(key, str) or not key.startswith("phc_"):
        return None
    host = (
        os.environ.get("OPENADAPT_POSTHOG_HOST", "").strip()
        or _read_file_config().get("posthog_host")
        or "https://us.i.posthog.com"
    )
    if not isinstance(host, str) or not host.startswith("https://"):
        return None
    return key, host.rstrip("/")


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """Fire one closed funnel event when opt-in telemetry is enabled."""
    if event not in _ALLOWED_EVENTS:
        return
    if not telemetry_enabled():
        return
    endpoint = _posthog_endpoint()
    if endpoint is None:
        return
    key, host = endpoint
    payload = {
        "api_key": key,
        "event": event,
        "distinct_id": _distinct_id(),
        "properties": {
            **_sanitize(properties),
            "$lib": "openadapt-desktop",
        },
    }

    def _send() -> None:
        try:
            import httpx

            httpx.post(f"{host}/capture/", json=payload, timeout=3.0)
        except Exception:
            logger.debug("Telemetry capture skipped for {}", event)

    threading.Thread(target=_send, name="oa-telemetry", daemon=True).start()
