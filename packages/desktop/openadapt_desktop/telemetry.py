"""Anonymous usage stats for OpenAdapt Desktop, sent only after consent.

Nothing is sent until the person answers "Share" in the first-launch question
(or in Settings). The answer is stored in ``[privacy].usage_stats`` of
``~/.openadapt/config.toml`` (see :mod:`openadapt_desktop.privacy_settings`).
``DO_NOT_TRACK=1`` or ``OPENADAPT_TELEMETRY_ENABLED=false`` always turns it off.

What an event carries is closed: an event name from :data:`ALLOWED_EVENTS`,
a random install id (a UUID made on this computer, not derived from the
hostname, username, or hardware), ``app_version``, ``os``, ``arch``, an
``internal`` flag for CI and developer machines, and a few short enum
properties from :data:`ALLOWED_PROPERTIES`. Screen content, keystrokes, file
paths, task text, workflow names, and errors' text are never sent. PostHog is
asked not to build a person profile or look up a location from the IP.

The PostHog project key below is a public, write-only ingestion key (the same
one the openadapt.ai website ships to every browser). It can send events but
cannot read any data. ``OPENADAPT_POSTHOG_KEY`` and ``OPENADAPT_POSTHOG_HOST``
override it for self-hosted or test projects.
"""

from __future__ import annotations

import os
import platform
import re
import threading
import uuid
from typing import Any, Callable

from openadapt_desktop import __version__, privacy_settings

try:
    from loguru import logger
except ImportError:  # pragma: no cover - desktop runtime always has loguru
    import logging

    logger = logging.getLogger("openadapt_desktop.telemetry")

#: Public, write-only PostHog project key. The single place to change it.
POSTHOG_PROJECT_KEY = "phc_935iWKc6O7u6DCp2eFAmK5WmCwv35QXMa6LulTJ3uqh"
POSTHOG_HOST = "https://us.i.posthog.com"
#: Seconds before a capture request is abandoned.
TIMEOUT_S = 3.0

ALLOWED_EVENTS = frozenset(
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
        "update_check",
    }
)

#: Per-event properties a caller may add. Strings must be short enum tokens.
ALLOWED_PROPERTIES = frozenset({"surface", "mode", "outcome", "error_code", "update_available"})

_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

_ARCH_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


def os_name() -> str:
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        platform.system(), "other"
    )


def arch_name() -> str:
    machine = platform.machine().lower()
    return _ARCH_ALIASES.get(machine, machine if _TOKEN.match(machine or "") else "other")


def is_internal_usage() -> bool:
    for var in ("OPENADAPT_INTERNAL", "OPENADAPT_DEV"):
        if (os.environ.get(var) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return any(os.environ.get(var) for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL"))


def telemetry_enabled() -> bool:
    """True only after consent and when nothing in the environment says no."""
    try:
        return privacy_settings.usage_stats_enabled()
    except Exception:
        return False


def install_id() -> str:
    """A random id for this install, made once and kept on this computer."""
    path = privacy_settings.state_dir() / "telemetry_id"
    try:
        if path.exists():
            value = path.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = uuid.uuid4().hex
        path.write_text(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        return uuid.uuid4().hex


def base_properties() -> dict[str, Any]:
    return {
        "app_version": __version__,
        "os": os_name(),
        "arch": arch_name(),
        "internal": is_internal_usage(),
        "package": "openadapt-desktop",
    }


def sanitize(properties: dict[str, Any] | None) -> dict[str, Any]:
    """Base properties plus only the allowed, closed caller properties."""
    out = base_properties()
    for key, value in (properties or {}).items():
        if key not in ALLOWED_PROPERTIES or value is None:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str) and _TOKEN.match(value):
            out[key] = value
    return out


def endpoint() -> tuple[str, str] | None:
    key = (os.environ.get("OPENADAPT_POSTHOG_KEY") or "").strip() or POSTHOG_PROJECT_KEY
    host = (os.environ.get("OPENADAPT_POSTHOG_HOST") or "").strip() or POSTHOG_HOST
    if not key.startswith("phc_") or not host.startswith("https://"):
        return None
    return key, host.rstrip("/")


def build_payload(event: str, properties: dict[str, Any] | None = None) -> dict[str, Any] | None:
    resolved = endpoint()
    if resolved is None:
        return None
    key, _host = resolved
    return {
        "api_key": key,
        "event": event,
        "distinct_id": install_id(),
        "properties": {
            **sanitize(properties),
            "$lib": "openadapt-desktop",
            "$process_person_profile": False,
            "$geoip_disable": True,
        },
    }


def _post(url: str, payload: dict[str, Any]) -> None:
    import httpx

    httpx.post(url, json=payload, timeout=TIMEOUT_S)


def _start(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="oa-telemetry", daemon=True).start()


def capture(event: str, properties: dict[str, Any] | None = None) -> bool:
    """Queue one event in the background. Returns whether it was queued.

    Never raises and never blocks the caller on the network.
    """
    try:
        if event not in ALLOWED_EVENTS or not telemetry_enabled():
            return False
        payload = build_payload(event, properties)
        resolved = endpoint()
        if payload is None or resolved is None:
            return False
        url = f"{resolved[1]}/capture/"

        def _send() -> None:
            try:
                _post(url, payload)
            except Exception:
                logger.debug("Usage stats event {} not sent", event)

        _start(_send)
        return True
    except Exception:
        logger.debug("Usage stats event {} skipped", event)
        return False
