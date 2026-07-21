"""Pinned local OpenEMR fixture orchestration.

No command uses a host shell.  Mutable state is confined to ``state_dir`` and
project-scoped Docker volumes.  A byte-for-byte SQL baseline is created once,
hashed, verified before every restore, and restored only while the OpenEMR
writer container is stopped.

The fixture was live-validated on 2026-07-16 through a disposable loopback-only
Podman VM while the host Docker backing filesystem remained preserved and
unavailable. Every live assumption has a hard check; no missing check is
converted into a benchmark success.
"""

from __future__ import annotations

import base64
import binascii
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
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "environment.lock.json"
COMPOSE_PATH = HERE / "compose.yml"

# Exact cardinality contracts measured against the pinned image and reviewed
# against its source paths. The counts include OpenEMR's own immutable audit,
# UUID, recent-patient, user-setting, contact, clinical-rule, and API-access
# subscribers. They remain exact and arm-specific: any additional, missing, or
# shifted row fails closed instead of disappearing behind a broad allowlist.
EXPECTED_TABLE_DELTAS: Mapping[str, Mapping[str, int]] = {
    "api": {
        "api_log": 2,
        "log": 16,
        "log_comment_encrypt": 16,
        "patient_data": 1,
        "uuid_mapping": 1,
        "uuid_registry": 2,
    },
    "compiled": {
        # Governed replay performs one pre-effect capture, one read per
        # declared effect, and the independent post-run capture. The pinned
        # OpenEMR audit subscriber records those 13 reads and their paired
        # field-level audit rows in addition to the browser save itself.
        "api_log": 13,
        "clinical_rules_log": 1,
        "contact": 1,
        "history_data": 1,
        "log": 311,
        "log_comment_encrypt": 311,
        "patient_data": 1,
        "recent_patients": 1,
        "user_settings": 2,
        "uuid_mapping": 12,
        "uuid_registry": 14,
    },
    "agent": {
        "api_log": 1,
        "clinical_rules_log": 1,
        "contact": 1,
        "history_data": 1,
        "log": 229,
        "log_comment_encrypt": 229,
        "patient_data": 1,
        "recent_patients": 1,
        "user_settings": 2,
        "uuid_mapping": 12,
        "uuid_registry": 14,
    },
}
MIN_FREE_BYTES = 15 * 1024**3
ACTOR_SCOPE = "openid api:oemr user/patient.crus"
ORACLE_SCOPE = "openid api:oemr user/patient.rs"


class FixtureError(RuntimeError):
    """A fixture precondition failed; callers must not claim a trial result."""


