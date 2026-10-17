"""Pinned local Frappe Lending fixture orchestration.

All commands use argv lists (no host-shell interpolation).  The only mutable
state lives below ``benchmark/frappe_lending/state`` by default.  The fixture
creates a byte-for-byte SQL baseline once, stores its SHA-256, verifies that
digest before every restore, and stops application writers while restoring.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "environment.lock.json"
COMPOSE_PATH = HERE / "compose.yml"
APPS_PATH = HERE / "apps.json"
BOOTSTRAP_PATH = HERE / "bootstrap_fixture.py"
PINNED_CONTAINERFILE = HERE / "Containerfile.pinned"

WRITER_SERVICES = (
    "backend",
    "frontend",
    "websocket",
)

# Exact cardinality contract for the pinned fixture. All three arms ultimately
# call Frappe's normal ``Document.insert`` path. The baseline must already
# contain any naming-series row, so the only accepted row-count change is one
# target row. A live validation may reveal a legitimate pinned-source
# subscriber in a future version, but it remains fail-closed until reviewed and
# added here with an exact arm-specific count. Broad table allowlists hide
# duplicates.
EXPECTED_TABLE_DELTAS: Mapping[str, Mapping[str, int]] = {
    "api": {"tabLoan Application": 1},
    "compiled": {"tabLoan Application": 1},
    "agent": {"tabLoan Application": 1},
}
MIN_BUILD_FREE_BYTES = 40 * 1024**3
SOURCE_LABELS = {
    "frappe": "ai.openadapt.benchmark.frappe.commit",
    "erpnext": "ai.openadapt.benchmark.erpnext.commit",
    "lending": "ai.openadapt.benchmark.lending.commit",
    "frappe_docker": "ai.openadapt.benchmark.frappe_docker.commit",
}


class FixtureError(RuntimeError):
    """Fail-safe fixture/precondition error."""


class FrappeFixture:
    """Build, seed, snapshot, reset, and audit the pinned local stack."""

    def __init__(
        self,
        state_dir: Path | str = HERE / "state",
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        build_engine: str | None = None,
        podman_connection: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.runner = runner
        self.lock = json.loads(LOCK_PATH.read_text())
        self.runtime_env = self.state_dir / "runtime.env"
        self.snapshot_path = self.state_dir / "baseline.sql"
        self.snapshot_hash_path = self.state_dir / "baseline.sql.sha256"
        self.source_dir = self.state_dir / "frappe_docker"
        self.build_engine = build_engine or os.environ.get(
            "OPENADAPT_BUILD_ENGINE", "docker"
        )
        self.podman_connection = podman_connection or os.environ.get(
            "OPENADAPT_PODMAN_CONNECTION"
        )
        if self.build_engine not in {"docker", "podman"}:
            raise FixtureError(
                "OPENADAPT_BUILD_ENGINE must be exactly 'docker' or 'podman'"
            )

    def _protect_state_dir(self) -> None:
        """Create a real, private state directory for secrets and evidence."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise FixtureError("fixture state path must be a real directory")
        self.state_dir.chmod(0o700)

    @staticmethod
    def _write_private_atomic_new(path: Path, payload: bytes) -> None:
        """Atomically publish one new 0600 file without overwrite or exposure."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short protected-file write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # link(2) is an atomic create-if-absent publication. Unlike
            # replace(2), it cannot overwrite evidence created by a racer.
            os.link(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError as exc:
            raise FixtureError(f"refusing overwrite of protected file: {path}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _validate_lock(self) -> None:
        """Fail if any source/app/image pin is missing or internally inconsistent."""
        for name, item in self.lock["upstreams"].items():
            if not re.fullmatch(r"[0-9a-f]{40}", item["commit"]):
                raise FixtureError(f"{name} is not pinned to a full Git commit")
        for name, image in self.lock["services"].items():
            if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
                raise FixtureError(f"{name} is not pinned to an OCI SHA-256 digest")
        apps = json.loads(APPS_PATH.read_text())
        expected_apps = [
            {
                "url": self.lock["upstreams"][name]["url"],
                "branch": self.lock["upstreams"][name]["ref"],
            }
            for name in ("erpnext", "lending")
        ]
        if apps != expected_apps:
            raise FixtureError(
                "apps.json does not match the locked ERPNext/Lending refs"
            )
        containerfile = PINNED_CONTAINERFILE.read_text()
        for name in ("frappe_build", "frappe_base"):
            if self.lock["services"][name] not in containerfile:
                raise FixtureError(f"pinned Containerfile omits locked {name} image")

    def _validate_runtime_env(self) -> None:
        """Refuse a stale env whose image references differ from the lock."""
        if not self.runtime_env.exists():
            raise FixtureError("runtime.env is absent; run prepare first")
        values = dict(
            line.split("=", 1)
            for line in self.runtime_env.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        expected = {
            "BENCHMARK_IMAGE": self.lock["image"],
            "MARIADB_IMAGE": self.lock["services"]["mariadb"],
            "REDIS_IMAGE": self.lock["services"]["redis"],
        }
        for key, value in expected.items():
            if values.get(key) != value:
                raise FixtureError(
                    f"runtime.env {key} differs from environment.lock.json; "
                    "use a new state directory"
                )
        self.runtime_env.chmod(0o600)

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:8080"

    def _run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        capture: bool = True,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            result = self.runner(
                list(argv),
                input=input_bytes,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=False,
                env=merged_env,
                timeout=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureError(f"command unavailable or timed out: {argv[0]}") from exc
        if result.returncode:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise FixtureError(f"command failed ({argv[0]}): {stderr[-2000:]}")
        return result

    def _compose(self, *args: str, input_bytes: bytes | None = None) -> bytes:
        if not self.runtime_env.exists():
            raise FixtureError("runtime.env is absent; run prepare first")
        argv = [
            "docker",
            "compose",
            "--project-name",
            "openadapt-frappe-lending",
            "--env-file",
            str(self.runtime_env),
            "--file",
            str(COMPOSE_PATH),
            *args,
        ]
        return self._run(argv, input_bytes=input_bytes).stdout or b""

    def _validate_tools(self, *, engine: str | None = None) -> None:
        selected = engine or "docker"
        for tool in ("git", selected):
            if shutil.which(tool) is None:
                raise FixtureError(f"required tool is unavailable: {tool}")

    def _engine_prefix(self, engine: str) -> list[str]:
        prefix = [engine]
        if engine == "podman" and self.podman_connection:
            prefix.extend(["--connection", self.podman_connection])
        return prefix

    def _container_store_write_probe(self, engine: str) -> None:
        """Exercise the selected engine's real internal content store.

        Host ``disk_usage`` cannot observe Docker Desktop or remote Podman
        storage. Importing and deleting a unique 1 MiB scratch layer catches
        the hidden-store ENOSPC/corruption failure before the large build.
        """
        prefix = self._engine_prefix(engine)
        tag = f"{self.lock['image']}-capacity-preflight"
        present = self._run(
            [*prefix, "image", "ls", "--quiet", tag], timeout_s=15
        ).stdout.strip()
        if present:
            raise FixtureError(
                f"refusing to replace existing container preflight image {tag}"
            )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            payload = b"0" * (1024 * 1024)
            info = tarfile.TarInfo("openadapt-capacity-probe")
            info.size = len(payload)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(payload))
        imported = False
        try:
            import_argv = (
                [*prefix, "image", "import", "-", tag]
                if engine == "docker"
                else [*prefix, "import", "-", tag]
            )
            self._run(
                import_argv,
                input_bytes=archive.getvalue(),
                timeout_s=60,
            )
            imported = True
            observed = self._run(
                [*prefix, "image", "ls", "--quiet", tag], timeout_s=15
            ).stdout.strip()
            if not observed:
                raise FixtureError("container capacity probe image was not retained")
        finally:
            if imported:
                self._run([*prefix, "image", "rm", tag], timeout_s=30)

    def runtime_preflight(
        self, *, require_build_space: bool, engine: str = "docker"
    ) -> None:
        """Fail closed before an engine build/pull or stack start.

        Frappe's source image build is intentionally not attempted when the
        daemon is unhealthy or the fixture filesystem has less than 40 GiB
        free. This avoids turning a benchmark setup problem into disk pressure
        or a hanging launch.
        """
        if engine not in {"docker", "podman"}:
            raise FixtureError(f"unsupported container engine: {engine}")
        self._validate_tools(engine=engine)
        prefix = self._engine_prefix(engine)
        version_template = (
            "{{.ServerVersion}}" if engine == "docker" else "{{.Version.Version}}"
        )
        try:
            version = self._run(
                [*prefix, "info", "--format", version_template], timeout_s=15
            ).stdout.strip()
            if not version or version == b"<no value>":
                raise FixtureError("engine info omitted server version")
        except FixtureError as exc:
            raise FixtureError(f"{engine} engine is not responsive: {exc}") from exc
        free = shutil.disk_usage(self.state_dir.parent).free
        if require_build_space and free < MIN_BUILD_FREE_BYTES:
            raise FixtureError(
                "refusing the Frappe image build: "
                f"{free / 1024**3:.1f} GiB free, at least "
                f"{MIN_BUILD_FREE_BYTES / 1024**3:.0f} GiB required"
            )
        self._run([*prefix, "system", "df"], timeout_s=30)
        self._container_store_write_probe(engine)

    def _verify_tag(self, name: str, item: Mapping[str, str]) -> None:
        ref = item["ref"]
        result = self._run(
            [
                "git",
                "ls-remote",
                "--tags",
                item["url"],
                f"refs/tags/{ref}",
                f"refs/tags/{ref}^{{}}",
            ]
        )
        rows = [line.split() for line in result.stdout.decode().splitlines() if line]
        commits = {sha for sha, _ref in rows}
        if item["commit"] not in commits:
            raise FixtureError(
                f"{name} {ref} does not resolve to locked commit {item['commit']}"
            )

    def verify_upstreams(self) -> None:
        """Verify advertised application tags resolve to the locked commits."""
        self._validate_tools(engine=self.build_engine)
        for name in ("frappe", "erpnext", "lending"):
            self._verify_tag(name, self.lock["upstreams"][name])

    def prepare(self) -> None:
        """Clone exact orchestration source and create protected runtime env."""
        self._validate_lock()
        self._validate_tools(engine=self.build_engine)
        self.verify_upstreams()
        self._protect_state_dir()
        docker_lock = self.lock["upstreams"]["frappe_docker"]
        if not self.source_dir.exists():
            self._run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    docker_lock["url"],
                    str(self.source_dir),
                ]
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(self.source_dir),
                    "checkout",
                    "--detach",
                    docker_lock["commit"],
                ]
            )
        head = (
            self._run(["git", "-C", str(self.source_dir), "rev-parse", "HEAD"])
            .stdout.decode()
            .strip()
        )
        if head != docker_lock["commit"]:
            raise FixtureError(
                f"existing frappe_docker source is {head}, expected {docker_lock['commit']}"
            )
        if not self.runtime_env.exists():
            values = {
                "BENCHMARK_IMAGE": self.lock["image"],
                "MARIADB_IMAGE": self.lock["services"]["mariadb"],
                "REDIS_IMAGE": self.lock["services"]["redis"],
                "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(24),
                "SITE_ADMIN_PASSWORD": secrets.token_urlsafe(24),
                "SITE_NAME": self.lock["site"],
                "FRAPPE_PORT": "8080",
            }
            self._write_private_atomic_new(
                self.runtime_env,
                "".join(f"{key}={value}\n" for key, value in values.items()).encode(),
            )
        self._validate_runtime_env()

    def build(self) -> None:
        """Build the custom image from exact verified application tags."""
        self.runtime_preflight(require_build_space=True, engine=self.build_engine)
        self.prepare()
        frappe = self.lock["upstreams"]["frappe"]
        if self.build_engine == "podman":
            build_argv = ["podman"]
            if self.podman_connection:
                build_argv.extend(["--connection", self.podman_connection])
            build_argv.append("build")
        else:
            build_argv = ["docker", "build"]
        try:
            self._run(
                [
                    *build_argv,
                    "--build-arg",
                    f"FRAPPE_PATH={frappe['url']}",
                    "--build-arg",
                    f"FRAPPE_BRANCH={frappe['ref']}",
                    "--build-arg",
                    f"FRAPPE_COMMIT={frappe['commit']}",
                    "--build-arg",
                    f"ERPNEXT_COMMIT={self.lock['upstreams']['erpnext']['commit']}",
                    "--build-arg",
                    f"LENDING_COMMIT={self.lock['upstreams']['lending']['commit']}",
                    "--build-arg",
                    "FRAPPE_DOCKER_COMMIT="
                    f"{self.lock['upstreams']['frappe_docker']['commit']}",
                    "--build-arg",
                    f"FRAPPE_BUILD_IMAGE={self.lock['services']['frappe_build']}",
                    "--build-arg",
                    f"FRAPPE_BASE_IMAGE={self.lock['services']['frappe_base']}",
                    "--secret",
                    f"id=apps_json,src={APPS_PATH}",
                    "--tag",
                    self.lock["image"],
                    "--file",
                    str(PINNED_CONTAINERFILE),
                    str(self.source_dir),
                ],
                capture=False,
            )
        finally:
            # Podman's remote client can leave a mode-0600 transfer copy beside
            # the build context. Delete only exact public apps.json copies; an
            # unrelated file sharing its prefix is never touched.
            for candidate in self.state_dir.glob("podman-build-secret-*"):
                is_exact_copy = (
                    candidate.is_file()
                    and candidate.read_bytes() == APPS_PATH.read_bytes()
                )
                if is_exact_copy:
                    candidate.unlink()

    def up(self, *, wait_s: float = 300.0) -> None:
        """Start the stack and fail unless its HTTP surface becomes ready."""
        self._validate_lock()
        self._validate_runtime_env()
        self.runtime_preflight(require_build_space=False)
        self.image_identity()
        self._compose("up", "--detach")

        self._wait_http_ready(wait_s=wait_s)

    def _wait_http_ready(self, *, wait_s: float = 300.0) -> None:
        """Wait for the real login page after writer services restart."""
        import httpx

        deadline = time.monotonic() + wait_s
        last = ""
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{self.base_url}/login", timeout=5)
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - retain last readiness error
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
        raise FixtureError(f"Frappe did not become ready within {wait_s}s: {last}")

    def image_identity(self) -> dict[str, Any]:
        """Verify source labels and return the exact custom image identity."""
        raw = self._run(["docker", "image", "inspect", self.lock["image"]]).stdout
        item = json.loads(raw)[0]
        labels = (item.get("Config") or {}).get("Labels") or {}
        source_labels: dict[str, str] = {}
        for upstream, label in SOURCE_LABELS.items():
            expected = self.lock["upstreams"][upstream]["commit"]
            observed = labels.get(label)
            if observed != expected:
                raise FixtureError(
                    f"custom image source label {label} is {observed!r}, "
                    f"expected {expected}; rebuild the pinned image"
                )
            source_labels[label] = observed
        return {
            "reference": self.lock["image"],
            "id": item.get("Id"),
            "repo_digests": item.get("RepoDigests", []),
            "source_labels": source_labels,
        }

    def bootstrap(self) -> None:
        """Seed only synthetic masters/users using the pinned app helpers."""
        # IPython executes redirected source one cell at a time and continues
        # after exceptions. Compile/exec the entire bootstrap as one cell so an
        # early failure cannot run later mutations or emit a false sentinel.
        source = BOOTSTRAP_PATH.read_text()
        script = (
            "namespace = {'__name__': '__openadapt_frappe_fixture__'}; "
            f"exec(compile({source!r}, {str(BOOTSTRAP_PATH)!r}, 'exec'), "
            "namespace, namespace)\n"
        ).encode()
        output = self._compose(
            "exec",
            "-T",
            "backend",
            "bench",
            "--site",
            self.lock["site"],
            "console",
            input_bytes=script,
        )
        if re.search(
            rb"(?:^|In \[\d+\]: )OPENADAPT_FRAPPE_FIXTURE_READY\r?$",
            output,
            re.MULTILINE,
        ) is None:
            raise FixtureError("fixture bootstrap did not emit its readiness sentinel")

    def _site_db_name(self) -> str:
        raw = self._compose(
            "exec",
            "-T",
            "backend",
            "cat",
            f"sites/{self.lock['site']}/site_config.json",
        )
        name = str(json.loads(raw)["db_name"])
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise FixtureError("site database name contains unsafe characters")
        return name

    def snapshot(self) -> str:
        """Create the immutable baseline SQL snapshot and return its digest."""
        if self.snapshot_path.exists() or self.snapshot_hash_path.exists():
            raise FixtureError(
                "baseline snapshot state already exists; refusing overwrite; "
                "use a new --state directory for a new benchmark baseline"
            )
        db_name = self._site_db_name()
        digest = ""
        self._compose("stop", *WRITER_SERVICES)
        try:
            sql = self._compose(
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                "exec mariadb-dump --user=root "
                '--password="$MARIADB_ROOT_PASSWORD" --skip-comments '
                '--single-transaction --databases "$1"',
                "openadapt-dump",
                db_name,
            )
            digest = hashlib.sha256(sql).hexdigest()
            self._protect_state_dir()
            self._write_private_atomic_new(self.snapshot_path, sql)
            self._write_private_atomic_new(
                self.snapshot_hash_path, f"{digest}  baseline.sql\n".encode()
            )
        finally:
            self._compose("start", *WRITER_SERVICES)
        self._wait_http_ready()
        return digest

    def baseline_sha256(self) -> str:
        """Verify and return the recorded baseline digest."""
        if not self.snapshot_path.exists() or not self.snapshot_hash_path.exists():
            raise FixtureError("baseline snapshot is absent; run snapshot first")
        expected = self.snapshot_hash_path.read_text().split()[0]
        observed = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()
        if observed != expected:
            raise FixtureError("baseline SQL digest mismatch; refusing reset")
        return observed

    def reset(self) -> str:
        """Restore the exact hashed DB baseline before one trial."""
        digest = self.baseline_sha256()
        sql = self.snapshot_path.read_bytes()
        self._compose("stop", *WRITER_SERVICES)
        try:
            self._compose(
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'exec mariadb --user=root --password="$MARIADB_ROOT_PASSWORD"',
                input_bytes=sql,
            )
            # The database is not the whole application state: Frappe caches
            # records/session metadata and workers queue jobs in fixture-only
            # Redis. Flush both stores before bringing writers back.
            self._compose("exec", "-T", "redis-cache", "redis-cli", "FLUSHALL")
            self._compose("exec", "-T", "redis-queue", "redis-cli", "FLUSHALL")
        finally:
            self._compose("start", *WRITER_SERVICES)
        self._wait_http_ready()
        return digest

    def db_records(self) -> list[dict[str, Any]]:
        """Read canonical target rows directly from MariaDB (read-only SQL)."""
        db_name = self._site_db_name()
        query = (
            "SELECT JSON_OBJECT("
            "'name',name,'applicant',applicant,'applicant_type',applicant_type,"
            "'applicant_email_address',applicant_email_address,"
            "'applicant_phone_number',applicant_phone_number,"
            "'company',company,'loan_product',loan_product,"
            "'loan_amount',CAST(loan_amount AS CHAR),"
            "'repayment_method',repayment_method,"
            "'repayment_periods',CAST(repayment_periods AS CHAR),"
            "'is_term_loan',CAST(is_term_loan AS CHAR),"
            "'rate_of_interest',CAST(rate_of_interest AS CHAR),"
            "'docstatus',CAST(docstatus AS CHAR)) "
            "FROM `tabLoan Application` "
            "WHERE applicant='OpenAdapt Synthetic Applicant' ORDER BY name;"
        )
        raw = self._compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'exec mariadb --user=root --password="$MARIADB_ROOT_PASSWORD" '
            '--batch --raw --skip-column-names "$1" --execute "$2"',
            "openadapt-query",
            db_name,
            query,
        )
        return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    def non_target_loan_applications_sha256(self) -> str:
        """Digest every column of every non-target Loan Application row.

        The table-count contract catches inserts/deletes. This independent raw
        row digest also catches count-neutral updates to unrelated applications
        inside the target table. Other tables remain bounded by exact row-count
        deltas; this benchmark does not claim full database change-data capture.
        """
        db_name = self._site_db_name()
        query = (
            "SELECT * FROM `tabLoan Application` "
            "WHERE COALESCE(applicant,'') <> 'OpenAdapt Synthetic Applicant' "
            "ORDER BY name;"
        )
        raw = self._compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'exec mariadb --user=root --password="$MARIADB_ROOT_PASSWORD" '
            '--batch --raw --skip-column-names "$1" --execute "$2"',
            "openadapt-non-target-loan-digest",
            db_name,
            query,
        )
        return hashlib.sha256(raw).hexdigest()

    def table_counts(self) -> dict[str, int]:
        """Return exact per-table row counts for the DB delta audit."""
        db_name = self._site_db_name()
        query = (
            "SET SESSION group_concat_max_len=1000000; "
            "SELECT GROUP_CONCAT(CONCAT('SELECT ', QUOTE(table_name), "
            "', COUNT(*) FROM `', REPLACE(table_name,'`','``'),'`') "
            "ORDER BY table_name SEPARATOR ' UNION ALL ') INTO @q "
            "FROM information_schema.tables WHERE table_schema=DATABASE() "
            "AND table_type='BASE TABLE'; PREPARE stmt FROM @q; "
            "EXECUTE stmt; DEALLOCATE PREPARE stmt;"
        )
        raw = self._compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'exec mariadb --user=root --password="$MARIADB_ROOT_PASSWORD" '
            '--batch --raw --skip-column-names "$1" --execute "$2"',
            "openadapt-counts",
            db_name,
            query,
        )
        counts: dict[str, int] = {}
        for line in raw.decode().splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or not parts[1].isdigit():
                raise FixtureError("exact table-count audit returned a malformed row")
            table, count = parts
            if table in counts:
                raise FixtureError("exact table-count audit returned a duplicate table")
            counts[table] = int(count)
        if not counts:
            raise FixtureError("exact table-count audit returned no tables")
        return counts


def audit_table_deltas(
    before: Mapping[str, int], after: Mapping[str, int], *, arm: str
) -> tuple[list[str], dict[str, int]]:
    """Return exact-contract violations and the complete table delta map."""
    if arm not in EXPECTED_TABLE_DELTAS:
        raise FixtureError(f"no database delta contract for arm {arm!r}")
    expected = EXPECTED_TABLE_DELTAS[arm]
    all_deltas = {
        table: after.get(table, 0) - before.get(table, 0)
        for table in sorted(set(before) | set(after))
    }
    violations: list[str] = []
    for table, required in expected.items():
        observed = all_deltas.get(table, 0)
        if observed != required:
            violations.append(f"{table}:{observed:+d} (expected {required:+d})")
    for table, observed in all_deltas.items():
        if table not in expected and observed != 0:
            violations.append(f"{table}:{observed:+d} (expected +0)")
    return violations, all_deltas


def unexpected_table_deltas(
    before: Mapping[str, int], after: Mapping[str, int], *, arm: str
) -> list[str]:
    """Compatibility wrapper returning exact-contract violations only."""
    return audit_table_deltas(before, after, arm=arm)[0]
