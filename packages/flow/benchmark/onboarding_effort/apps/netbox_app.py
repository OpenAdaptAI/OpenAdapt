"""The onboarding benchmark's NetBox 4.1 instance (Apache-2.0).

Community image ``netboxcommunity/netbox:v4.1.11`` with PostgreSQL and Redis
in one pod, ``onboard-nb``: UI ``127.0.0.1:9486``, PostgreSQL ``127.0.0.1:9489``
(the engineer's read-only verifier connects there as ``nb_reader``). Reset
restores a ``pg_dump`` baseline. Synthetic inventory only (documentation
address ranges); throwaway local passwords.
"""

from __future__ import annotations

import csv
import io
import subprocess
import time
from typing import Any
from urllib.request import urlopen

POD = "onboard-nb"
PG_C, REDIS_C, NB_C = "onboard-nb-pg", "onboard-nb-redis", "onboard-nb-app"
NB_IMAGE = "docker.io/netboxcommunity/netbox:v4.1.11"
PG_IMAGE = "docker.io/library/postgres:16-alpine"
REDIS_IMAGE = "ffb4a863fae9"  # docker.io/library/redis (alpine), already local
PORT, PG_PORT = 9486, 9489
BASE = f"http://127.0.0.1:{PORT}"
DB_PASS = "synthetic-db-pass"
READER_PASS = "synthetic-reader-pass"
USERS = {"admin": "synthetic-admin-pass", "operator": "synthetic-operator-pass"}
BASELINE = "/var/lib/postgresql/baseline.dump"


def podman(*args: str, input: str | None = None, timeout: float = 600, check: bool = True) -> str:
    proc = subprocess.run(["podman", *args], input=input, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args[:3])} failed: {proc.stderr[-500:]}")
    return proc.stdout


def nbsql(query: str) -> list[dict[str, Any]]:
    out = podman("exec", "-i", PG_C, "psql", "-U", "netbox", "-d", "netbox", "--csv",
                 "-v", "ON_ERROR_STOP=1", "-f", "-", input=query)
    return list(csv.DictReader(io.StringIO(out)))


def shell(code: str) -> str:
    return podman("exec", "-i", NB_C, "/opt/netbox/venv/bin/python",
                  "/opt/netbox/netbox/manage.py", "shell", input=code, timeout=600)


def wait_http(path: str = "/login/", timeout_s: float = 900) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(BASE + path, timeout=5) as r:
                if r.status < 500:
                    return
        except Exception:  # noqa: BLE001 - starting up
            time.sleep(2)
    raise RuntimeError("NetBox did not answer")


SEED = """
from dcim.models import Site, DeviceRole, Manufacturer, DeviceType
from ipam.models import VLANGroup
from extras.models import Tag
from users.models import User
from django.contrib.contenttypes.models import ContentType
for s in ("Lisbon Lab", "Oslo Edge", "Quito Branch"):
    Site.objects.get_or_create(name=s, slug=s.lower().replace(" ", "-"))
for r, c in (("Core Switch", "2196f3"), ("Access Point", "4caf50"), ("Edge Router", "ff9800")):
    DeviceRole.objects.get_or_create(name=r, slug=r.lower().replace(" ", "-"), defaults={"color": c})
m, _ = Manufacturer.objects.get_or_create(name="Synthetica", slug="synthetica")
for t in ("SX-24", "SX-48", "AP-7", "ER-2"):
    DeviceType.objects.get_or_create(manufacturer=m, model=t, slug=t.lower())
for g in ("Campus", "Datacenter"):
    VLANGroup.objects.get_or_create(name=g, slug=g.lower())
Tag.objects.get_or_create(name="onb-audit", slug="onb-audit")
if not User.objects.filter(username="operator").exists():
    User.objects.create_user("operator", password="synthetic-operator-pass")
print("seeded")
"""


def exists() -> bool:
    return subprocess.run(["podman", "pod", "exists", POD]).returncode == 0


def up() -> None:
    if exists():
        podman("pod", "start", POD)
        wait_http()
        return
    podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{PORT}:8080",
           "-p", f"127.0.0.1:{PG_PORT}:5432")
    podman("run", "-d", "--pod", POD, "--name", PG_C, "-e", "POSTGRES_USER=netbox",
           "-e", f"POSTGRES_PASSWORD={DB_PASS}", "-e", "POSTGRES_DB=netbox", PG_IMAGE)
    podman("run", "-d", "--pod", POD, "--name", REDIS_C, REDIS_IMAGE)
    podman("run", "-d", "--pod", POD, "--name", NB_C,
           "-e", "DB_HOST=127.0.0.1", "-e", "DB_NAME=netbox", "-e", "DB_USER=netbox",
           "-e", f"DB_PASSWORD={DB_PASS}", "-e", "REDIS_HOST=127.0.0.1",
           "-e", "REDIS_CACHE_HOST=127.0.0.1",
           "-e", "SECRET_KEY=synthetic-test-only-secret-key-0123456789abcdefghijklmnopqrstuvwxyz",
           "-e", "SKIP_SUPERUSER=false", "-e", "SUPERUSER_NAME=admin",
           "-e", "SUPERUSER_EMAIL=admin@example.invalid",
           "-e", f"SUPERUSER_PASSWORD={USERS['admin']}", NB_IMAGE)
    wait_http()
    print(shell(SEED))
    nbsql(f"DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nb_reader') THEN "
          f"CREATE ROLE nb_reader LOGIN PASSWORD '{READER_PASS}'; END IF; END$$;"
          "GRANT SELECT ON ALL TABLES IN SCHEMA public TO nb_reader;")
    snapshot()


def stop() -> None:
    podman("pod", "stop", POD, check=False)


def snapshot() -> None:
    podman("exec", PG_C, "sh", "-c", f"pg_dump -U netbox -Fc netbox > {BASELINE}")


def reset() -> None:
    # Drop sessions and connections, then restore the baseline in place.
    podman("exec", PG_C, "sh", "-c",
           f"pg_restore -U netbox -d netbox --clean --if-exists --single-transaction {BASELINE}")
    nbsql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO nb_reader;")
    podman("exec", REDIS_C, "redis-cli", "FLUSHALL", check=False)


def login(page: Any, user: str = "admin") -> None:
    page.goto(BASE + "/login/")
    if page.locator("#id_username").count() == 0:
        return  # this browser context is already signed in
    page.fill("#id_username", user)
    page.fill("#id_password", USERS[user])
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
