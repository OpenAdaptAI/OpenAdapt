"""Anonymous, zero-dependency CLI usage telemetry for OpenAdapt.

Mirrors ``openadapt_desktop.telemetry`` so CLI and Desktop events land in the
same PostHog project with a shared anonymous ``install_id`` (``~/.openadapt/telemetry_id``)
and identical privacy invariants:

* Honors ``DO_NOT_TRACK=1``, ``OPENADAPT_TELEMETRY=0`` (or other falsy values),
  ``OPENADAPT_TELEMETRY_ENABLED=false``, and ``[privacy].usage_stats = "off"``
  in ``~/.openadapt/config.toml``.
* Closed allowlist of event names, top-level command names, and categorical
  properties. Never captures CLI arguments, file paths, URLs, workflow names,
  error text, or user text.
* Zero third-party dependencies (``urllib.request`` on a background daemon
  thread); never blocks or raises on network failure.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt.version import __version__

#: Public, write-only PostHog project key (shared with OpenAdapt Desktop).
POSTHOG_PROJECT_KEY = "phc_935iWKc6O7u6DCp2eFAmK5WmCwv35QXMa6LulTJ3uqh"
POSTHOG_HOST = "https://us.i.posthog.com"
TIMEOUT_S = 3.0

ALLOWED_EVENTS = frozenset(
    {
        "cli_command_run",
        "cli_quickstart_completed",
    }
)

ALLOWED_COMMANDS = frozenset(
    {
        "quickstart",
        "doctor",
        "onboard",
        "run",
        "record",
        "compile",
        "verify",
        "version",
        "help",
    }
)

ALLOWED_PROPERTIES = frozenset(
    {
        "command",
        "exit_code",
        "duration_ms_bucket",
        "outcome",
    }
)

_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_CI_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "JENKINS_URL",
    "BUILDKITE",
    "CIRCLECI",
)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def os_name() -> str:
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        platform.system(), "other"
    )


def arch_name() -> str:
    machine = platform.machine().lower()
    return _ARCH_ALIASES.get(
        machine, machine if _TOKEN.match(machine or "") else "other"
    )


def python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def is_ci() -> bool:
    return any(bool(os.environ.get(var)) for var in _CI_VARS)


def is_internal_usage() -> bool:
    for var in ("OPENADAPT_INTERNAL", "OPENADAPT_DEV"):
        if _env(var) in _TRUTHY:
            return True
    return is_ci()


def state_dir() -> Path:
    """Shared state directory (``~/.openadapt`` or parent of ``OPENADAPT_CONFIG_TOML``)."""
    cfg = (os.environ.get("OPENADAPT_CONFIG_TOML") or "").strip()
    if cfg:
        return Path(cfg).expanduser().parent
    return Path.home() / ".openadapt"


def _config_usage_stats_off() -> bool:
    cfg_env = (os.environ.get("OPENADAPT_CONFIG_TOML") or "").strip()
    cfg_path = Path(cfg_env).expanduser() if cfg_env else (state_dir() / "config.toml")
    if not cfg_path.is_file():
        return False
    try:
        text = cfg_path.read_text(encoding="utf-8")
        try:
            import tomllib  # Python 3.11+

            data = tomllib.loads(text)
            privacy = data.get("privacy") if isinstance(data, dict) else None
            if isinstance(privacy, dict):
                return privacy.get("usage_stats") == "off"
            return False
        except ImportError:
            return bool(re.search(r'usage_stats\s*=\s*"off"', text))
    except Exception:
        return False


def telemetry_enabled() -> bool:
    """True unless opted out by environment variable or ``config.toml``."""
    try:
        if _env("DO_NOT_TRACK") in _TRUTHY:
            return False
        if _env("OPENADAPT_TELEMETRY") in _FALSY:
            return False
        enabled_env = _env("OPENADAPT_TELEMETRY_ENABLED")
        if enabled_env in _FALSY:
            return False
        if enabled_env in _TRUTHY:
            return True
        if _config_usage_stats_off():
            return False
        return True
    except Exception:
        return False


def install_id() -> str:
    """Random anonymous install UUID shared with OpenAdapt Desktop."""
    path = state_dir() / "telemetry_id"
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = uuid.uuid4().hex
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        return uuid.uuid4().hex


def duration_ms_bucket(elapsed_ms: float) -> str:
    """Bucket a duration in milliseconds into closed categorical ranges."""
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


def normalize_command(command: Optional[str]) -> str:
    """Map a top-level subcommand to the closed allowlist (or ``other``)."""
    token = (command or "").strip().lower()
    if token in ALLOWED_COMMANDS:
        return token
    return "other"


def base_properties() -> dict[str, Any]:
    return {
        "version": __version__,
        "os": os_name(),
        "arch": arch_name(),
        "python_minor": python_minor(),
        "is_internal": is_internal_usage(),
        "is_ci": is_ci(),
        "package": "openadapt",
    }


def sanitize(properties: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return base properties plus only allowlisted, closed caller properties."""
    out = base_properties()
    for key, value in (properties or {}).items():
        if key not in ALLOWED_PROPERTIES or value is None:
            continue
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and _TOKEN.match(value):
            out[key] = value
    return out


def endpoint() -> Optional[tuple[str, str]]:
    key = (os.environ.get("OPENADAPT_POSTHOG_KEY") or "").strip() or POSTHOG_PROJECT_KEY
    host = (os.environ.get("OPENADAPT_POSTHOG_HOST") or "").strip() or POSTHOG_HOST
    if not key.startswith("phc_") or not host.startswith("https://"):
        return None
    return key, host.rstrip("/")


def build_payload(
    event: str, properties: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
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
            "$lib": "openadapt-cli",
            "$process_person_profile": False,
            "$geoip_disable": True,
        },
    }


def _post(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S):
        pass


def _start(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="oa-cli-telemetry", daemon=True).start()


def capture(event: str, properties: Optional[dict[str, Any]] = None) -> bool:
    """Queue one allowlisted event in the background. Never blocks or raises."""
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
                pass

        _start(_send)
        return True
    except Exception:
        return False


def capture_cli_command(
    command: Optional[str], *, exit_code: int, duration_ms: float
) -> bool:
    """Emit ``cli_command_run`` with allowlisted command and bucketed duration."""
    return capture(
        "cli_command_run",
        {
            "command": normalize_command(command),
            "exit_code": int(exit_code),
            "duration_ms_bucket": duration_ms_bucket(duration_ms),
        },
    )


def capture_quickstart_completed(*, ok: bool, duration_ms: float) -> bool:
    """Emit ``cli_quickstart_completed`` with closed outcome and bucketed duration."""
    return capture(
        "cli_quickstart_completed",
        {
            "outcome": "ok" if ok else "failed",
            "duration_ms_bucket": duration_ms_bucket(duration_ms),
        },
    )
