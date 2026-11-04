"""EspoCRM 8.4 (CRM, PHP, MariaDB): a sales rep creates an account.

The desk is a single-page app: Save sends the record as JSON over XHR
(``POST /api/v1/Account``), with the session's own auth header. Runs as an
external service from the project's image; nothing from EspoCRM (AGPL-3.0)
is copied here. Synthetic companies only.
"""

from __future__ import annotations

import subprocess
from typing import Any

from benchmark.api_generalization.common import (
    AppBase,
    ExecSqlVerifier,
    dump_database,
    mysql_rows,
    podman,
    rec_click,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

POD = "apigen-espo"
DB_C, APP_C = "apigen-espo-db", "apigen-espo-app"
APP_IMAGE = "docker.io/espocrm/espocrm:8.4"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # throwaway local container
ADMIN_PASS = "synthetic-admin-pass"  # throwaway local container
UPSTREAM, PROXY = 9671, 9672

QUERY = "SELECT id, name, website, description FROM account WHERE deleted = 0"
JUDGE = (
    "SELECT id, name, website, description, type, industry, assigned_user_id, deleted "
    "FROM account ORDER BY created_at, id"
)


def esql(query: str) -> list[dict[str, Any]]:
    return mysql_rows(DB_C, "root", DB_PASS, "espocrm", query)


class App(AppBase):
    def dump(self) -> dict[str, list[dict[str, Any]]]:
        return dump_database('mysql', esql)

    name = "espocrm_account"
    workflow = "new-account"
    upstream_port = UPSTREAM
    proxy_port = PROXY
    form_path = "/#Account/create"
    save_path_regex = r"^/api/v1/Account$"
    unique_param = "name"
    normal_values = [
        dict(name="Harbor Office Supply", website="harbor.example.invalid", description="Paper and toner, weekly delivery."),
        dict(name="Northwind Clinics", website="northwind.example.invalid", description="Three sites, one buyer."),
        dict(name="Bluebird Freight", website="bluebird.example.invalid", description="Pallet shipping, net 30."),
    ]
    blank_values = dict(name="Kestrel Labs", website="", description="")
    dup_values = dict(name="Quinn", website="quinn.example.invalid", description="Quinn")
    invalid_values = dict(name="", website="bad.example.invalid", description="no name")
    invalid_note = "no name (required; the page refuses it)"

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", POD]).returncode == 0:
            podman("pod", "start", POD)
            self.wait_http("/")
            return
        podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{UPSTREAM}:80")
        podman("run", "-d", "--pod", POD, "--name", DB_C, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}",
               "-e", "MARIADB_DATABASE=espocrm", DB_IMAGE)
        podman("run", "-d", "--pod", POD, "--name", APP_C, "-e", "ESPOCRM_DATABASE_HOST=127.0.0.1",
               "-e", "ESPOCRM_DATABASE_USER=root", "-e", f"ESPOCRM_DATABASE_PASSWORD={DB_PASS}",
               "-e", "ESPOCRM_ADMIN_USERNAME=admin", "-e", f"ESPOCRM_ADMIN_PASSWORD={ADMIN_PASS}",
               "-e", f"ESPOCRM_SITE_URL=http://127.0.0.1:{PROXY}", APP_IMAGE)
        self.wait_http("/", timeout_s=900)

    def down(self) -> None:
        podman("pod", "stop", POD, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/")
        page.fill("#field-userName", "admin")
        page.fill("#field-password", ADMIN_PASS)
        page.click("#btn-login")
        page.wait_for_selector("#navbar", state="attached", timeout=30000)
        page.wait_for_load_state("networkidle")

    def open_form(self, page: Any) -> None:
        page.goto(self.base + self.form_path)
        page.wait_for_selector('.record [data-name="name"] input', timeout=30000)
        page.wait_for_load_state("networkidle")

    def expire_session(self, page: Any) -> None:
        esql("UPDATE auth_token SET is_active = 0")

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, '.record [data-name="name"] input', v["name"], "name")
        rec_text(rec, page, '.record [data-name="website"] input', v["website"], "website")
        rec_text(rec, page, '.record [data-name="description"] textarea', v["description"], "description")
        rec_click(rec, page, '.record button[data-action="save"]')

    def rows(self) -> list[dict[str, Any]]:
        return esql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(esql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"name": v["name"], "website": v["website"], "description": v["description"]}

    def effects(self) -> list[Effect]:
        match = {"name": ValueExpr(param="name")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one account with this name", timeout_s=8.0)]
        for f in ("website", "description"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"account {f} read-back", timeout_s=8.0))
        return out
