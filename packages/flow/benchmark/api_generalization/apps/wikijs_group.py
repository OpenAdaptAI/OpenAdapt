"""Wiki.js 2.5 (wiki, Node, Postgres): an administrator creates a user group.

The admin area is a single-page app: Create sends a GraphQL mutation
(``groups.create``) as a batch of one operation to ``/graphql``, signed with
the session's bearer token. Runs as an external service from the project's
image; nothing from Wiki.js (AGPL-3.0) is copied here. Synthetic names only.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import time
import urllib.request
from typing import Any

from benchmark.api_generalization.common import (
    AppBase,
    Case,
    ExecSqlVerifier,
    podman,
    rec_click,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

POD = "apigen-wiki"
DB_C, APP_C = "apigen-wiki-db", "apigen-wiki-app"
APP_IMAGE = "docker.io/requarks/wiki:2.5"
DB_IMAGE = "docker.io/library/postgres:16"
DB_PASS = "synthetic-db-pass"  # throwaway local container
ADMIN_EMAIL = "admin@example.invalid"
ADMIN_PASS = "synthetic-admin-pass"  # throwaway local container
UPSTREAM, PROXY = 9677, 9678

QUERY = 'SELECT id, name FROM groups WHERE "isSystem" = false'
JUDGE = 'SELECT id, name, "isSystem" AS is_system, "redirectOnLogin" AS redirect FROM groups ORDER BY id'


def wsql(query: str) -> list[dict[str, Any]]:
    out = podman("exec", "-i", DB_C, "psql", "-U", "wiki", "-d", "wiki", "--csv", "-v", "ON_ERROR_STOP=1",
                 "-f", "-", input=query)
    return list(csv.DictReader(io.StringIO(out)))


class App(AppBase):
    name = "wikijs_group"
    workflow = "new-group"
    upstream_port = UPSTREAM
    proxy_port = PROXY
    form_path = "/a/groups"
    save_path_regex = r"^/graphql$"
    save_body_require = "groups"
    unique_param = "name"
    #: The save checks the right record on screen; the person admitting the
    #: API step names the parameter that identifies the record.
    api_identity = {"subject_name": "name"}
    normal_values = [dict(name="Night Shift Editors"), dict(name="Clinic Reviewers"), dict(name="Field Technicians")]
    blank_values = dict(name="Weekend Staff")  # one required field: nothing can be left blank
    dup_values = dict(name="Quinn Quinn")
    invalid_values = dict(name="")
    invalid_note = "no name (the page refuses it)"

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", POD]).returncode == 0:
            podman("pod", "start", POD)
            self.wait_http("/login")
            return
        podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{UPSTREAM}:3000")
        podman("run", "-d", "--pod", POD, "--name", DB_C, "-e", "POSTGRES_USER=wiki",
               "-e", f"POSTGRES_PASSWORD={DB_PASS}", "-e", "POSTGRES_DB=wiki", DB_IMAGE)
        time.sleep(5)
        podman("run", "-d", "--pod", POD, "--name", APP_C, "-e", "DB_TYPE=postgres", "-e", "DB_HOST=127.0.0.1",
               "-e", "DB_PORT=5432", "-e", "DB_USER=wiki", "-e", f"DB_PASS={DB_PASS}", "-e", "DB_NAME=wiki",
               APP_IMAGE)
        self.wait_http("/", timeout_s=600)
        body = json.dumps({"adminEmail": ADMIN_EMAIL, "adminPassword": ADMIN_PASS,
                           "adminPasswordConfirm": ADMIN_PASS, "siteUrl": self.base, "telemetry": False})
        req = urllib.request.Request(f"http://127.0.0.1:{UPSTREAM}/finalize", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=120).read()
        time.sleep(10)  # the server restarts itself after setup
        self.wait_http("/login", timeout_s=300)

    def down(self) -> None:
        podman("pod", "stop", POD, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/login")
        page.wait_for_load_state("networkidle")
        page.fill("input[type=email]", ADMIN_EMAIL)
        page.fill("input[type=password]", ADMIN_PASS)
        page.keyboard.press("Enter")
        page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
        page.wait_for_load_state("networkidle")

    def open_form(self, page: Any) -> None:
        page.goto(self.base + self.form_path)
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="New Group").wait_for(timeout=30000)

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_click(rec, page, "button:has-text('New Group')")
        page.wait_for_timeout(600)
        rec_text(rec, page, ".v-dialog input", v["name"], "name")
        rec_click(rec, page, ".v-dialog button:has-text('Create')")

    def cases(self) -> list[Case]:
        # The session is a signed token the server keeps no record of, so
        # there is no server-side session to end (S1 doesn't apply).
        return [c for c in super().cases() if c.kind != "expired_session"]

    def rows(self) -> list[dict[str, Any]]:
        return wsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(wsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"name": v["name"]}

    def effects(self) -> list[Effect]:
        match = {"name": ValueExpr(param="name")}
        return [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                       probe="exactly one group with this name", timeout_s=8.0)]
