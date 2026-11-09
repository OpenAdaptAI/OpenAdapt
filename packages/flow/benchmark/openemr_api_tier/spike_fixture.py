"""A private copy of the pinned local OpenEMR fixture for the API-tier spike.

Same pinned images, upstream compose file, bootstrap and reset as
``benchmark/openemr_local``; its own compose project, ports and state
directory, so it does not disturb fixtures other studies are using.

State (runtime secrets, OAuth client IDs, SQL baseline) lives outside the
repository in ``$API_SPIKE_STATE`` (default
``~/oa/scratch/api-spike-fixture/state``). Every record in it is synthetic.

    python -m benchmark.openemr_api_tier.spike_fixture create   # once
    python -m benchmark.openemr_api_tier.spike_fixture up       # start + reset
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import benchmark.openemr_local.fixture as fx

PROJECT = "openadapt-openemr-apispike"
UI_PORT = os.environ.get("API_SPIKE_UI_PORT", "9811")
API_PORT = os.environ.get("API_SPIKE_API_PORT", "9810")
STATE = Path(
    os.environ.get(
        "API_SPIKE_STATE",
        str(Path.home() / "oa/scratch/api-spike-fixture/state"),
    )
)


class SpikeFixture(fx.OpenEMRFixture):
    def __init__(self, state_dir: Path = STATE) -> None:
        super().__init__(state_dir)
        self.lock = dict(self.lock, project=PROJECT)

    @property
    def ui_base_url(self) -> str:
        return f"http://127.0.0.1:{UI_PORT}"

    @property
    def api_base_url(self) -> str:
        return f"https://127.0.0.1:{API_PORT}"

    def _validate_runtime_env(self) -> None:
        v = self._runtime_values()
        assert v.get("OPENEMR_HTTP_PORT") == UI_PORT
        assert v.get("OPENEMR_HTTPS_PORT") == API_PORT
        assert v.get("OPENEMR_IMAGE") == self.lock["services"]["openemr"]
        assert v.get("MARIADB_IMAGE") == self.lock["services"]["mariadb"]
        self.runtime_env.chmod(0o600)

    def write_runtime_env(self) -> None:
        values = {
            "OPENEMR_IMAGE": self.lock["services"]["openemr"],
            "MARIADB_IMAGE": self.lock["services"]["mariadb"],
            "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
            "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
            "OPENEMR_ACTOR_USER": "openadapt_actor",
            "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
            "OPENEMR_HTTP_PORT": UI_PORT,
            "OPENEMR_HTTPS_PORT": API_PORT,
        }
        self._protect_state_dir()
        self._write_private_exclusive(
            self.runtime_env,
            (
                "# generated local synthetic fixture; never commit\n"
                + "".join(f"{k}={v}\n" for k, v in values.items())
            ).encode(),
        )

    def start(self) -> None:
        """Start without pulling (the pinned images are already local)."""
        self.image_identity()
        self._compose("up", "-d", timeout_s=600.0)
        self._wait_ready()
        self._verify_running_source()


def main(argv: list[str]) -> int:
    f = SpikeFixture()
    cmd = argv[0] if argv else "up"
    if cmd == "create":
        f.write_runtime_env()
        f.start()
        f.bootstrap()
        print("baseline", f.snapshot())
    elif cmd == "up":
        f.start()
        print("reset", f.reset()[:12])
    elif cmd == "down":
        f._compose("stop", timeout_s=120.0)
    else:
        raise SystemExit(f"unknown command {cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