def _jwt_scope_set(token: str) -> set[str]:
    """Read the issued token's scope claim for an exact local boundary check.

    This is not a substitute for signature validation. The pinned OpenEMR API
    validates the token on every oracle/actor request; here we additionally
    refuse a token whose issued privilege set differs from the benchmark role.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise FixtureError("OAuth access_token was not a three-part JWT")
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError("OAuth access_token payload was malformed") from exc
    scopes = claims.get("scopes") if isinstance(claims, dict) else None
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(item, str) or not item for item in scopes)
    ):
        raise FixtureError("OAuth access_token omitted a valid scopes claim")
    if len(scopes) != len(set(scopes)):
        raise FixtureError("OAuth access_token contained duplicate scopes")
    return set(scopes)


class OpenEMRFixture:
    """Prepare, seed, snapshot, reset, and audit the pinned OpenEMR stack."""

    def __init__(
        self,
        state_dir: Path | str = HERE / "state",
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.runner = runner
        self.lock = json.loads(LOCK_PATH.read_text())
        self.runtime_env = self.state_dir / "runtime.env"
        self.clients_path = self.state_dir / "oauth-clients.json"
        self.snapshot_path = self.state_dir / "baseline.sql"
        self.snapshot_hash_path = self.state_dir / "baseline.sql.sha256"

    def _protect_state_dir(self) -> None:
        """Create the fixture state directory without group/other access."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise FixtureError("fixture state path must be a real directory")
        self.state_dir.chmod(0o700)

    @staticmethod
    def _write_private_exclusive(path: Path, payload: bytes) -> None:
        """Atomically create a 0600 file; never expose or overwrite evidence."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise FixtureError(f"refusing overwrite of protected file: {path}") from exc
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short protected-file write")
                view = view[written:]
            os.fsync(fd)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)

    @property
    def ui_base_url(self) -> str:
        """HTTP is loopback-only and avoids trusting the fixture TLS cert in UI."""
        return "http://127.0.0.1:9301"

    @property
    def api_base_url(self) -> str:
        """OAuth and REST use the official image's self-signed HTTPS endpoint."""
        return "https://127.0.0.1:9300"

    def _validate_lock(self) -> None:
        if self.lock.get("schema_version") != 1:
            raise FixtureError("unsupported environment lock schema")
        for name, item in self.lock["upstreams"].items():
            if not re.fullmatch(r"[0-9a-f]{40}", item.get("commit", "")):
                raise FixtureError(f"{name} is not pinned to a full Git commit")
        for name, image in self.lock["services"].items():
            if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
                raise FixtureError(f"{name} is not pinned to an OCI index digest")
        proofs = self.lock.get("source_proofs", {})
        if len(proofs) < 4:
            raise FixtureError("source identity requires multiple pinned file proofs")
        for path, digest in proofs.items():
            if not path.startswith("/var/www/localhost/htdocs/openemr/"):
                raise FixtureError(f"source proof escapes OpenEMR root: {path}")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise FixtureError(f"source proof for {path} is not SHA-256")

    def _run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_s: float = 120.0,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = self.runner(
                list(argv),
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                check=False,
                env=dict(env) if env is not None else None,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureError(f"command unavailable or timed out: {argv[0]}") from exc
        if completed.returncode != 0:
            # Some bootstrap commands print credentials on stdout. Never copy
            # command output into the exception or benchmark logs.
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            safe = stderr[:500] if "secret" not in stderr.lower() else "redacted"
            raise FixtureError(
                f"command failed ({completed.returncode}): {argv[0]}: {safe}"
            )
        return completed

    def _compose(
        self, *args: str, input_bytes: bytes | None = None, timeout_s: float = 300.0
    ) -> subprocess.CompletedProcess[bytes]:
        self._validate_runtime_env()
        return self._run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.runtime_env),
                "-f",
                str(COMPOSE_PATH),
                "-p",
                self.lock["project"],
                *args,
            ],
            input_bytes=input_bytes,
            timeout_s=timeout_s,
        )

    def _runtime_values(self) -> dict[str, str]:
        if not self.runtime_env.exists():
            raise FixtureError("runtime.env is absent; run prepare first")
        values = dict(
            line.split("=", 1)
            for line in self.runtime_env.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        return values

    def _validate_runtime_env(self) -> None:
        values = self._runtime_values()
        expected = {
            "OPENEMR_IMAGE": self.lock["services"]["openemr"],
            "MARIADB_IMAGE": self.lock["services"]["mariadb"],
            "OPENEMR_ACTOR_USER": "openadapt_actor",
            "OPENEMR_HTTP_PORT": "9301",
            "OPENEMR_HTTPS_PORT": "9300",
        }
        for name, value in expected.items():
            if values.get(name) != value:
                raise FixtureError(f"runtime.env {name} differs from the lock")
        for secret_name in (
            "MARIADB_ROOT_PASSWORD",
            "OPENEMR_DB_PASSWORD",
            "OPENEMR_ACTOR_PASSWORD",
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", values.get(secret_name, "")):
                raise FixtureError(f"runtime.env {secret_name} is absent or malformed")
        self.runtime_env.chmod(0o600)

    @staticmethod
    def _digest_from_image(image: str) -> str:
        return image.rsplit("@", 1)[1]

    def runtime_preflight(self) -> None:
        """Refuse an unresponsive daemon or low-space state filesystem."""
        self._validate_lock()
        parent = self.state_dir
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if shutil.disk_usage(parent).free < MIN_FREE_BYTES:
            raise FixtureError("less than 15 GiB free for the local fixture")
        self._run(["docker", "version"], timeout_s=15.0)
        self._run(["docker", "info"], timeout_s=15.0)
        # This still cannot promise free space inside Docker Desktop's hidden
        # VM filesystem, but it forces the daemon to enumerate its real object
        # store and catches the observed corrupt/ENOSPC failure earlier than an
        # application container start.  The digest pull remains the definitive
        # write test and is never converted into a benchmark result.
        self._run(["docker", "system", "df"], timeout_s=30.0)
        self._docker_storage_write_probe()
        self._run(["docker", "compose", "version"], timeout_s=15.0)

    def _docker_storage_write_probe(self) -> None:
        """Write and remove a tiny image layer in the daemon's real store.

        Host ``disk_usage`` cannot see Docker Desktop's hidden ext4 volume—the
        filesystem that previously returned ENOSPC.  Importing a 1 MiB scratch
        layer exercises that exact content store without pulling or running a
        container.  The unique tag must be absent before the probe, and only
        that tag is removed afterward.
        """
        tag = f"{self.lock['project']}:capacity-preflight"
        present = self._run(
            ["docker", "image", "ls", "--quiet", tag], timeout_s=15.0
        ).stdout.strip()
        if present:
            raise FixtureError(
                f"refusing to replace existing Docker preflight image {tag}"
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
            self._run(
                ["docker", "image", "import", "-", tag],
                input_bytes=archive.getvalue(),
                timeout_s=60.0,
            )
            imported = True
            observed = self._run(
                ["docker", "image", "ls", "--quiet", tag], timeout_s=15.0
            ).stdout.strip()
            if not observed:
                raise FixtureError("Docker capacity probe image was not retained")
        finally:
            if imported:
                self._run(["docker", "image", "rm", tag], timeout_s=30.0)

    def _verify_remote_source(self, name: str) -> None:
        item = self.lock["upstreams"][name]
        if name == "openemr":
            remote_ref = f"refs/tags/{item['ref']}^{{}}"
        else:
            remote_ref = f"refs/heads/{item['ref']}"
        output = self._run(
            ["git", "ls-remote", item["url"], remote_ref], timeout_s=60.0
        ).stdout.decode()
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if rows != [[item["commit"], remote_ref]]:
            raise FixtureError(
                f"{name} remote ref no longer resolves to the locked commit"
            )

    def _verify_remote_image(self, image: str) -> None:
        output = self._run(
            ["docker", "buildx", "imagetools", "inspect", image],
            timeout_s=60.0,
        ).stdout.decode()
        digest = self._digest_from_image(image)
        if not re.search(rf"^Digest:\s+{re.escape(digest)}$", output, re.MULTILINE):
            raise FixtureError(f"remote image identity did not match {digest}")

    def _verify_remote_source_proofs(self) -> None:
        """Bind every installed-file proof to the exact locked Git commit."""
        commit = self.lock["upstreams"]["openemr"]["commit"]
        prefix = "/var/www/localhost/htdocs/openemr/"
        for installed_path, expected in self.lock["source_proofs"].items():
            relative = installed_path.removeprefix(prefix)
            url = (
                f"https://raw.githubusercontent.com/openemr/openemr/{commit}/{relative}"
            )
            try:
                request = Request(url, headers={"User-Agent": "OpenAdapt-benchmark"})
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    payload = response.read()
            except Exception as exc:  # noqa: BLE001 - identity failure is fatal
                raise FixtureError(
                    f"could not read locked OpenEMR source proof: {relative}"
                ) from exc
            if hashlib.sha256(payload).hexdigest() != expected:
                raise FixtureError(
                    f"locked source proof differs from commit for {relative}"
                )

    def prepare(self) -> None:
        """Verify remote identities and create a protected runtime env once."""
        self._validate_lock()
        self._protect_state_dir()
        self._verify_remote_source("openemr")
        self._verify_remote_source_proofs()
        for image in self.lock["services"].values():
            self._verify_remote_image(image)
        if not self.runtime_env.exists():
            values = {
                "OPENEMR_IMAGE": self.lock["services"]["openemr"],
                "MARIADB_IMAGE": self.lock["services"]["mariadb"],
                "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_ACTOR_USER": "openadapt_actor",
                "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_HTTP_PORT": "9301",
                "OPENEMR_HTTPS_PORT": "9300",
            }
            payload = (
                "# generated local synthetic fixture; never commit\n"
                + "".join(f"{key}={value}\n" for key, value in values.items())
            ).encode()
            self._write_private_exclusive(
                self.runtime_env,
                payload,
            )
        self._validate_runtime_env()

    def image_identity(self) -> dict[str, Any]:
        """Verify the locally pulled image is the locked multi-arch digest."""
        image = self.lock["services"]["openemr"]
        payload = json.loads(
            self._run(["docker", "image", "inspect", image], timeout_s=30.0).stdout
        )
        if len(payload) != 1:
            raise FixtureError("OpenEMR image inspect returned no unique image")
        repo_digest = "openemr/openemr@" + self._digest_from_image(image)
        # Docker reports the familiar unqualified repository while Podman's
        # Docker-compatible API normalizes Docker Hub references to
        # ``docker.io/...``. Accept only those two spellings, with the exact
        # locked index digest; a different registry or digest still fails.
        accepted_repo_digests = {
            repo_digest,
            "docker.io/" + repo_digest,
        }
        observed_repo_digests = payload[0].get("RepoDigests", [])
        if not accepted_repo_digests.intersection(observed_repo_digests):
            raise FixtureError("local OpenEMR image lacks the locked RepoDigest")
        return {
            "id": payload[0].get("Id", ""),
            "repo_digest": repo_digest,
            "observed_repo_digests": observed_repo_digests,
            "source_commit": self.lock["upstreams"]["openemr"]["commit"],
        }

    def _verify_running_source(self) -> dict[str, str]:
        paths = list(self.lock["source_proofs"])
        output = self._compose(
            "exec", "-T", "openemr", "sha256sum", *paths, timeout_s=30.0
        ).stdout.decode()
        observed: dict[str, str] = {}
        for line in output.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                observed[parts[1].lstrip("* ")] = parts[0]
        if observed != self.lock["source_proofs"]:
            raise FixtureError(
                "running OpenEMR source proofs do not match release commit; "
                "refusing the fixture"
            )
        return observed

    def source_identity(self) -> dict[str, str]:
        """Return installed source proofs after checking every locked digest."""
        return self._verify_running_source()

    def _wait_ready(self, timeout_s: float = 300.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                self._compose(
                    "exec",
                    "-T",
                    "openemr",
                    "/usr/bin/curl",
                    "--fail",
                    "--insecure",
                    "--silent",
                    "https://localhost/meta/health/readyz",
                    timeout_s=15.0,
                )
                return
            except FixtureError:
                # Startup 404/connection refusal is expected until the image's
                # installation entrypoint finishes. The bounded deadline turns
                # a persistent failure into one explicit refusal.
                pass
            time.sleep(2.0)
        raise FixtureError("OpenEMR health endpoint did not become ready")

    def up(self) -> None:
        """Start only pinned services, then verify image and source identity."""
        self.runtime_preflight()
        self._compose("pull", timeout_s=600.0)
        self.image_identity()
        self._compose("up", "-d", timeout_s=600.0)
        self._wait_ready()
        self._verify_running_source()

    def _db(
        self,
        sql: str | None = None,
        *,
        input_bytes: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> bytes:
        argv = [
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb '
            '-uroot --batch --raw --skip-column-names "$@"',
            "openadapt-mariadb",
        ]
        if sql is not None:
            argv.extend(["-e", sql])
        return self._compose(*argv, input_bytes=input_bytes, timeout_s=timeout_s).stdout

    def _db_lines(self, sql: str) -> list[str]:
        return [line for line in self._db(sql).decode().splitlines() if line]

    def _client_ids(self) -> set[str]:
        return set(
            self._db_lines(
                "SELECT client_id FROM openemr.oauth_clients "
                "WHERE client_name LIKE 'OpenEMR API Test Client %' ORDER BY client_id"
            )
        )

    def _register_client(self) -> str:
        before = self._client_ids()
        # This command prints a generated secret. Output is captured in memory,
        # never logged, and the benchmark uses password grant by client ID only.
        self._compose(
            "exec",
            "-T",
            "openemr",
            "php",
            "/var/www/localhost/htdocs/openemr/bin/console",
            "openemr-dev:register-api-test-client",
            "--site=default",
            timeout_s=60.0,
        )
        created = self._client_ids() - before
        if len(created) != 1:
            raise FixtureError("API client bootstrap did not create exactly one client")
        return created.pop()

    @staticmethod
    def _sql_quote(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_:/ .-]+", value):
            raise FixtureError("unsafe fixture SQL literal")
        return "'" + value.replace("'", "''") + "'"

    def bootstrap(self) -> None:
        """Enable local APIs and create distinct least-privilege OAuth clients."""
        if self.clients_path.exists() or self.snapshot_path.exists():
            raise FixtureError(
                "fixture already bootstrapped; use a new state directory"
            )
        self._verify_running_source()
        globals_sql = [
            ("rest_api", "1"),
            ("rest_fhir_api", "1"),
            ("oauth_password_grant", "1"),
            ("oauth_app_manual_approval", "1"),
            ("site_addr_oath", self.api_base_url),
            ("simplified_demographics", "1"),
            ("omit_employers", "1"),
        ]
        statements = []
        for name, value in globals_sql:
            statements.append(
                "INSERT INTO openemr.globals (gl_name,gl_index,gl_value) VALUES "
                f"({self._sql_quote(name)},0,{self._sql_quote(value)}) "
                "ON DUPLICATE KEY UPDATE gl_value=VALUES(gl_value)"
            )
        self._db(";".join(statements) + ";")

        existing = self._db_lines(
            "SELECT COUNT(*) FROM openemr.patient_data "
            "WHERE lname='LoanParity' OR email='openadapt.loan-parity@example.invalid'"
        )
        if existing != ["0"]:
            raise FixtureError("synthetic target already exists before baseline")

        actor_client = self._register_client()
        oracle_client = self._register_client()
        for client_id, scope in (
            (actor_client, ACTOR_SCOPE),
            (oracle_client, ORACLE_SCOPE),
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", client_id):
                raise FixtureError("generated OAuth client ID has unexpected format")
            self._db(
                "UPDATE openemr.oauth_clients SET "
                f"scope={self._sql_quote(scope)}, grant_types='password', "
                "is_enabled=1 WHERE client_id="
                f"{self._sql_quote(client_id)};"
            )
        clients = {
            "actor_client_id": actor_client,
            "actor_scope": ACTOR_SCOPE,
            "oracle_client_id": oracle_client,
            "oracle_scope": ORACLE_SCOPE,
        }
        self._protect_state_dir()
        self._write_private_exclusive(
            self.clients_path,
            (json.dumps(clients, indent=2, sort_keys=True) + "\n").encode(),
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def snapshot(self) -> str:
        """Create the one immutable SQL baseline; never overwrite evidence."""
        if self.snapshot_path.exists() or self.snapshot_hash_path.exists():
            raise FixtureError("refusing overwrite of existing baseline snapshot state")
        if not self.clients_path.exists():
            raise FixtureError("bootstrap OAuth clients before taking the baseline")
        dump = self._compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot "$@"',
            "openadapt-mariadb-dump",
            "--single-transaction",
            "--routines",
            "--events",
            "--triggers",
            "--databases",
            self.lock["database"],
            timeout_s=300.0,
        ).stdout
        if len(dump) < 1024:
            raise FixtureError("database dump was implausibly small")
        self._protect_state_dir()
        self._write_private_exclusive(self.snapshot_path, dump)
        digest = self._sha256(self.snapshot_path)
        self._write_private_exclusive(self.snapshot_hash_path, (digest + "\n").encode())
        return digest

    def baseline_hash(self) -> str:
        if not self.snapshot_path.exists() or not self.snapshot_hash_path.exists():
            raise FixtureError("hashed baseline is incomplete")
        expected = self.snapshot_hash_path.read_text().strip()
        actual = self._sha256(self.snapshot_path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
            raise FixtureError("baseline SQL hash mismatch")
        return actual

    def reset(self) -> str:
        """Restore the verified baseline while application writers are stopped."""
        digest = self.baseline_hash()
        self._compose("stop", "openemr", timeout_s=60.0)
        self._db(
            "DROP DATABASE IF EXISTS openemr; "
            "CREATE DATABASE openemr CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        )
        self._db(input_bytes=self.snapshot_path.read_bytes(), timeout_s=300.0)
        self._compose("start", "openemr", timeout_s=120.0)
        self._wait_ready()
        self._verify_running_source()
        return digest

    def token_session(self, role: str) -> Any:
        """Issue one short-lived bearer token with an exact, role-specific scope."""
        if role not in {"actor", "oracle"}:
            raise FixtureError("OAuth role must be actor or oracle")
        if not self.clients_path.exists():
            raise FixtureError("OAuth client identity is absent; run bootstrap")
        clients = json.loads(self.clients_path.read_text())
        scope = ACTOR_SCOPE if role == "actor" else ORACLE_SCOPE
        client_id = clients[f"{role}_client_id"]
        values = self._runtime_values()

        import httpx

        # ``httpx`` is a core openadapt-flow dependency. Keep this benchmark
        # runnable from the base environment instead of silently depending on
        # the optional ``dev`` extra's requests package. TLS is loopback-only
        # with the fixture image's self-signed certificate; image/source pins
        # and loopback binding provide the benchmark identity boundary.
        session = httpx.Client(verify=False, timeout=30)
        response = session.post(
            f"{self.api_base_url}/oauth2/default/token",
            data={
                "grant_type": "password",
                "client_id": client_id,
                "scope": scope,
                "user_role": "users",
                "username": values["OPENEMR_ACTOR_USER"],
                "password": values["OPENEMR_ACTOR_PASSWORD"],
            },
        )
        if response.status_code != 200:
            raise FixtureError(
                f"{role} token request returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FixtureError(f"{role} token response was not JSON") from exc
        requested_scope = set(scope.split())
        # Pinned OpenEMR v8.0.0.3 includes ``api:oemr`` in the signed JWT's
        # ``scopes`` claim but omits that base API capability from the token
        # response's display-oriented ``scope`` string. Check both exact forms.
        returned_scope = set(str(payload.get("scope", "")).split())
        if returned_scope != requested_scope - {"api:oemr"}:
            raise FixtureError(
                f"{role} token scope differs from the exact requested scope"
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or len(token) < 40:
            raise FixtureError(f"{role} token response omitted access_token")
        if _jwt_scope_set(token) != requested_scope:
            raise FixtureError(
                f"{role} access token claim differs from the exact requested scope"
            )
        session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def table_counts(self) -> dict[str, int]:
        """Exact row counts for every OpenEMR table (not engine estimates)."""
        tables = self._db_lines(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA='openemr' AND TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME"
        )
        for table in tables:
            if not re.fullmatch(r"[A-Za-z0-9_ -]+", table):
                raise FixtureError(f"unexpected table name from catalog: {table!r}")
        if not tables:
            raise FixtureError("OpenEMR catalog contained no base tables")
        # One database/container round trip keeps the full 60-trial matrix
        # practical while still using exact COUNT(*), never engine estimates.
        union = " UNION ALL ".join(
            f"SELECT '{table}', COUNT(*) FROM openemr.`{table}`" for table in tables
        )
        counts: dict[str, int] = {}
        for row in self._db_lines(union):
            parts = row.split("\t")
            if len(parts) != 2 or parts[0] not in tables or not parts[1].isdigit():
                raise FixtureError("exact table-count audit returned a malformed row")
            counts[parts[0]] = int(parts[1])
        if set(counts) != set(tables):
            raise FixtureError("exact table-count audit omitted one or more tables")
        return counts

    def db_records(self) -> list[dict[str, str]]:
        """Direct SQL readback of the target fields and identifiers."""
        fields = (
            "id",
            "pid",
            "LOWER(HEX(uuid))",
            "title",
            "fname",
            "lname",
            "DOB",
            "sex",
            "street",
            "city",
            "state",
            "postal_code",
            "phone_home",
            "email",
            "country_code",
        )
        output = self._db(
            "SELECT " + ",".join(fields) + " FROM openemr.patient_data "
            "WHERE lname='LoanParity' AND "
            "email='openadapt.loan-parity@example.invalid' ORDER BY id"
        ).decode()
        names = (
            "id",
            "pid",
            "uuid",
            "title",
            "fname",
            "lname",
            "DOB",
            "sex",
            "street",
            "city",
            "state",
            "postal_code",
            "phone_home",
            "email",
            "country_code",
        )
        records: list[dict[str, str]] = []
        for line in output.splitlines():
            values = line.split("\t")
            if len(values) != len(names):
                raise FixtureError("SQL patient row had an unexpected column count")
            records.append(
                {
                    name: "" if value == "NULL" else value
                    for name, value in zip(names, values)
                }
            )
        return records

    def non_target_patient_data_sha256(self) -> str:
        """Digest every non-target patient row in deterministic dump order.

        This closes the net-row-count gap: target + collateral insert + hidden
        delete could otherwise leave ``patient_data`` at the expected +1.
        CREATE/AUTO_INCREMENT metadata is excluded so the digest changes only
        when a non-target row changes.
        """
        dump = self._compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot "$@"',
            "openadapt-patient-data-audit",
            "--skip-comments",
            "--compact",
            "--no-create-info",
            "--skip-extended-insert",
            "--order-by-primary",
            "--where=NOT (lname <=> 'LoanParity' AND "
            "email <=> 'openadapt.loan-parity@example.invalid')",
            self.lock["database"],
            "patient_data",
            timeout_s=120.0,
        ).stdout
        return hashlib.sha256(dump).hexdigest()

    def history_data_sha256(self, *, exclude_pid: int | None = None) -> str:
        """Digest all history rows, optionally excluding one exact target PID."""
        argv = [
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot "$@"',
            "openadapt-history-data-audit",
            "--skip-comments",
            "--compact",
            "--no-create-info",
            "--skip-extended-insert",
            "--order-by-primary",
        ]
        if exclude_pid is not None:
            if exclude_pid < 0:
                raise FixtureError("target PID must be non-negative")
            argv.append(f"--where=pid IS NULL OR pid <> {exclude_pid}")
        argv.extend([self.lock["database"], "history_data"])
        dump = self._compose(*argv, timeout_s=120.0).stdout
        return hashlib.sha256(dump).hexdigest()

    def history_count_for_pid(self, pid: int) -> int:
        """Return the exact auxiliary-row count for one target PID."""
        if pid < 0:
            raise FixtureError("target PID must be non-negative")
        rows = self._db_lines(
            f"SELECT COUNT(*) FROM openemr.history_data WHERE pid={pid}"
        )
        if len(rows) != 1 or not rows[0].isdigit():
            raise FixtureError("history_data target count was malformed")
        return int(rows[0])


def audit_table_deltas(
    before: Mapping[str, int], after: Mapping[str, int], *, arm: str
) -> tuple[list[str], dict[str, int]]:
    """Return fail-closed contract violations and the complete delta map.

    Every table present in either snapshot is included in ``all_deltas`` so
    evidence cannot discard a zero or non-zero change.  The only accepted
    non-zero values are the exact, arm-specific cardinalities above.
    """
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
    """Compatibility wrapper returning only exact-contract violations."""
    return audit_table_deltas(before, after, arm=arm)[0]
