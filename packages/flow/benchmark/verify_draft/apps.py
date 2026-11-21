"""The three apps the verify-draft benchmark sets up (fixture code, not engine).

Each ``setup_*`` plays the operator: it brings the app to a seeded state,
records one demonstration as a HAR file, and writes ``task.yaml`` and
``sandbox.yaml``. Everything it writes is a fact a person would supply
(where the app is, how two users sign in, two database connections), never
a judgment about the check. All data is synthetic.

- **mockloan**: OpenAdapt's MockLoan fault server (SQLite), create a
  disbursement. Tuned on before (zero-config benchmark).
- **workbench**: the task-generation workbench's MockMed profile
  (PostgreSQL), update a patient's phone and email through HTML forms.
  Ours, tuned on before.
- **mealie**: Mealie v2.8.0 (PostgreSQL) on the lab, update a recipe through
  its REST API. Nobody had set it up or tuned on it before this benchmark.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

from benchmark.verify_draft.har import Recorder


@dataclass
class AppRun:
    name: str
    untuned: bool
    describe: str
    trace: Path
    task: Path
    sandbox: Path
    #: Held-out probes, chosen by the harness (not the model): other values
    #: for clean writes, and another record for an app-level sibling write.
    heldout_payloads: list[dict[str, Any]]
    other_record: dict[str, Any]
    #: Operator inputs the sandbox file holds (facts, not decisions).
    operator_inputs: list[str]
    teardown: Callable[[], None] = lambda: None
    setup_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


def _write(path: Path, data: Any) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# -- MockLoan -------------------------------------------------------------------


def setup_mockloan(work: Path) -> AppRun:
    from openadapt_flow.mockloan import fault_server

    started = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    url, db, stop = fault_server.serve(0)
    base = url.rstrip("/")
    for loan, prod, amt, memo in [
        ("LN-1001", "Personal", "2500.00", "Seed one"),
        ("LN-1002", "Auto", "9100.00", "Seed two"),
        ("LN-1003", "Home", "120000.00", "Seed three"),
    ]:
        requests.post(
            base + "/api/disbursement",
            json={"loan_id": loan, "product": prod, "amount": amt, "memo": memo},
            timeout=10,
        )
    # The demonstration, recorded; then undone so the sandbox starts seeded only.
    snapshot = shutil.copy(db.database_path, work / "seeded.sqlite3")
    rec = Recorder()
    rec.session.get(base + "/api/disbursements", timeout=10)
    params = {
        "loan_id": "LN-2040",
        "product": "Personal",
        "amount": "3150.00",
        "memo": "Kitchen repair advance",
    }
    rec.session.post(base + "/api/disbursement", json=params, timeout=10)
    rec.save(work / "trace.har")
    shutil.copy(snapshot, db.database_path)
    task = _write(
        work / "task.yaml",
        {
            "instruction": "Authorize a disbursement of 3150.00 on loan LN-2040 (Personal) with the memo 'Kitchen repair advance'.",
            "params": params,
        },
    )
    sandbox = _write(
        work / "sandbox.yaml",
        {
            "schema_version": "openadapt.verify-sandbox/v1",
            "app": {"base_url": base},
            "actors": {"agent": {"id": "officer-a"}, "other": {"id": "officer-b"}},
            "store": {
                "dialect": "sqlite",
                "read": {"sqlite_database": str(db.database_path)},
                "admin": {"sqlite_database": str(db.database_path)},
            },
        },
    )
    return AppRun(
        "mockloan",
        False,
        "db",
        work / "trace.har",
        task,
        sandbox,
        heldout_payloads=[
            {"product": "Auto", "amount": "4420.50", "memo": "Held-out memo A"},
            {"product": "Home", "amount": "77000.00", "memo": "Held-out memo B"},
            {"product": "Personal", "amount": "15.00", "memo": "Held-out memo C"},
            {"product": "Auto", "amount": "999.99", "memo": "Held-out memo D"},
            {"product": "Home", "amount": "5000.00", "memo": "Held-out memo E"},
        ],
        other_record={"loan_id": "LN-1002"},
        operator_inputs=["app base URL", "SQLite file path (read and admin)"],
        teardown=stop,
        setup_seconds=round(time.monotonic() - started, 1),
        notes=["MockLoan has no sign-in: the two actors send identical requests"],
    )


# -- the workbench (MockMed profile, PostgreSQL) --------------------------------


def _pg_cluster(work: Path, port: int) -> Callable[[], None]:
    data = work / "pgdata"
    if data.exists():
        shutil.rmtree(data)
    subprocess.run(
        [
            "initdb",
            "-D",
            str(data),
            "-U",
            "wb_admin",
            "--auth=trust",
            "-E",
            "UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(data),
            "-o",
            f"-p {port} -k '' -c listen_addresses=127.0.0.1",
            "-l",
            str(work / "pg.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    for sql in ("CREATE ROLE wb_reader LOGIN", "CREATE DATABASE wb_mockmed"):
        subprocess.run(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "wb_admin",
                "-d",
                "postgres",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
        )

    def stop() -> None:
        subprocess.run(
            ["pg_ctl", "-D", str(data), "-m", "fast", "stop"], capture_output=True
        )

    return stop


WB_SEED = {
    "patient": [
        {
            "mrn": "MRN-1001",
            "first_name": "Ana",
            "last_name": "Silva",
            "dob": "1980-02-11",
            "phone": "555-0101",
            "email": "ana.silva@example.org",
            "_as": "p1",
        },
        {
            "mrn": "MRN-1002",
            "first_name": "Ben",
            "last_name": "Okafor",
            "dob": "1975-07-30",
            "phone": "555-0102",
            "email": "ben.okafor@example.org",
            "_as": "p2",
        },
        {
            "mrn": "MRN-1003",
            "first_name": "Chen",
            "last_name": "Wu",
            "dob": "1990-12-01",
            "phone": "555-0103",
            "email": "chen.wu@example.org",
            "_as": "p3",
        },
    ],
    "encounter": [
        {"patient_id": "$p1", "type": "Triage", "note": "Seed visit one"},
        {"patient_id": "$p2", "type": "Consult", "note": "Seed visit two"},
    ],
}


def setup_workbench(work: Path, port: int = 9796) -> AppRun:

    started = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    stop_pg = _pg_cluster(work, port)
    dsn = f"host=127.0.0.1 port={port} user=wb_admin dbname=wb_mockmed"
    try:
        return _workbench(work, port, dsn, stop_pg, started)
    except BaseException:
        stop_pg()
        raise


def _workbench(
    work: Path, port: int, dsn: str, stop_pg: Callable[[], None], started: float
) -> AppRun:
    from openadapt_flow.taskgen import workbench as W
    from openadapt_flow.taskgen.profile import load_profile

    url, wb, stop_app = W.serve(load_profile("mockmed"), dsn, reader="wb_reader")
    wb.store.reset(WB_SEED)
    seeded = wb.store.dump()
    rec = Recorder()
    rec.session.post(
        url + "/login",
        data={"username": "clinician", "password": "synthetic"},
        allow_redirects=False,
        timeout=10,
    )
    pid = next(r["id"] for r in seeded["patient"] if r["mrn"] == "MRN-1002")
    rec.session.get(f"{url}/e/patient/{pid}/edit", timeout=10)
    form = {
        "__version": "1",
        "first_name": "Ben",
        "last_name": "Okafor",
        "dob": "1975-07-30",
        "phone": "555-0177",
        "email": "b.okafor@clinic.example.org",
    }
    rec.session.post(
        f"{url}/e/patient/{pid}/edit", data=form, allow_redirects=False, timeout=10
    )
    rec.save(work / "trace.har")
    wb.store.reset(WB_SEED)  # back to the seeded state
    task = _write(
        work / "task.yaml",
        {
            "instruction": "Update patient MRN-1002's phone to 555-0177 and email to b.okafor@clinic.example.org.",
            "params": {
                "mrn": "MRN-1002",
                "phone": "555-0177",
                "email": "b.okafor@clinic.example.org",
            },
        },
    )
    conn = {
        "sql_driver": "psycopg2",
        "sql_connect_args": {"host": "127.0.0.1", "port": port, "dbname": "wb_mockmed"},
    }
    sandbox = _write(
        work / "sandbox.yaml",
        {
            "schema_version": "openadapt.verify-sandbox/v1",
            "app": {"base_url": url},
            "actors": {
                "agent": {
                    "id": "clinician",
                    "login": {
                        "method": "POST",
                        "path": "/login",
                        "form": {"username": "clinician", "password": "synthetic"},
                    },
                }
            },
            "store": {
                "dialect": "postgresql",
                "read": {
                    **conn,
                    "sql_connect_args": {
                        **conn["sql_connect_args"],
                        "user": "wb_reader",
                    },
                },
                "admin": {
                    **conn,
                    "sql_connect_args": {
                        **conn["sql_connect_args"],
                        "user": "wb_admin",
                    },
                },
            },
        },
    )

    def teardown() -> None:
        stop_app()
        wb.store.conn.close()
        stop_pg()

    return AppRun(
        "workbench",
        False,
        "db",
        work / "trace.har",
        task,
        sandbox,
        heldout_payloads=[
            {"phone": "555-0901", "email": "heldout.a@example.org"},
            {"phone": "555-0902", "email": "heldout.b@example.org"},
            {"phone": "555-0903", "email": "heldout.c@example.org"},
            {"phone": "555-0904", "email": "heldout.d@example.org"},
            {"phone": "555-0905", "email": "heldout.e@example.org"},
        ],
        other_record={},
        operator_inputs=[
            "app base URL",
            "agent sign-in (user, password)",
            "read-only DB connection",
            "admin DB connection",
        ],
        teardown=teardown,
        setup_seconds=round(time.monotonic() - started, 1),
        notes=[
            "the workbench signs in one user only: no second actor, so no lost-update case and no actor axis"
        ],
    )


# -- Mealie (untuned, on the lab) ------------------------------------------------


MEALIE = os.environ.get("VD_MEALIE_URL", "http://127.0.0.1:9664")
MEALIE_PG_PORT = int(os.environ.get("VD_MEALIE_PG_PORT", "9665"))


def setup_mealie(work: Path) -> AppRun:
    """Mealie must be up (fresh volume) at VD_MEALIE_URL with its PostgreSQL
    at VD_MEALIE_PG_PORT (the lab stack, tunnelled to loopback)."""

    import psycopg2

    started = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VD_MEALIE_PG_PW", "vd-synthetic-pg")
    os.environ.setdefault("VD_MEALIE_READER_PW", "vd-synthetic-reader")
    os.environ.setdefault("VD_MEALIE_ADMIN_PW", "MyPassword")
    os.environ.setdefault("VD_MEALIE_COOK_PW", "vd-synthetic-cook")
    con = psycopg2.connect(
        host="127.0.0.1",
        port=MEALIE_PG_PORT,
        user="mealie",
        password=os.environ["VD_MEALIE_PG_PW"],
        dbname="mealie",
    )
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname='vd_reader'")
    if not cur.fetchone():
        cur.execute(
            "CREATE ROLE vd_reader LOGIN PASSWORD %s",
            (os.environ["VD_MEALIE_READER_PW"],),
        )
    cur.execute("GRANT USAGE ON SCHEMA public TO vd_reader")
    cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO vd_reader")
    cur.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO vd_reader")
    con.close()

    def token(user: str, pw: str) -> dict[str, str]:
        r = requests.post(
            MEALIE + "/api/auth/token",
            data={"username": user, "password": pw},
            timeout=30,
        )
        r.raise_for_status()
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    admin = token("changeme@example.com", os.environ["VD_MEALIE_ADMIN_PW"])
    me = requests.get(MEALIE + "/api/users/self", headers=admin, timeout=30).json()
    users = (
        requests.get(MEALIE + "/api/admin/users?perPage=50", headers=admin, timeout=30)
        .json()
        .get("items", [])
    )
    cook = next((u for u in users if u.get("username") == "cook2"), None)
    if cook is None:
        r = requests.post(
            MEALIE + "/api/admin/users",
            headers=admin,
            timeout=30,
            json={
                "username": "cook2",
                "fullName": "Second Cook",
                "email": "cook2@example.org",
                "password": os.environ["VD_MEALIE_COOK_PW"],
                "group": me["group"],
                "household": me["household"],
                "admin": False,
                "canInvite": False,
                "canManage": True,
                "canOrganize": True,
            },
        )
        r.raise_for_status()
        cook = r.json()
    for name, desc in [
        ("Lentil soup", "Seed recipe one"),
        ("Rye bread", "Seed recipe two"),
        ("Plum cake", "Seed recipe three"),
    ]:
        r = requests.post(
            MEALIE + "/api/recipes", headers=admin, json={"name": name}, timeout=30
        )
        if r.status_code < 300:
            requests.patch(
                MEALIE + f"/api/recipes/{r.json()}",
                headers=admin,
                json={"description": desc, "recipeYield": "4 servings"},
                timeout=30,
            )
    # The demonstration: one PATCH on lentil-soup, recorded, then undone.
    before = requests.get(
        MEALIE + "/api/recipes/lentil-soup", headers=admin, timeout=30
    ).json()
    rec = Recorder(admin)
    rec.session.get(MEALIE + "/api/recipes/lentil-soup", timeout=30)
    params = {
        "slug": "lentil-soup",
        "description": "Red lentils, cumin and lemon. Ready in 40 minutes.",
        "recipe_yield": "6 bowls",
    }
    rec.session.patch(
        MEALIE + "/api/recipes/lentil-soup",
        json={
            "description": params["description"],
            "recipeYield": params["recipe_yield"],
        },
        timeout=30,
    )
    rec.save(work / "trace.har")
    requests.patch(
        MEALIE + "/api/recipes/lentil-soup",
        headers=admin,
        json={
            "description": before.get("description"),
            "recipeYield": before.get("recipeYield"),
        },
        timeout=30,
    )
    (work / "openapi.json").write_text(
        requests.get(MEALIE + "/openapi.json", timeout=30).text, encoding="utf-8"
    )
    task = _write(
        work / "task.yaml",
        {
            "instruction": "Update the recipe lentil-soup: set its description to 'Red lentils, cumin and lemon. Ready in 40 minutes.' and its yield to '6 bowls'.",
            "params": params,
        },
    )
    pg = {
        "sql_driver": "psycopg2",
        "sql_connect_args": {
            "host": "127.0.0.1",
            "port": MEALIE_PG_PORT,
            "dbname": "mealie",
        },
    }
    sandbox = _write(
        work / "sandbox.yaml",
        {
            "schema_version": "openadapt.verify-sandbox/v1",
            "app": {"base_url": MEALIE},
            "actors": {
                "agent": {
                    "id": me["id"],
                    "login": {
                        "method": "POST",
                        "path": "/api/auth/token",
                        "token_field": "access_token",
                        "form": {
                            "username": "changeme@example.com",
                            "password": "${VD_MEALIE_ADMIN_PW}",
                        },
                    },
                },
                "other": {
                    "id": cook["id"],
                    "login": {
                        "method": "POST",
                        "path": "/api/auth/token",
                        "token_field": "access_token",
                        "form": {
                            "username": "cook2",
                            "password": "${VD_MEALIE_COOK_PW}",
                        },
                    },
                },
            },
            "store": {
                "dialect": "postgresql",
                "read": {
                    **pg,
                    "sql_connect_args": {**pg["sql_connect_args"], "user": "vd_reader"},
                    "sql_password_env": "VD_MEALIE_READER_PW",
                },
                "admin": {
                    **pg,
                    "sql_connect_args": {**pg["sql_connect_args"], "user": "mealie"},
                    "sql_password_env": "VD_MEALIE_PG_PW",
                },
            },
        },
    )
    return AppRun(
        "mealie",
        True,
        str(work / "openapi.json"),
        work / "trace.har",
        task,
        sandbox,
        heldout_payloads=[
            {"description": "Held-out description A.", "recipe_yield": "2 bowls"},
            {
                "description": "Held-out description B, with a comma.",
                "recipe_yield": "8 servings",
            },
            {"description": "Held-out description C.", "recipe_yield": "1 pot"},
            {"description": "Held-out description D.", "recipe_yield": "3 bowls"},
            {"description": "Held-out description E.", "recipe_yield": "10 servings"},
        ],
        other_record={"slug": "rye-bread"},
        operator_inputs=[
            "app base URL",
            "two users' sign-in (user, password env)",
            "read-only DB connection",
            "admin DB connection",
        ],
        setup_seconds=round(time.monotonic() - started, 1),
    )


SETUPS: dict[str, Callable[[Path], AppRun]] = {
    "mockloan": setup_mockloan,
    "workbench": setup_workbench,
    "mealie": setup_mealie,
}
