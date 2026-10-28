"""A local Odoo 18 Community (LGPL-3.0) with synthetic contacts.

The skills registry's first non-healthcare entry: create a contact in the
Contacts app. Odoo's images are pulled by digest at run time
(environment.lock.json); nothing of Odoo is stored here.

- The web app (``ui_url``) is what a clerk uses.
- The record check reads ``res_partner`` through a read-only database role
  (``oa_record_check``) on the loopback database port.
- The oracle reads ``res_partner`` as the database owner from inside the
  database container, a channel the product never uses.

Generated secrets live in ``state_dir``, outside the repository. All data is
synthetic.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import urllib.request
import xmlrpc.client
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "compose.yml"
PROJECT = "openadapt-si-odoo"
WEB_PORT = 9412
DB_PORT = 9413
DATABASE = "oa"
DEFAULT_STATE = Path.home() / "oa/scratch/second-installation/odoo-state"
ACTOR_LOGIN = "openadapt.actor@example.invalid"
RECORD_ROLE = "oa_record_check"


class FixtureError(RuntimeError):
    pass


class OdooFixture:
    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = Path(
            state_dir or os.environ.get("ODOO_STATE_DIR") or DEFAULT_STATE
        )
        self.lock = json.loads((HERE / "environment.lock.json").read_text())
        for name, image in self.lock["services"].items():
            if "@sha256:" not in image:
                raise FixtureError(f"{name} image is not pinned by digest: {image}")

    @property
    def ui_url(self) -> str:
        return f"http://localhost:{WEB_PORT}/"

    # -- secrets and compose ---------------------------------------------------

    def secrets(self) -> dict[str, str]:
        path = self.state_dir / "secrets.json"
        if path.exists():
            return json.loads(path.read_text())
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "db_password": secrets.token_urlsafe(24),
            "admin_password": secrets.token_urlsafe(18),
            "actor_password": secrets.token_urlsafe(18),
            "record_check_password": secrets.token_urlsafe(24),
        }
        path.touch(mode=0o600)
        path.write_text(json.dumps(data, indent=2))
        return data

    def _env(self) -> dict[str, str]:
        s = self.secrets()
        images = self.lock["services"]
        return {
            "ODOO_IMAGE": images["odoo"],
            "ODOO_PG_IMAGE": images["postgres"],
            "ODOO_DB_PASSWORD": s["db_password"],
            "ODOO_PORT": str(WEB_PORT),
            "ODOO_DB_PORT": str(DB_PORT),
        }

    def compose(self, *args: str, timeout: float = 1800) -> str:
        proc = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **self._env()},
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise FixtureError(
                f"compose {' '.join(args[:2])} failed: {proc.stderr[-1500:]}"
            )
        return proc.stdout

    def psql(self, sql: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "odoo",
            "-d",
            DATABASE,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )

    # -- lifecycle -------------------------------------------------------------

    def up(self, *, wait_s: float = 900.0) -> None:
        """Start the database, create ``oa`` with Contacts once, start Odoo."""

        self.compose("up", "-d", "db")
        # Postgres answers (with 0 or 1) once it is up; 1 means ``oa`` exists.
        self._wait(lambda: self._db_exists().strip() in ("0", "1"), wait_s, "database")
        if self._db_exists().strip() != "1":
            self.compose(
                "run",
                "--rm",
                "odoo",
                "odoo",
                "-d",
                DATABASE,
                "-i",
                "base,contacts",
                "--without-demo=all",
                "--stop-after-init",
            )
        self.compose("up", "-d", "odoo")
        self._wait(self._web_ready, wait_s, "Odoo web")
        self.bootstrap()

    def down(self, *, volumes: bool = True) -> None:
        self.compose("down", *(["-v"] if volumes else []))

    def _db_exists(self) -> str:
        try:
            return self.compose(
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "odoo",
                "-d",
                "postgres",
                "-At",
                "-c",
                f"SELECT count(*) FROM pg_database WHERE datname = '{DATABASE}';",
            )
        except FixtureError:
            return ""

    def _web_ready(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.ui_url}web/login", timeout=5) as r:  # noqa: S310
                return r.status == 200
        except (URLError, HTTPError, OSError):
            return False

    @staticmethod
    def _wait(check: Any, wait_s: float, what: str) -> None:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(5)
        raise FixtureError(f"{what} not ready after {wait_s}s")

    # -- accounts --------------------------------------------------------------

    def _rpc(self, login: str, password: str) -> tuple[int, Any]:
        common = xmlrpc.client.ServerProxy(f"{self.ui_url}xmlrpc/2/common")
        uid = common.authenticate(DATABASE, login, password, {})
        if not uid:
            raise FixtureError(f"Odoo refused the sign-in for {login}")
        return uid, xmlrpc.client.ServerProxy(f"{self.ui_url}xmlrpc/2/object")

    def admin(self) -> tuple[int, Any, str]:
        """The administrator's XML-RPC session (its default password replaced once)."""

        s = self.secrets()
        try:
            uid, models = self._rpc("admin", s["admin_password"])
            return uid, models, s["admin_password"]
        except FixtureError:
            uid, models = self._rpc("admin", "admin")
            models.execute_kw(
                DATABASE,
                uid,
                "admin",
                "res.users",
                "write",
                [[uid], {"password": s["admin_password"]}],
            )
            uid, models = self._rpc("admin", s["admin_password"])
            return uid, models, s["admin_password"]

    def bootstrap(self) -> None:
        """The dedicated user and the record check's read-only role, once."""

        s = self.secrets()
        uid, models, pw = self.admin()
        # An internal user who may create and edit contacts ("Contact
        # Creation"); without it Odoo shows no save button.
        groups = [
            models.execute_kw(
                DATABASE,
                uid,
                pw,
                "ir.model.data",
                "check_object_reference",
                ["base", xmlid],
            )[1]
            for xmlid in ("group_user", "group_partner_manager")
        ]
        found = models.execute_kw(
            DATABASE, uid, pw, "res.users", "search", [[["login", "=", ACTOR_LOGIN]]]
        )
        if found:
            models.execute_kw(
                DATABASE,
                uid,
                pw,
                "res.users",
                "write",
                [found, {"groups_id": [[4, g] for g in groups]}],
            )
        else:
            models.execute_kw(
                DATABASE,
                uid,
                pw,
                "res.users",
                "create",
                [
                    {
                        "name": "OpenAdapt Actor",
                        "login": ACTOR_LOGIN,
                        "password": s["actor_password"],
                        "groups_id": [[6, 0, groups]],
                    }
                ],
            )
        exists = self.psql(
            f"SELECT count(*) FROM pg_roles WHERE rolname = '{RECORD_ROLE}';"
        )
        verb = "ALTER" if exists.strip() == "1" else "CREATE"
        self.psql(
            f"{verb} ROLE {RECORD_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"PASSWORD '{s['record_check_password']}';"
        )
        self.psql(
            f"ALTER ROLE {RECORD_ROLE} SET default_transaction_read_only = on; "
            f"GRANT SELECT ON res_partner TO {RECORD_ROLE};"
        )

    # -- the oracle and the harness's own writes ---------------------------------

    CONTACT_SQL = (
        "SELECT json_build_object('id', id::text, 'name', coalesce(name, ''), "
        "'email', coalesce(email, ''), 'phone', coalesce(phone, ''), "
        "'street', coalesce(street, ''), 'city', coalesce(city, ''), "
        "'zip', coalesce(zip, '')) FROM res_partner "
        "WHERE active AND NOT is_company AND type = 'contact' "
        "AND id NOT IN (SELECT partner_id FROM res_users) "
        "AND id NOT IN (SELECT partner_id FROM res_company) ORDER BY id;"
    )

    def contacts(self) -> list[dict[str, str]]:
        """Every live person contact (not a user, not a company): the oracle."""

        return [
            json.loads(line)
            for line in self.psql(self.CONTACT_SQL).splitlines()
            if line
        ]

    def create_contact(self, fields: dict[str, str]) -> str:
        uid, models, pw = self.admin()
        return str(
            models.execute_kw(DATABASE, uid, pw, "res.partner", "create", [fields])
        )

    def archive_all_contacts(self) -> int:
        """Reset: archive every person contact the benchmark made."""

        ids = [int(c["id"]) for c in self.contacts()]
        if ids:
            uid, models, pw = self.admin()
            models.execute_kw(
                DATABASE, uid, pw, "res.partner", "write", [ids, {"active": False}]
            )
        return len(ids)
