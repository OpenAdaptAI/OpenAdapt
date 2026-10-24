"""This proof's own OpenEMR copy: its Compose project, ports, reset and app change.

The pinned fixture is ``benchmark/openemr_local`` (OpenEMR 8.0.0.3, synthetic
data only). This copy runs as its own Compose project on loopback ports, so it
never touches another session's copy, following the lifecycle soak's adapter
(``benchmark/lifecycle/apps.py``). The app change is the soak's ``relabel``
narrowed to the save button: "Confirm Create New Patient" becomes "Save new
patient", served to every client as a vendor release would be.

Environment (all optional):

- ``LIFECYCLE_PROOF_STATE``: the fixture state directory;
- ``LIFECYCLE_PROOF_PROJECT``: the Compose project
  (default ``openadapt-openemr-compose``);
- ``LIFECYCLE_PROOF_UI_PORT`` / ``LIFECYCLE_PROOF_API_PORT`` (9401 / 9400).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

PROJECT = os.environ.get("LIFECYCLE_PROOF_PROJECT", "openadapt-openemr-compose")
UI_PORT = os.environ.get("LIFECYCLE_PROOF_UI_PORT", "9401")
API_PORT = os.environ.get("LIFECYCLE_PROOF_API_PORT", "9400")
DEFAULT_STATE = Path.home() / "oa" / "scratch" / "compose-proof" / "openemr-state"
WEBROOT = "/var/www/localhost/htdocs/openemr"
POPUP = f"{WEBROOT}/interface/new/new_search_popup.php"
SQL_FIELDS = (
    "id",
    "pid",
    "fname",
    "lname",
    "DOB",
    "email",
)


def podman_env() -> dict[str, str]:
    """Docker Compose pointed at the benchmark Podman machine."""

    env = dict(os.environ)
    if not env.get("DOCKER_HOST"):
        sock = subprocess.run(
            [
                "podman",
                "machine",
                "inspect",
                "openadapt-benchmark",
                "--format",
                "{{.ConnectionInfo.PodmanSocket.Path}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if sock:
            env["DOCKER_HOST"] = f"unix://{sock}"
    return env


def make_fixture(state_dir: Path) -> Any:
    import benchmark.openemr_local.fixture as fx

    class ProofFixture(fx.OpenEMRFixture):
        def __init__(self, state: Path) -> None:
            super().__init__(state)
            self.lock = {**self.lock, "project": PROJECT}

        @property
        def ui_base_url(self) -> str:
            return f"http://127.0.0.1:{UI_PORT}"

        @property
        def api_base_url(self) -> str:
            return f"https://127.0.0.1:{API_PORT}"

        def _expected(self) -> dict[str, str]:
            return {
                "OPENEMR_IMAGE": self.lock["services"]["openemr"],
                "MARIADB_IMAGE": self.lock["services"]["mariadb"],
                "OPENEMR_ACTOR_USER": "openadapt_actor",
                "OPENEMR_HTTP_PORT": UI_PORT,
                "OPENEMR_HTTPS_PORT": API_PORT,
            }

        def _validate_runtime_env(self) -> None:
            values = self._runtime_values()
            for key, value in self._expected().items():
                if values.get(key) != value:
                    raise fx.FixtureError(f"runtime.env {key} differs")
            self.runtime_env.chmod(0o600)

        def runtime_preflight(self) -> None:
            # The shared machine's rules (~/oa/scratch/fixture-lease/README.md).
            free = shutil.disk_usage("/System/Volumes/Data").free / 2**30
            if free < 15:
                raise fx.FixtureError(f"only {free:.1f} GiB free on the host")
            if os.getloadavg()[0] >= 40:
                raise fx.FixtureError("the 1-minute load is over 40")

        def prepare(self) -> None:
            import secrets

            self._protect_state_dir()
            if not self.runtime_env.exists():
                values = {
                    **self._expected(),
                    "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
                    "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
                    "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
                }
                self._write_private_exclusive(
                    self.runtime_env,
                    (
                        "# generated local synthetic fixture; never commit\n"
                        + "".join(f"{k}={v}\n" for k, v in values.items())
                    ).encode(),
                )
            super().prepare()

        def _run(self, argv, *, input_bytes=None, timeout_s=300.0):  # type: ignore[override]
            if argv[:1] == ["docker"]:
                proc = subprocess.run(
                    argv,
                    input=input_bytes,
                    capture_output=True,
                    timeout=timeout_s,
                    env=podman_env(),
                    check=False,
                )
                if proc.returncode != 0:
                    raise fx.FixtureError(
                        f"{' '.join(argv[:4])} failed: {proc.stderr.decode()[-800:]}"
                    )
                return proc
            return super()._run(argv, input_bytes=input_bytes, timeout_s=timeout_s)

    return ProofFixture(state_dir)


class ProofApp:
    """Bring-up, the oracle's SQL read, and the app change."""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = Path(
            state_dir or os.environ.get("LIFECYCLE_PROOF_STATE") or DEFAULT_STATE
        )
        self.fx = make_fixture(self.state_dir)
        self.db_container = f"{PROJECT}-db-1"
        self.app_container = f"{PROJECT}-openemr-1"

    def bootstrapped(self) -> bool:
        return all(
            p.is_file()
            for p in (
                self.fx.runtime_env,
                self.fx.clients_path,
                self.fx.snapshot_path,
                self.fx.snapshot_hash_path,
            )
        )

    def bring_up(self, say: Any = print) -> str:
        if self.bootstrapped():
            say("starting the existing copy")
            self.fx._compose("up", "-d", timeout_s=900.0)
            self.fx._wait_ready(timeout_s=900.0)
            return "restarted"
        self.fx.runtime_preflight()
        say("checking the pinned OpenEMR release")
        self.fx.prepare()
        self.fx.image_identity()
        say("starting; OpenEMR installs itself on first start")
        self.fx._compose("up", "-d", timeout_s=900.0)
        self.fx._wait_ready(timeout_s=1800.0)
        self.fx._verify_running_source()
        say("adding synthetic API clients")
        self.fx.bootstrap()
        self.fx.snapshot()
        return "created"

    def point_hooks_here(self) -> None:
        """verify.yaml's hooks (sign-in, reset, REST read-back) read these."""

        lock = self.state_dir / "environment.lock.json"
        lock.write_text(json.dumps(self.fx.lock, indent=2) + "\n")
        os.environ["OPENEMR_FIXTURE_STATE"] = str(self.state_dir)
        os.environ["OPENEMR_FIXTURE_LOCK"] = str(lock)
        os.environ["OPENEMR_UI_URL"] = self.fx.ui_base_url
        os.environ["OPENEMR_API_URL"] = self.fx.api_base_url
        sock = podman_env().get("DOCKER_HOST")
        if sock:
            os.environ["DOCKER_HOST"] = sock

    def _exec_bytes(
        self, container: str, script: str, *, stdin: bytes = b"", timeout: float = 120
    ) -> bytes:
        proc = subprocess.run(
            ["podman", "exec", "-i", container, "sh", "-c", script],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"podman exec failed: {proc.stderr.decode()[-400:]}")
        return proc.stdout

    def _exec(self, container: str, script: str, *, stdin: bytes = b"") -> str:
        return self._exec_bytes(container, script, stdin=stdin).decode()

    def rows(self) -> list[dict[str, str]]:
        """Every chart, read directly from the database (the oracle)."""

        out = self._exec(
            self.db_container,
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot --batch --raw '
            "--skip-column-names",
            stdin=(
                "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data "
                "ORDER BY id;"
            ).encode(),
        )
        return [
            {
                k: ("" if v == "NULL" else v)
                for k, v in zip(SQL_FIELDS, line.split("\t"))
            }
            for line in out.splitlines()
            if line
        ]

    def charts_for(self, email: str) -> int:
        return sum(1 for row in self.rows() if row["email"] == email)

    def dump(self) -> Path:
        """The current charts, for putting back after a re-qualification."""

        path = self.state_dir / "proof-data.sql"
        # The dump is bytes: OpenEMR's install data is not all UTF-8.
        sql = self._exec_bytes(
            self.db_container,
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot '
            "--single-transaction --triggers openemr",
            timeout=600,
        )
        path.write_bytes(sql)
        path.chmod(0o600)
        return path

    def restore(self, path: Path) -> None:
        self._exec_bytes(
            self.db_container,
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot openemr',
            stdin=path.read_bytes(),
            timeout=600,
        )

    def relabel_save(self) -> dict[str, str]:
        """The vendor change: the save button gets a new label."""

        self._exec(
            self.app_container,
            f"[ -f {POPUP}.oa-orig ] || cp -p {POPUP} {POPUP}.oa-orig; "
            f"sed -i \"s/xla('Confirm Create New Patient')/xla('Save new patient')/\" "
            f"{POPUP}",
        )
        return {"Confirm Create New Patient": "Save new patient"}

    def undo_changes(self) -> None:
        self._exec(
            self.app_container,
            f"[ -f {POPUP}.oa-orig ] && cp -p {POPUP}.oa-orig {POPUP}; true",
        )

    def stop(self) -> None:
        self.fx._compose("stop", timeout_s=300.0)
