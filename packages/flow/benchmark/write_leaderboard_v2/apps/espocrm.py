"""EspoCRM 8.4 (CRM, PHP, MariaDB): create an account.

The app is a single-page app; Save posts the record as JSON
(``POST /api/v1/Account``). Runs from the project's image as an external
service; nothing from EspoCRM (AGPL-3.0) is copied here. Synthetic
companies only.
"""

from __future__ import annotations

import secrets
import subprocess
from typing import Any

from benchmark.write_leaderboard_v2.apps.base import (
    App,
    json_select,
    mysql_exec,
    mysql_rows,
    podman,
    sql_str,
)
from benchmark.write_leaderboard_v2.proxy import Save

APP_IMAGE = "docker.io/espocrm/espocrm:8.4"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # a throwaway local container's, not a credential
ADMIN_PASS = "synthetic-admin-pass"  # a throwaway local container's, not a credential

COLS = ["id AS id", "name AS name", "website AS website", "type AS type", "industry AS industry",
        "description AS description", "deleted AS deleted"]


class EspoCRM(App):
    name = "espocrm"
    description = "EspoCRM, the company's CRM"
    record_noun = "account"
    login_marker = "#field-password"
    columns = {"name": "text", "website": "text", "type": "text", "industry": "text", "description": "text",
               "deleted": "text"}
    save = Save(r"^/api/v1/Account$")
    home_path = "/"
    save_ok_status = frozenset({200})

    @property
    def pod(self) -> str:
        return f"lbv2-espo-{self.slot}"

    @property
    def db_c(self) -> str:
        return f"{self.pod}-db"

    def sql(self, q: str) -> list[dict[str, Any]]:
        return mysql_rows(self.db_c, DB_PASS, "espocrm", q)

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", self.pod]).returncode == 0:
            podman("pod", "start", self.pod)
            self.wait_http("/")
            return
        podman("pod", "create", "--name", self.pod, "-p", f"127.0.0.1:{self.upstream}:80")
        podman("run", "-d", "--pod", self.pod, "--name", self.db_c, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}",
               "-e", "MARIADB_DATABASE=espocrm", DB_IMAGE)
        podman("run", "-d", "--pod", self.pod, "--name", f"{self.pod}-app", "-e", "ESPOCRM_DATABASE_HOST=127.0.0.1",
               "-e", "ESPOCRM_DATABASE_USER=root", "-e", f"ESPOCRM_DATABASE_PASSWORD={DB_PASS}",
               "-e", "ESPOCRM_ADMIN_USERNAME=admin", "-e", f"ESPOCRM_ADMIN_PASSWORD={ADMIN_PASS}",
               "-e", f"ESPOCRM_SITE_URL=http://127.0.0.1:{self.proxy_port}", APP_IMAGE)
        self.wait_http("/", timeout_s=900)

    def down(self) -> None:
        podman("pod", "rm", "-f", self.pod, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/")
        page.wait_for_selector("#field-userName", timeout=60000)
        page.fill("#field-userName", "admin")
        page.fill("#field-password", ADMIN_PASS)
        page.click("#btn-login")
        page.wait_for_selector("#navbar", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")

    def is_present(self, row: dict[str, Any]) -> bool:
        return str(row.get("deleted", "0")) in ("0", "", "False", "false")

    def rows(self) -> list[dict[str, Any]]:
        return self.sql(json_select(COLS, "account ORDER BY created_at, id"))

    def _insert(self, a: dict[str, Any]) -> None:
        rid = secrets.token_hex(8)[:17]
        mysql_exec(self.db_c, DB_PASS, "espocrm",
                   "INSERT INTO account (id, name, website, type, industry, description, deleted, created_at, modified_at, "
                   "created_by_id, assigned_user_id) VALUES "
                   f"({sql_str(rid)}, {sql_str(a['name'])}, {sql_str(a.get('website', ''))}, {sql_str(a.get('type', ''))}, "
                   f"{sql_str(a.get('industry', ''))}, {sql_str(a.get('description', ''))}, 0, NOW(), NOW(), "
                   "(SELECT id FROM user WHERE user_name = 'admin'), (SELECT id FROM user WHERE user_name = 'admin'))")

    def seed(self, people: list[dict[str, Any]]) -> None:
        have = {r["name"] for r in self.rows()}
        for a in people:
            if a["name"] not in have:
                self._insert(a)

    def reset(self, task: dict[str, Any], seed: list[dict[str, Any]]) -> None:
        keep = {a["name"] for a in seed}
        for r in self.rows():
            if r["name"] not in keep:
                mysql_exec(self.db_c, DB_PASS, "espocrm", f"DELETE FROM account WHERE id = {sql_str(r['id'])}")
        for a in seed:
            mysql_exec(self.db_c, DB_PASS, "espocrm",
                       f"UPDATE account SET website = {sql_str(a.get('website', ''))}, type = {sql_str(a.get('type', ''))}, "
                       f"industry = {sql_str(a.get('industry', ''))}, description = {sql_str(a.get('description', ''))}, "
                       f"deleted = 0 WHERE name = {sql_str(a['name'])}")
        self.seed(seed)

    def side_write(self, target: dict[str, Any], change: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = {sql_str(v)}" for k, v in change.items())
        mysql_exec(self.db_c, DB_PASS, "espocrm", f"UPDATE account SET {sets} WHERE name = {sql_str(target['name'])}")

    def expect(self, t: dict[str, Any]) -> dict[str, str]:
        return {"name": t["name"], "website": t["website"], "type": t["type"], "industry": t["industry"],
                "description": t["description"], "deleted": "0"}

    def task_text(self, t: dict[str, Any]) -> str:
        return (f"Create a new account named {t['name']}: website {t['website']}, type {t['type']}, "
                f"industry {t['industry']}, description \"{t['description']}\". "
                "Fill in exactly these values (name, website, type, industry, description) "
                "and leave every other field as it is.")

    def scripted(self, page: Any, t: dict[str, Any]) -> str:
        """Model-free harness check only: the form filled by selectors."""

        page.goto(self.base + "/#Account/create")
        page.wait_for_selector('.record [data-name="name"] input', timeout=30000)
        page.fill('.record [data-name="name"] input', t["name"])
        page.fill('.record [data-name="website"] input', t["website"])
        for f in ("type", "industry"):
            page.evaluate("([f, v]) => document.querySelector(`.record [data-name='${f}'] select`).selectize.setValue(v)",
                          [f, t[f]])
        page.fill('.record [data-name="description"] textarea', t["description"])
        page.click('.record button[data-action="save"]')
        page.wait_for_timeout(3000)
        return f"RESULT: DONE {page.url}"
