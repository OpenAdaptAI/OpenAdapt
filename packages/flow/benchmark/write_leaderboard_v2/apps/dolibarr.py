"""Dolibarr 20 (ERP/CRM, PHP, MariaDB): add a contact.

Runs from the project's own image as an external service; nothing from
Dolibarr (GPL-3.0) is copied here. Synthetic people only.
"""

from __future__ import annotations

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

APP_IMAGE = "docker.io/dolibarr/dolibarr:20.0.2"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # a throwaway local container's, not a credential
ADMIN_PASS = "synthetic-admin-pass"  # a throwaway local container's, not a credential

COLS = ["p.rowid AS id", "p.lastname AS lastname", "p.firstname AS firstname", "p.poste AS poste",
        "p.email AS email", "p.phone AS phone", "p.statut AS statut", "p.fk_soc AS fk_soc"]


class Dolibarr(App):
    name = "dolibarr"
    description = "Dolibarr, the company's ERP and CRM"
    record_noun = "contact"
    login_marker = "#password"
    columns = {"lastname": "text", "firstname": "text", "poste": "text", "email": "email", "phone": "phone",
               "statut": "text", "fk_soc": "text"}
    save = Save(r"^/contact/card\.php$", body_require=r"(^|&)action=add(&|$)")
    body_field = {"phone": "phone_pro", "poste": "poste", "email": "email", "firstname": "firstname", "lastname": "lastname"}
    home_path = "/index.php"
    save_ok_status = frozenset({302})

    @property
    def pod(self) -> str:
        return f"lbv2-doli-{self.slot}"

    @property
    def db_c(self) -> str:
        return f"{self.pod}-db"

    def sql(self, q: str) -> list[dict[str, Any]]:
        return mysql_rows(self.db_c, DB_PASS, "dolibarr", q)

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", self.pod]).returncode == 0:
            podman("pod", "start", self.pod)
            self.wait_http("/")
            return
        podman("pod", "create", "--name", self.pod, "-p", f"127.0.0.1:{self.upstream}:80")
        podman("run", "-d", "--pod", self.pod, "--name", self.db_c, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}",
               "-e", "MARIADB_DATABASE=dolibarr", DB_IMAGE)
        podman("run", "-d", "--pod", self.pod, "--name", f"{self.pod}-app", "-e", "DOLI_DB_HOST=127.0.0.1",
               "-e", "DOLI_DB_NAME=dolibarr", "-e", "DOLI_DB_USER=root", "-e", f"DOLI_DB_PASSWORD={DB_PASS}",
               "-e", "DOLI_ADMIN_LOGIN=admin", "-e", f"DOLI_ADMIN_PASSWORD={ADMIN_PASS}", "-e", "DOLI_INSTALL_AUTO=1",
               "-e", "DOLI_INIT_DEMO=0", "-e", f"DOLI_URL_ROOT=http://127.0.0.1:{self.proxy_port}",
               "-e", "DOLI_COMPANY_NAME=Synthetic Supplies", "-e", "DOLI_ENABLE_MODULES=Societe", APP_IMAGE)
        self.wait_http("/", timeout_s=900)

    def down(self) -> None:
        podman("pod", "rm", "-f", self.pod, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/index.php")
        page.fill("#username", "admin")
        page.fill("#password", ADMIN_PASS)
        page.click("input[type=submit]")
        page.wait_for_load_state("load")
        if page.locator("#username").count():
            raise RuntimeError("the harness couldn't sign in to Dolibarr")

    def signed_out(self, url: str) -> bool:
        return False  # Dolibarr shows its login form on the same URL; checked by page content in run.py

    def rows(self) -> list[dict[str, Any]]:
        return self.sql(json_select(COLS, "llx_socpeople p ORDER BY p.rowid"))

    def _insert(self, p: dict[str, Any]) -> None:
        mysql_exec(self.db_c, DB_PASS, "dolibarr",
                   "INSERT INTO llx_socpeople (entity, datec, lastname, firstname, poste, email, phone, statut, priv, fk_user_creat) "
                   f"VALUES (1, NOW(), {sql_str(p['lastname'])}, {sql_str(p.get('firstname', ''))}, {sql_str(p.get('poste', ''))}, "
                   f"{sql_str(p.get('email', ''))}, {sql_str(p.get('phone', ''))}, 1, 0, 1)")

    def seed(self, people: list[dict[str, Any]]) -> None:
        have = {(r["lastname"], r["firstname"]) for r in self.rows()}
        for p in people:
            if (p["lastname"], p.get("firstname", "")) not in have:
                self._insert(p)

    def reset(self, task: dict[str, Any], seed: list[dict[str, Any]]) -> None:
        keep = {(p["lastname"], p.get("firstname", "")) for p in seed}
        for r in self.rows():
            if (r["lastname"], r["firstname"]) not in keep:
                mysql_exec(self.db_c, DB_PASS, "dolibarr", f"DELETE FROM llx_socpeople WHERE rowid = {int(r['id'])}")
        for p in seed:
            mysql_exec(self.db_c, DB_PASS, "dolibarr",
                       f"UPDATE llx_socpeople SET poste = {sql_str(p.get('poste', ''))}, email = {sql_str(p.get('email', ''))}, "
                       f"phone = {sql_str(p.get('phone', ''))}, statut = 1 WHERE lastname = {sql_str(p['lastname'])} "
                       f"AND firstname = {sql_str(p.get('firstname', ''))}")
        self.seed(seed)

    def side_write(self, target: dict[str, Any], change: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = {sql_str(v)}" for k, v in change.items())
        mysql_exec(self.db_c, DB_PASS, "dolibarr",
                   f"UPDATE llx_socpeople SET {sets} WHERE lastname = {sql_str(target['lastname'])} "
                   f"AND firstname = {sql_str(target.get('firstname', ''))}")

    def expect(self, task: dict[str, Any]) -> dict[str, str]:
        return {"lastname": task["lastname"], "firstname": task["firstname"], "poste": task["poste"],
                "email": task["email"], "phone": task["phone"]}

    def task_text(self, t: dict[str, Any]) -> str:
        return (f"Add a new contact (not linked to any third party): {t['firstname']} {t['lastname']}, "
                f"job position {t['poste']}, professional phone {t['phone']}, email {t['email']}. "
                "Fill in exactly these values (first name, last name, job position, professional phone, email) "
                "and leave every other field as it is.")

    def scripted(self, page: Any, t: dict[str, Any]) -> str:
        """Model-free harness check only: the form filled by selectors."""

        page.goto(self.base + "/contact/card.php?action=create")
        page.fill("#lastname", t["lastname"])
        page.fill("#firstname", t["firstname"])
        page.fill("#title", t["poste"])
        page.fill("#phone_pro", t["phone"])
        page.fill("#email", t["email"])
        page.click("input[name=add]")
        page.wait_for_load_state("load")
        return f"RESULT: DONE {page.url}"
