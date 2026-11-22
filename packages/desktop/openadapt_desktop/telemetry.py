"""Anonymous usage stats for OpenAdapt Desktop, sent only after consent.

Nothing is sent until the person answers "Share" in the first-launch question
(or in Settings). The answer is stored in ``[privacy].usage_stats`` of
``~/.openadapt/config.toml`` (see :mod:`openadapt_desktop.privacy_settings`).
``DO_NOT_TRACK=1``, ``OPENADAPT_TELEMETRY=0`` (or other falsy values), or
``OPENADAPT_TELEMETRY_ENABLED=false`` always turns it off.

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
        "workflow_recorded",
        "workflow_compiled",
        "workflow_run_completed",
        "phone_approval_resolved",
        "receipt_exported",
    }
)

#: Per-event properties a caller may add. Strings must be short enum tokens.
ALLOWED_PROPERTIES = frozenset(
    {
        "surface",
        "mode",
        "outcome",
        "error_code",
        "update_available",
        "step_count_bucket",
        "duration_bucket",
        "duration_ms_bucket",
        "success",
        "verification_mode",
        "decision",
        "latency_bucket",
        "format",
    }
)

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


_SURFACES = frozenset({"browser", "desktop", "native", "hybrid", "controller", "engine"})
_RUN_OUTCOMES = frozenset({"VERIFIED", "STOPPED", "FAILED"})
_VERIFICATION_MODES = frozenset({"read_back", "same_screen", "visual_veto", "none"})
_APPROVAL_DECISIONS = frozenset({"approved", "denied", "expired", "cancelled"})
_EXPORT_FORMATS = frozenset({"json", "cbor", "card", "pdf", "bundle"})


def step_count_bucket(count: int) -> str:
    n = max(0, int(count))
    if n == 0:
        return "0"
    if n <= 5:
        return "1_5"
    if n <= 20:
        return "6_20"
    if n <= 50:
        return "21_50"
    return "gt_50"


def duration_bucket(elapsed_s: float) -> str:
    s = max(0.0, float(elapsed_s))
    if s < 10:
        return "lt_10s"
    if s < 60:
        return "10s_60s"
    if s < 300:
        return "1m_5m"
    return "gt_5m"


def duration_ms_bucket(elapsed_ms: float) -> str:
    ms = max(0.0, float(elapsed_ms))
    if ms < 100:
        return "lt_100ms"
    if ms < 1_000:
        return "100ms_1s"
    if ms < 10_000:
        return "1s_10s"
    if ms < 60_000:
        return "10s_60s"
    return "gt_60s"


def latency_bucket(latency_s: float) -> str:
    s = max(0.0, float(latency_s))
    if s < 5:
        return "lt_5s"
    if s < 30:
        return "5s_30s"
    if s < 120:
        return "30s_2m"
    return "gt_2m"


def workflow_recorded(*, step_count: int, duration_s: float, surface: str) -> bool:
    """Emit ``workflow_recorded`` with strictly categorical, non-PII properties."""
    norm_surface = (surface or "").strip().lower()
    return capture(
        "workflow_recorded",
        {
            "step_count_bucket": step_count_bucket(step_count),
            "duration_bucket": duration_bucket(duration_s),
            "surface": norm_surface if norm_surface in _SURFACES else "other",
        },
    )


def workflow_compiled(*, success: bool, step_count: int) -> bool:
    """Emit ``workflow_compiled`` with strictly categorical, non-PII properties."""
    return capture(
        "workflow_compiled",
        {
            "success": bool(success),
            "step_count_bucket": step_count_bucket(step_count),
        },
    )


def workflow_run_completed(
    *, outcome: str, verification_mode: str, duration_ms: float
) -> bool:
    """Emit ``workflow_run_completed`` with closed outcome (VERIFIED|STOPPED|FAILED)."""
    norm_outcome = (outcome or "").strip().upper()
    norm_mode = (verification_mode or "").strip().lower()
    return capture(
        "workflow_run_completed",
        {
            "outcome": norm_outcome if norm_outcome in _RUN_OUTCOMES else "FAILED",
            "verification_mode": (
                norm_mode if norm_mode in _VERIFICATION_MODES else "other"
            ),
            "duration_ms_bucket": duration_ms_bucket(duration_ms),
        },
    )


def phone_approval_resolved(*, decision: str, latency_s: float) -> bool:
    """Emit ``phone_approval_resolved`` with closed decision and latency bucket."""
    norm_decision = (decision or "").strip().lower()
    return capture(
        "phone_approval_resolved",
        {
            "decision": (
                norm_decision if norm_decision in _APPROVAL_DECISIONS else "other"
            ),
            "latency_bucket": latency_bucket(latency_s),
        },
    )


def receipt_exported(*, format: str) -> bool:
    """Emit ``receipt_exported`` with closed export format."""
    norm_fmt = (format or "").strip().lower()
    return capture(
        "receipt_exported",
        {
            "format": norm_fmt if norm_fmt in _EXPORT_FORMATS else "other",
        },
    )
