"""The onboarding benchmark's own OpenEMR instance.

It reuses the pinned fixture in ``benchmark/openemr_local`` (same image
digests, same SQL-baseline reset, same OAuth roles) under its own compose
project and ports, so it never touches another session's stack:

* project ``openadapt-openemr-onboard``;
* UI ``http://127.0.0.1:9481``, API ``https://127.0.0.1:9480``;
* state (generated secrets, SQL baseline) under ``$OE_SCRATCH/openemr-state``,
  outside the repository.

OpenEMR is GPL-3.0. It runs as a separate service; no OpenEMR file is stored
in this repository.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from benchmark.openemr_local import fixture as fx

PROJECT = "openadapt-openemr-onboard"
HTTP_PORT = "9481"
HTTPS_PORT = "9480"
MIN_FREE_BYTES = 10 * 1024**3


def state_dir() -> Path:
    root = os.environ.get("OE_SCRATCH", str(Path.home() / "oa/scratch/onboarding-effort"))
    return Path(root) / "openemr-state"


class OnboardOpenEMR(fx.OpenEMRFixture):
    """The pinned OpenEMR fixture under this benchmark's project and ports."""

    def __init__(self, state: Path | str | None = None) -> None:
        super().__init__(state or state_dir())
        self.lock = dict(self.lock)
        self.lock["project"] = PROJECT

    @property
    def ui_base_url(self) -> str:
        return f"http://127.0.0.1:{HTTP_PORT}"

    @property
    def api_base_url(self) -> str:
        return f"https://127.0.0.1:{HTTPS_PORT}"

    def prepare(self) -> None:
        self._validate_lock()
        self._protect_state_dir()
        if not self.runtime_env.exists():
            import secrets

            values = {
                "OPENEMR_IMAGE": self.lock["services"]["openemr"],
                "MARIADB_IMAGE": self.lock["services"]["mariadb"],
                "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_ACTOR_USER": "openadapt_actor",
                "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_HTTP_PORT": HTTP_PORT,
                "OPENEMR_HTTPS_PORT": HTTPS_PORT,
            }
            payload = (
                "# generated local synthetic fixture; never commit\n"
                + "".join(f"{k}={v}\n" for k, v in values.items())
            ).encode()
            self._write_private_exclusive(self.runtime_env, payload)
        self._validate_runtime_env()

    def _validate_runtime_env(self) -> None:
        values = self._runtime_values()
        expected = {
            "OPENEMR_IMAGE": self.lock["services"]["openemr"],
            "MARIADB_IMAGE": self.lock["services"]["mariadb"],
            "OPENEMR_ACTOR_USER": "openadapt_actor",
            "OPENEMR_HTTP_PORT": HTTP_PORT,
            "OPENEMR_HTTPS_PORT": HTTPS_PORT,
        }
        for name, value in expected.items():
            if values.get(name) != value:
                raise fx.FixtureError(f"runtime.env {name} differs from the lock")
        for secret_name in (
            "MARIADB_ROOT_PASSWORD",
            "OPENEMR_DB_PASSWORD",
            "OPENEMR_ACTOR_PASSWORD",
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", values.get(secret_name, "")):
                raise fx.FixtureError(f"runtime.env {secret_name} malformed")
        self.runtime_env.chmod(0o600)

    def runtime_preflight(self) -> None:
        self._validate_lock()
        parent = self.state_dir
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if shutil.disk_usage(parent).free < MIN_FREE_BYTES:
            raise fx.FixtureError("less than 10 GiB free on the host")
        self._run(["docker", "version"], timeout_s=30.0)

    def up(self) -> None:
        self.runtime_preflight()
        self._compose("up", "-d", timeout_s=900.0)
        self._wait_ready(timeout_s=900.0)
        self._verify_running_source()

    def stop(self) -> None:
        self._compose("stop", timeout_s=300.0)

    # -- direct SQL for oracles and seeding (root, read/write) ---------------

    def sql(self, statement: str, *, timeout_s: float = 120.0) -> list[list[str]]:
        """Run SQL as root and return tab-split rows (benchmark use only)."""
        out = self._db(statement, timeout_s=timeout_s).decode("utf-8", "replace")
        return [line.split("\t") for line in out.splitlines() if line]

    def actor_credentials(self) -> dict[str, Any]:
        values = self._runtime_values()
        return {
            "username": values["OPENEMR_ACTOR_USER"],
            "password": values["OPENEMR_ACTOR_PASSWORD"],
        }


_instance: OnboardOpenEMR | None = None


def instance() -> OnboardOpenEMR:
    global _instance
    if _instance is None:
        _instance = OnboardOpenEMR()
    return _instance
