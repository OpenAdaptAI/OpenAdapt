"""A local Medplum (FHIR R4) server and web app with synthetic patients.

The second FHIR system for the second-installation benchmark
(benchmark/second_installation). Medplum is Apache-2.0; its images are pulled
by digest at run time (environment.lock.json) and nothing of it is stored here.

- The web app (``ui_url``) is what a clerk uses: it creates a Patient through
  Medplum's resource form.
- The FHIR API (``fhir_url``) is the record channel: the run's record check and
  duplicate check read it with a client-credentials token, as a deployment
  would.
- The oracle reads Postgres from inside the database container, a channel the
  product never uses.

Every published port binds to loopback. Generated secrets live in
``state_dir`` (outside the repository). All data is synthetic.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "compose.yml"
PROJECT = "openadapt-si-medplum"
SERVER_PORT = 9410
APP_PORT = 9411
DEFAULT_STATE = Path.home() / "oa/scratch/second-installation/medplum-state"

#: Medplum's seeded first administrator (a fixture credential on a
#: loopback-only server with synthetic data).
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "medplum_admin"


class FixtureError(RuntimeError):
    pass


def _http(
    method: str,
    url: str,
    *,
    body: Any = None,
    form: Optional[dict[str, str]] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Any:
    headers = {"Accept": "application/fhir+json, application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise FixtureError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else None


class MedplumFixture:
    """Compose orchestration, admin access, a client for the record channel."""

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        server_port: int = SERVER_PORT,
        app_port: int = APP_PORT,
        project: str = PROJECT,
    ) -> None:
        self.state_dir = Path(
            state_dir or os.environ.get("MEDPLUM_STATE_DIR") or DEFAULT_STATE
        )
        self.server_port = server_port
        self.app_port = app_port
        self.project = project
        self.lock = json.loads((HERE / "environment.lock.json").read_text())
        for name, image in self.lock["services"].items():
            if "@sha256:" not in image:
                raise FixtureError(f"{name} image is not pinned by digest: {image}")

    # -- addresses -------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.server_port}"

    @property
    def fhir_url(self) -> str:
        return f"{self.base_url}/fhir/R4"

    @property
    def ui_url(self) -> str:
        return f"http://localhost:{self.app_port}/"

    # -- secrets and compose ---------------------------------------------------

    def _secrets(self) -> dict[str, str]:
        path = self.state_dir / "secrets.json"
        if path.exists():
            return json.loads(path.read_text())
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "db_password": secrets.token_urlsafe(24),
            "redis_password": secrets.token_urlsafe(24),
        }
        path.touch(mode=0o600)
        path.write_text(json.dumps(data, indent=2))
        return data

    def _env(self) -> dict[str, str]:
        s = self._secrets()
        images = self.lock["services"]
        return {
            "MEDPLUM_SERVER_IMAGE": images["server"],
            "MEDPLUM_APP_IMAGE": images["app"],
            "MEDPLUM_PG_IMAGE": images["postgres"],
            "MEDPLUM_REDIS_IMAGE": images["redis"],
            "MEDPLUM_DB_PASSWORD": s["db_password"],
            "MEDPLUM_REDIS_PASSWORD": s["redis_password"],
            "MEDPLUM_SERVER_PORT": str(self.server_port),
            "MEDPLUM_APP_PORT": str(self.app_port),
        }

    def compose(self, *args: str, input_text: Optional[str] = None) -> str:
        proc = subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(COMPOSE), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env={**os.environ, **self._env()},
            timeout=1800,
        )
        if proc.returncode != 0:
            raise FixtureError(
                f"compose {' '.join(args[:2])} failed: {proc.stderr[-1500:]}"
            )
        return proc.stdout

    def up(self, *, wait_s: float = 900.0) -> None:
        self.compose("up", "-d")
        self.wait_ready(wait_s=wait_s)

    def down(self, *, volumes: bool = True) -> None:
        self.compose("down", *(["-v"] if volumes else []))

    def wait_ready(self, *, wait_s: float = 900.0) -> None:
        deadline = time.monotonic() + wait_s
        last = ""
        while time.monotonic() < deadline:
            try:
                with (
                    urllib.request.urlopen(  # noqa: S310
                        f"{self.base_url}/healthcheck", timeout=5
                    ) as r1,
                    urllib.request.urlopen(self.ui_url, timeout=5) as r2,
                ):  # noqa: S310
                    if r1.status == 200 and r2.status == 200:
                        return
                    last = f"server {r1.status}, app {r2.status}"
            except (URLError, HTTPError, OSError) as exc:
                last = str(exc)
            time.sleep(5)
        raise FixtureError(f"Medplum not ready after {wait_s}s: {last}")

    # -- tokens ----------------------------------------------------------------

    def admin_token(self) -> str:
        """An access token for the seeded administrator (setup only)."""

        verifier = secrets.token_urlsafe(32)
        login = _http(
            "POST",
            f"{self.base_url}/auth/login",
            body={
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
                "scope": "openid",
                "codeChallengeMethod": "plain",
                "codeChallenge": verifier,
            },
        )
        if "code" not in login and login.get("memberships"):
            login = _http(
                "POST",
                f"{self.base_url}/auth/profile",
                body={
                    "login": login["login"],
                    "profile": login["memberships"][0]["id"],
                },
            )
        token = _http(
            "POST",
            f"{self.base_url}/oauth2/token",
            form={
                "grant_type": "authorization_code",
                "code": login["code"],
                "code_verifier": verifier,
            },
        )
        return token["access_token"]

    def client(self, name: str) -> dict[str, str]:
        """A client application (id and secret) for one role, made once."""

        path = self.state_dir / "clients.json"
        clients = json.loads(path.read_text()) if path.exists() else {}
        if name in clients:
            return clients[name]
        admin = self.admin_token()
        me = _http("GET", f"{self.base_url}/auth/me", token=admin)
        project_id = me["project"]["id"]
        made = _http(
            "POST",
            f"{self.base_url}/admin/projects/{project_id}/client",
            body={"name": f"openadapt-{name}", "description": "synthetic fixture"},
            token=admin,
        )
        clients[name] = {"id": made["id"], "secret": made["secret"]}
        path.touch(mode=0o600)
        path.write_text(json.dumps(clients, indent=2))
        return clients[name]

    def ensure_user(self, email: str, first: str, last: str) -> str:
        """A project member who signs in to the web app; returns its password."""

        path = self.state_dir / "secrets.json"
        data = json.loads(path.read_text())
        key = f"user:{email}"
        if key in data:
            return data[key]
        password = secrets.token_urlsafe(18)
        admin = self.admin_token()
        me = _http("GET", f"{self.base_url}/auth/me", token=admin)
        _http(
            "POST",
            f"{self.base_url}/admin/projects/{me['project']['id']}/invite",
            body={
                "resourceType": "Practitioner",
                "firstName": first,
                "lastName": last,
                "email": email,
                "password": password,
                "sendEmail": False,
            },
            token=admin,
        )
        data[key] = password
        path.write_text(json.dumps(data, indent=2))
        return password

    def client_token(self, name: str) -> str:
        c = self.client(name)
        token = _http(
            "POST",
            f"{self.base_url}/oauth2/token",
            form={
                "grant_type": "client_credentials",
                "client_id": c["id"],
                "client_secret": c["secret"],
            },
        )
        return token["access_token"]

    # -- FHIR (setup and the harness's own writes) ------------------------------

    def create_patient(self, resource: dict[str, Any], *, token: str) -> str:
        made = _http("POST", f"{self.fhir_url}/Patient", body=resource, token=token)
        return made["id"]

    def delete_patient(self, patient_id: str, *, token: str) -> None:
        _http("DELETE", f"{self.fhir_url}/Patient/{patient_id}", token=token)

    def patient_ids(self, *, token: str) -> list[str]:
        ids: list[str] = []
        url: Optional[str] = f"{self.fhir_url}/Patient?_count=1000&_elements=id"
        while url:
            bundle = _http("GET", url, token=token)
            ids += [e["resource"]["id"] for e in bundle.get("entry", [])]
            url = next(
                (
                    lnk["url"]
                    for lnk in bundle.get("link", [])
                    if lnk["relation"] == "next"
                ),
                None,
            )
        return ids

    # -- the oracle's channel --------------------------------------------------

    def psql(self, sql: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "medplum",
            "-d",
            "medplum",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )

    def patient_rows(self) -> list[dict[str, Any]]:
        """Every live Patient, as stored (the oracle's read)."""

        # One JSON object per line, so a "|" inside a value can't split a row.
        out = self.psql(
            "SELECT json_build_object('id', id, 'resource', content::json) "
            'FROM "Patient" WHERE deleted = false ORDER BY id;'
        )
        return [json.loads(line) for line in out.splitlines() if line.strip()]
