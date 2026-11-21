"""The real systems for the API-verification bench: Medplum (FHIR) and Gitea.

Both run in Docker on loopback with synthetic data. Each lab gives the bench
three identities:

* ``writer``: the credentials the verifier holds (the actor in the log);
* ``bystander``: another caller, whose changes are outside the scope;
* an oracle that reads the store through a channel the verifier never uses
  (Medplum's Postgres; a Gitea site administrator's token).

Ports 9690-9699 (the zen fixture lane's free block). Secrets are generated
into the state folder (mode 0600), never into the repository.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fhir_medplum"))

STATE = Path(os.environ.get("API_SCOPED_STATE", Path.home() / "oa-api-scoped"))
MEDPLUM_PORT, MEDPLUM_APP_PORT, GITEA_PORT = 9693, 9694, 9692


def http(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback lab
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, raw.decode(errors="replace")[:300]


# -- Medplum ---------------------------------------------------------------------


class MedplumLab:
    name = "medplum"

    def __init__(self) -> None:
        from fixture import MedplumFixture

        self.fx = MedplumFixture(
            state_dir=STATE / "medplum",
            server_port=MEDPLUM_PORT,
            app_port=MEDPLUM_APP_PORT,
            project="oa-api-scoped-medplum",
        )
        self.base = self.fx.base_url

    def up(self) -> None:
        self.fx.up()

    def down(self) -> None:
        self.fx.down(volumes=True)

    def token(self, role: str) -> str:
        return self.fx.client_token(f"api-{role}")

    def actor(self, role: str) -> str:
        """The writer's reference in AuditEvent.agent.who."""
        return f"ClientApplication/{self.fx.client(f'api-{role}')['id']}"

    def env(self) -> dict[str, str]:
        return {
            "MEDPLUM_URL": self.base,
            "MEDPLUM_TOKEN": self.token("writer"),
            "MEDPLUM_ACTOR": self.actor("writer"),
        }

    def oracle_patient(self, rid: str) -> Optional[dict[str, Any]]:
        out = self.fx.psql(
            "SELECT content FROM \"Patient\" WHERE deleted = false AND id = '%s';" % rid.replace("'", "")
        ).strip()
        return json.loads(out) if out else None

    def oracle_count(self, family: str) -> int:
        out = self.fx.psql(
            "SELECT count(*) FROM \"Patient\" WHERE deleted = false AND content::jsonb #>> '{name,0,family}' = '%s';"
            % family.replace("'", "")
        )
        return int(out.strip() or 0)


# -- Gitea ------------------------------------------------------------------------


class GiteaLab:
    name = "gitea"
    project = "oa-api-scoped-gitea"
    users = ("oa-admin", "oa-writer", "oa-bystander")

    def __init__(self) -> None:
        self.base = f"http://127.0.0.1:{GITEA_PORT}"
        self.dir = STATE / "gitea"

    def compose(self, *args: str) -> str:
        proc = subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(HERE / "gitea.compose.yml"), *args],
            capture_output=True,
            text=True,
            env={**os.environ, "GITEA_PORT": str(GITEA_PORT)},
            timeout=900,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"compose {' '.join(args[:2])} failed: {proc.stderr[-800:]}")
        return proc.stdout

    def _secrets(self) -> dict[str, Any]:
        path = self.dir / "secrets.json"
        if path.exists():
            return json.loads(path.read_text())
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {u: secrets.token_urlsafe(18) for u in self.users}
        path.touch(mode=0o600)
        path.write_text(json.dumps(data))
        return data

    def _save(self, data: dict[str, Any]) -> None:
        (self.dir / "secrets.json").write_text(json.dumps(data))

    def up(self) -> None:
        self.compose("up", "-d", "--wait")
        pw = self._secrets()
        for user in self.users:
            got = subprocess.run(
                ["docker", "compose", "-p", self.project, "-f", str(HERE / "gitea.compose.yml"),
                 "exec", "-T", "-u", "git", "gitea", "gitea", "admin", "user", "create",
                 "--username", user, "--password", pw[user], "--email", f"{user}@example.invalid",
                 "--must-change-password=false", *(["--admin"] if user == "oa-admin" else [])],
                capture_output=True, text=True, env={**os.environ, "GITEA_PORT": str(GITEA_PORT)},
            )
            if got.returncode != 0 and "already exists" not in (got.stdout + got.stderr):
                raise RuntimeError(f"user create failed: {got.stderr[-300:]}")
        for repo in ("lab", "other"):
            http("POST", f"{self.base}/api/v1/user/repos", body={"name": repo, "auto_init": True},
                 headers=self._basic("oa-writer"))
            http("PUT", f"{self.base}/api/v1/repos/oa-writer/{repo}/collaborators/oa-bystander",
                 body={"permission": "write"}, headers=self._basic("oa-writer"))

    def down(self) -> None:
        self.compose("down", "-v")

    def _basic(self, user: str) -> dict[str, str]:
        pw = self._secrets()[user]
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def token(self, role: str) -> str:
        user = {"writer": "oa-writer", "bystander": "oa-bystander", "oracle": "oa-admin"}[role]
        data = self._secrets()
        key = f"token:{user}"
        if key not in data:
            code, got = http(
                "POST", f"{self.base}/api/v1/users/{user}/tokens",
                body={"name": f"bench-{secrets.token_hex(3)}", "scopes": ["write:repository", "write:issue", "read:user"]},
                headers=self._basic(user),
            )
            if code != 201:
                raise RuntimeError(f"token create failed: {code} {got}")
            data[key] = got["sha1"]
            self._save(data)
        return str(data[key])

    def env(self) -> dict[str, str]:
        return {"GITEA_URL": self.base, "GITEA_TOKEN": self.token("writer")}

    def oracle_issue(self, repo: str, number: str) -> Optional[dict[str, Any]]:
        code, got = http("GET", f"{self.base}/api/v1/repos/oa-writer/{repo}/issues/{number}",
                         headers={"Authorization": f"token {self.token('oracle')}"})
        return got if code == 200 else None


LABS = {"medplum": MedplumLab, "gitea": GiteaLab}


def wait_port(url: str, seconds: float = 300) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5)  # noqa: S310
            return
        except Exception:  # noqa: BLE001
            time.sleep(3)
    raise RuntimeError(f"{url} not ready")


if __name__ == "__main__":
    what, name = sys.argv[1], sys.argv[2]
    lab = LABS[name]()
    if what == "up":
        lab.up()
        print(f"{name} up")
    elif what == "down":
        lab.down()
        print(f"{name} down")
