"""Point a real-app lab run at a chaos-proxied port.

Set ``OPENADAPT_LAB_CHAOS`` to a profile in ``profiles.json`` (``vpn-typical``,
``vpn-bad``, ``replica-lag``, ``vpn-bad+replica-lag``) and pass every app URL
through :func:`chaos_url`. A URL whose port is an app's ``upstream_port`` on the
lab host is rewritten to that app's proxied port for the profile; any other URL
is returned unchanged. Unset (or ``none``): no rewriting. Set
``OPENADAPT_LAB_HOST`` to the deployment hostname before requesting chaos;
an absent or placeholder host is refused before a run starts.

    from benchmark.lab_chaos.chaos import chaos_url, active_profile
    base = chaos_url("http://fixture.example:9635")

pytest: ``--chaos <profile>`` sets the variable (see ``pytest_addoption`` below,
loaded with ``-p benchmark.lab_chaos.chaos``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

ENV = "OPENADAPT_LAB_CHAOS"
HOST_ENV = "OPENADAPT_LAB_HOST"
PROFILES_PATH = Path(__file__).with_name("profiles.json")


def load_profiles() -> dict[str, Any]:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


def deployment_host(config: Optional[dict[str, Any]] = None) -> str:
    """Require a hostname configured for this operator's fixture deployment."""

    config = load_profiles() if config is None else config
    host = os.environ.get(HOST_ENV, config.get("host", "")).strip().rstrip(".")
    parts = urlsplit("//" + host)
    if (
        not host
        or host.lower().endswith(".invalid")
        or parts.hostname is None
        or parts.netloc != host
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or ":" in host
        or any(c.isspace() for c in host)
    ):
        raise ValueError(
            f"set {HOST_ENV} to the fixture hostname before requesting chaos"
        )
    return parts.hostname


def _validate_profile(name: str, config: dict[str, Any]) -> None:
    if name not in config["profiles"]:
        raise ValueError(f"{ENV}={name!r} is not a profile in {PROFILES_PATH.name}")
    deployment_host(config)


def active_profile() -> Optional[str]:
    name = os.environ.get(ENV, "").strip()
    if not name or name == "none":
        return None
    _validate_profile(name, load_profiles())
    return name


def chaos_url(url: str, profile: Optional[str] = None) -> str:
    profile = profile or active_profile()
    if profile is None:
        return url
    config = load_profiles()
    _validate_profile(profile, config)
    parts = urlsplit(url)
    if parts.hostname != deployment_host(config):
        return url
    for app in config["apps"].values():
        if parts.port == app["upstream_port"]:
            netloc = f"{parts.hostname}:{app['ports'][profile]}"
            return urlunsplit(parts._replace(netloc=netloc))
    return url


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--chaos",
        default=None,
        help="run real-app lab tests through a chaos profile (profiles.json)",
    )


def pytest_configure(config: Any) -> None:
    profile = config.getoption("--chaos", default=None)
    if profile:
        os.environ[ENV] = profile
    active_profile()  # refuse bad profile/host before test setup, including env configuration
