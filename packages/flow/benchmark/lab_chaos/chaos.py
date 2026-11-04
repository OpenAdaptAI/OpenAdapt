"""Point a real-app lab run at a chaos-proxied port.

Set ``OPENADAPT_LAB_CHAOS`` to a profile in ``profiles.json`` (``vpn-typical``,
``vpn-bad``, ``replica-lag``, ``vpn-bad+replica-lag``) and pass every app URL
through :func:`chaos_url`. A URL whose port is an app's ``upstream_port`` on the
lab host is rewritten to that app's proxied port for the profile; any other URL
is returned unchanged. Unset (or ``none``): no rewriting.

    from benchmark.lab_chaos.chaos import chaos_url, active_profile
    base = chaos_url("http://100.111.126.101:9635")

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
PROFILES_PATH = Path(__file__).with_name("profiles.json")


def load_profiles() -> dict[str, Any]:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


def active_profile() -> Optional[str]:
    name = os.environ.get(ENV, "").strip()
    if not name or name == "none":
        return None
    if name not in load_profiles()["profiles"]:
        raise ValueError(f"{ENV}={name!r} is not a profile in {PROFILES_PATH.name}")
    return name


def chaos_url(url: str, profile: Optional[str] = None) -> str:
    profile = profile or active_profile()
    if profile is None:
        return url
    config = load_profiles()
    parts = urlsplit(url)
    if parts.hostname != config["host"]:
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
        active_profile()  # fail early on an unknown name
