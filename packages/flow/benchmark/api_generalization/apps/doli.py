"""Dolibarr 20 (ERP/CRM, PHP, MariaDB): the shared fixture.

Runs as an external service from the project's image; nothing from
Dolibarr (GPL-3.0) is copied here. Synthetic companies and contacts.
"""

from __future__ import annotations

import subprocess
from typing import Any

from benchmark.api_generalization.common import AppBase, mysql_rows, podman

POD = "apigen-doli"
DB_C, APP_C = "apigen-doli-db", "apigen-doli-app"
APP_IMAGE = "docker.io/dolibarr/dolibarr:20.0.2"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # throwaway local container
ADMIN_PASS = "synthetic-admin-pass"  # throwaway local container
UPSTREAM, PROXY = 9851, 9852


def dsql(query: str) -> list[dict[str, Any]]:
    return mysql_rows(DB_C, "root", DB_PASS, "dolibarr", query)


class DolibarrBase(AppBase):
    upstream_port = UPSTREAM
    proxy_port = PROXY

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", POD]).returncode == 0:
            podman("pod", "start", POD)
            self.wait_http("/")
            return
        podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{UPSTREAM}:80")
        podman("run", "-d", "--pod", POD, "--name", DB_C, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}",
               "-e", "MARIADB_DATABASE=dolibarr", DB_IMAGE)
        podman("run", "-d", "--pod", POD, "--name", APP_C, "-e", "DOLI_DB_HOST=127.0.0.1", "-e", "DOLI_DB_NAME=dolibarr",
               "-e", "DOLI_DB_USER=root", "-e", f"DOLI_DB_PASSWORD={DB_PASS}", "-e", "DOLI_ADMIN_LOGIN=admin",
               "-e", f"DOLI_ADMIN_PASSWORD={ADMIN_PASS}", "-e", "DOLI_INSTALL_AUTO=1", "-e", "DOLI_INIT_DEMO=0",
               "-e", f"DOLI_URL_ROOT=http://127.0.0.1:{PROXY}", "-e", "DOLI_COMPANY_NAME=Synthetic Supplies",
               "-e", "DOLI_ENABLE_MODULES=Societe", APP_IMAGE)
        self.wait_http("/", timeout_s=900)

    def down(self) -> None:
        podman("pod", "stop", POD, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/index.php")
        page.fill("#username", "admin")
        page.fill("#password", ADMIN_PASS)
        page.click("input[type=submit]")
        page.wait_for_load_state("load")

    def expire_session(self, page: Any) -> None:
        # PHP keeps sessions in files; removing them ends every session.
        podman("exec", APP_C, "sh", "-c", "rm -f /var/lib/php/sessions/* /tmp/sess_* 2>/dev/null; true")
