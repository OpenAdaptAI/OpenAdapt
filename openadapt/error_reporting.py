"""Optional crash reporting for the OpenAdapt CLI.

Reports unhandled errors to a Sentry-compatible backend (GlitchTip). Designed to
be safe and quiet:

- **Off unless it's a packaged build.** By default reporting is active only in a
  frozen/packaged build (the released app). Source / dev / CI / headless runs do
  NOT report -- that source-run flooding is what previously buried real issues.
  Set ``OPENADAPT_ERROR_REPORTING_FORCE=1`` to report from a source run (e.g. to
  test it). This replaces the old, fragile ``active_branch == "main"`` gate.
- **Master switch:** ``OPENADAPT_ERROR_REPORTING_ENABLED=0`` disables entirely.
- **Filtered:** unactionable unsupported-environment errors (no display, wrong
  platform, broken native deps) are dropped in ``before_send``.
- **Never raises:** if ``sentry-sdk`` is missing or init fails, it is a silent
  no-op -- telemetry must never break the app.

Config lives on ``openadapt.config.settings`` (``OPENADAPT_ERROR_REPORTING_*``).
"""

from __future__ import annotations

import sys
from typing import Optional

from openadapt.config import settings
from openadapt.version import __version__

try:
    import sentry_sdk
except ImportError:  # optional dependency -> reporting becomes a no-op
    sentry_sdk = None  # type: ignore[assignment]


# Unactionable: errors from environments where OpenAdapt cannot run -- headless /
# no display, wrong platform, or a broken/partial native dependency set. These
# flood the dashboard from CI and automated/headless environments and bury real
# issues. Drop them.
_UNSUPPORTED_ENV_SUBSTRINGS = (
    "failed to acquire X connection",
    "Bad display name",
    "DISPLAY not set",
    "this platform is not supported",
    "No module named 'pywinauto'",
    "module 'pywinauto' has no attribute",
    "_ARRAY_API not found",
)


def is_unsupported_environment_error(event: dict, hint: dict) -> bool:
    """Return True if the event is an unactionable unsupported-environment error."""
    texts = []
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[1] is not None:
        texts.append(f"{type(exc_info[1]).__name__}: {exc_info[1]}")
    for value in (event.get("exception", {}) or {}).get("values", []) or []:
        texts.append(f"{value.get('type')}: {value.get('value')}")
    blob = " ".join(texts)
    return any(sub in blob for sub in _UNSUPPORTED_ENV_SUBSTRINGS)


def before_send_event(event: dict, hint: dict) -> Optional[dict]:
    """Drop unactionable unsupported-environment events; keep the rest."""
    if is_unsupported_environment_error(event, hint):
        return None
    return event


def _reporting_enabled() -> bool:
    """Report only from a packaged build by default (never source/dev/CI/headless)."""
    if not settings.error_reporting_enabled:
        return False
    if getattr(sys, "frozen", False):
        return True
    return settings.error_reporting_force


def configure_error_reporting() -> None:
    """Initialize crash reporting if enabled. Safe to call always; never raises."""
    if (
        sentry_sdk is None
        or not _reporting_enabled()
        or not settings.error_reporting_dsn
    ):
        return
    try:
        sentry_sdk.init(
            dsn=settings.error_reporting_dsn,
            release=f"openadapt@{__version__}",
            environment=settings.error_reporting_environment,
            before_send=before_send_event,
            ignore_errors=[KeyboardInterrupt],
        )
    except Exception:
        # Telemetry must never break the app.
        pass
