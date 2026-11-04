"""NetBox 4.1 (network inventory, Django, PostgreSQL): the shared fixture.

Runs as an external service from the community image (NetBox is
Apache-2.0); nothing is copied here. Synthetic tenants and addresses
(192.0.2.0/24 and 198.51.100.0/24 are documentation ranges).
"""

from __future__ import annotations

import csv
import io
import subprocess
from typing import Any

from benchmark.api_generalization.common import AppBase, podman

POD = "apigen-nb"
PG_C, REDIS_C, NB_C = "apigen-nb-pg", "apigen-nb-redis", "apigen-nb-app"
NB_IMAGE = "docker.io/netboxcommunity/netbox:v4.1.11"
PG_IMAGE = "docker.io/library/postgres:16-alpine"
REDIS_IMAGE = "ffb4a863fae9"  # docker.io/library/redis (alpine), already on this machine
DB_PASS = "synthetic-db-pass"  # throwaway local container
ADMIN_PASS = "synthetic-admin-pass"  # throwaway local container
UPSTREAM, PROXY = 9841, 9842
SEED = """
from extras.models import CustomField
from core.models import ObjectType
from ipam.models import IPAddress
cf, _ = CustomField.objects.get_or_create(name='owner_team', defaults=dict(type='text', label='Owner team', default='Network Ops'))
cf.default = 'Network Ops'; cf.save()
cf.object_types.set([ObjectType.objects.get_for_model(IPAddress)])
print('seeded')
"""


def nbsql(query: str) -> list[dict[str, Any]]:
    out = podman("exec", "-i", PG_C, "psql", "-U", "netbox", "-d", "netbox", "--csv", "-v", "ON_ERROR_STOP=1", "-f", "-",
                 input=query)
    return list(csv.DictReader(io.StringIO(out)))


class NetBoxBase(AppBase):
    upstream_port = UPSTREAM
    proxy_port = PROXY

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", POD]).returncode == 0:
            podman("pod", "start", POD)
            self.wait_http("/login/")
            return
        podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{UPSTREAM}:8080")
        podman("run", "-d", "--pod", POD, "--name", PG_C, "-e", "POSTGRES_USER=netbox", "-e", f"POSTGRES_PASSWORD={DB_PASS}",
               "-e", "POSTGRES_DB=netbox", PG_IMAGE)
        podman("run", "-d", "--pod", POD, "--name", REDIS_C, REDIS_IMAGE)
        podman("run", "-d", "--pod", POD, "--name", NB_C,
               "-e", "DB_HOST=127.0.0.1", "-e", "DB_NAME=netbox", "-e", "DB_USER=netbox", "-e", f"DB_PASSWORD={DB_PASS}",
               "-e", "REDIS_HOST=127.0.0.1", "-e", "REDIS_CACHE_HOST=127.0.0.1",
               "-e", "SECRET_KEY=synthetic-test-only-secret-key-0123456789abcdefghijklmnopqrstuvwxyz",
               "-e", "SKIP_SUPERUSER=false", "-e", "SUPERUSER_NAME=admin", "-e", "SUPERUSER_EMAIL=admin@example.invalid",
               "-e", f"SUPERUSER_PASSWORD={ADMIN_PASS}", "-e", "SUPERUSER_API_TOKEN=0123456789abcdef0123456789abcdef01234567",
               NB_IMAGE)
        self.wait_http("/login/", timeout_s=900)
        print(podman("exec", "-i", NB_C, "/opt/netbox/venv/bin/python", "/opt/netbox/netbox/manage.py", "shell",
                     input=SEED, timeout=300))

    def down(self) -> None:
        podman("pod", "stop", POD, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/login/")
        page.fill("#id_username", "admin")
        page.fill("#id_password", ADMIN_PASS)
        page.click("button[type=submit]")
        page.wait_for_load_state("load")

    def expire_session(self, page: Any) -> None:
        nbsql("DELETE FROM django_session")
