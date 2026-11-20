"""OpenEMR 8.0.0.3 (EHR, PHP, MariaDB): register a new patient.

Runs from the project's image, pinned by digest, as an external service;
nothing from OpenEMR (GPL-3.0) is copied here. Synthetic people only.
Lane 1 of the shared fixture lease.
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

APP_IMAGE = "docker.io/openemr/openemr@sha256:0aa4d3d52b22fa69986c087e7c99e9854d8dfd70440634eb7c8af0e08f19f3ab"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # a throwaway local container's, not a credential
ADMIN_PASS = "synthetic-admin-pass"  # a throwaway local container's, not a credential

COLS = ["pid AS id", "fname AS fname", "lname AS lname", "DOB AS DOB", "sex AS sex", "street AS street",
        "city AS city", "state AS state", "postal_code AS postal_code", "phone_home AS phone_home", "email AS email"]


class OpenEMR(App):
    name = "openemr"
    description = "OpenEMR, the clinic's electronic health record"
    record_noun = "patient"
    login_marker = "#clearPass"
    lane = 1
    columns = {"fname": "text", "lname": "text", "DOB": "date", "sex": "text", "street": "text", "city": "text",
               "state": "text", "postal_code": "text", "phone_home": "phone", "email": "email"}
    save = Save(r"^/interface/new/new_comprehensive_save\.php$")
    body_field = {"fname": "form_fname", "lname": "form_lname", "DOB": "form_DOB", "phone_home": "form_phone_home",
                  "email": "form_email", "street": "form_street", "city": "form_city", "postal_code": "form_postal_code"}
    home_path = None  # main.php needs the token sign-in gives it; without it OpenEMR ends the session
    save_ok_status = frozenset({200})  # success and failure both answer 200; only an error status shows a refusal

    @property
    def pod(self) -> str:
        return f"lbv2-oemr-{self.slot}"

    @property
    def db_c(self) -> str:
        return f"{self.pod}-db"

    def sql(self, q: str) -> list[dict[str, Any]]:
        return mysql_rows(self.db_c, DB_PASS, "openemr", q)

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", self.pod]).returncode == 0:
            podman("pod", "start", self.pod)
            self.wait_http("/interface/login/login.php", timeout_s=1800)
            return
        podman("pod", "create", "--name", self.pod, "-p", f"127.0.0.1:{self.upstream}:80")
        podman("run", "-d", "--pod", self.pod, "--name", self.db_c, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}", DB_IMAGE,
               "--character-set-server=utf8mb4")
        podman("run", "-d", "--pod", self.pod, "--name", f"{self.pod}-app", "-e", "MYSQL_HOST=127.0.0.1",
               "-e", f"MYSQL_ROOT_PASS={DB_PASS}", "-e", "MYSQL_USER=openemr", "-e", f"MYSQL_PASS={DB_PASS}",
               "-e", "OE_USER=admin", "-e", f"OE_PASS={ADMIN_PASS}", APP_IMAGE)
        self.wait_http("/interface/login/login.php", timeout_s=1800)

    def down(self) -> None:
        podman("pod", "rm", "-f", self.pod, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/interface/login/login.php?site=default")
        page.fill("#authUser", "admin")
        page.fill("#clearPass", ADMIN_PASS)
        page.click("#login-button")
        page.wait_for_url(lambda u: "login.php" not in u, timeout=60000)
        page.wait_for_load_state("networkidle")

    def signed_out(self, url: str) -> bool:
        return "login.php" in url

    def rows(self) -> list[dict[str, Any]]:
        return self.sql(json_select(COLS, "patient_data ORDER BY pid"))

    def _insert(self, p: dict[str, Any]) -> None:
        mysql_exec(self.db_c, DB_PASS, "openemr",
                   "INSERT INTO patient_data (uuid, pid, pubpid, fname, lname, DOB, sex, street, city, state, postal_code, "
                   "phone_home, email, date, regdate) SELECT UNHEX(REPLACE(UUID(), '-', '')), COALESCE(MAX(pid), 0) + 1, "
                   f"COALESCE(MAX(pid), 0) + 1, {sql_str(p['fname'])}, {sql_str(p['lname'])}, {sql_str(p['DOB'])}, "
                   f"{sql_str(p['sex'])}, {sql_str(p.get('street', ''))}, {sql_str(p.get('city', ''))}, "
                   f"{sql_str(p.get('state', ''))}, {sql_str(p.get('postal_code', ''))}, {sql_str(p.get('phone_home', ''))}, "
                   f"{sql_str(p.get('email', ''))}, NOW(), NOW() FROM patient_data; "
                   "UPDATE patient_data SET pubpid = pid WHERE pubpid = '';")

    def seed(self, people: list[dict[str, Any]]) -> None:
        have = {(r["fname"], r["lname"]) for r in self.rows()}
        for p in people:
            if (p["fname"], p["lname"]) not in have:
                self._insert(p)

    def reset(self, task: dict[str, Any], seed: list[dict[str, Any]]) -> None:
        keep = {(p["fname"], p["lname"]) for p in seed}
        for r in self.rows():
            if (r["fname"], r["lname"]) not in keep:
                mysql_exec(self.db_c, DB_PASS, "openemr", f"DELETE FROM patient_data WHERE pid = {int(r['id'])}")
        for p in seed:
            mysql_exec(self.db_c, DB_PASS, "openemr",
                       f"UPDATE patient_data SET phone_home = {sql_str(p.get('phone_home', ''))}, "
                       f"email = {sql_str(p.get('email', ''))}, street = {sql_str(p.get('street', ''))}, "
                       f"city = {sql_str(p.get('city', ''))}, DOB = {sql_str(p['DOB'])} "
                       f"WHERE fname = {sql_str(p['fname'])} AND lname = {sql_str(p['lname'])}")
        self.seed(seed)

    def side_write(self, target: dict[str, Any], change: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = {sql_str(v)}" for k, v in change.items())
        mysql_exec(self.db_c, DB_PASS, "openemr",
                   f"UPDATE patient_data SET {sets} WHERE fname = {sql_str(target['fname'])} AND lname = {sql_str(target['lname'])}")

    def expect(self, t: dict[str, Any]) -> dict[str, str]:
        return {c: t[c] for c in ("fname", "lname", "DOB", "sex", "street", "city", "state", "postal_code", "phone_home", "email")}

    def task_text(self, t: dict[str, Any]) -> str:
        return (f"Register a new patient: {t['fname']} {t['lname']}, date of birth {t['DOB']}, sex {t['sex']}, "
                f"address {t['street']}, {t['city']}, {t['state_name']} {t['postal_code']}, home phone {t['phone_home']}, "
                f"email {t['email']}. Fill in exactly these values (first and last name, date of birth, sex, street, "
                "city, state, postal code, home phone, email) and leave every other field as it is.")

    def scripted(self, page: Any, t: dict[str, Any]) -> str:
        """Model-free harness check only: the form filled by selectors."""

        page.goto(self.base + "/interface/new/new.php")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Contact", exact=True).click()
        for f in ("fname", "lname", "DOB", "street", "city", "postal_code", "phone_home", "email"):
            page.fill(f"#form_{f}", t[f])
        page.select_option("#form_sex", t["sex"])
        page.select_option("#form_state", t["state"])
        # The page's own Create button opens a duplicate-check dialog that needs
        # the app's tab frame; the harness check posts the same form directly.
        page.evaluate("document.getElementById('DEM').submit()")
        page.wait_for_timeout(4000)
        return f"RESULT: DONE {page.url}"
