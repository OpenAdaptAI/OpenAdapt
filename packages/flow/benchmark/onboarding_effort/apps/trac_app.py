"""The onboarding benchmark's Trac 1.6 instance (BSD-3-Clause).

Image ``localhost/apigen-trac:1.6`` is built from PyPI by
``benchmark/api_generalization/apps/trac.Containerfile`` (no Trac file is
stored here). This benchmark runs its own container, ``onboard-trac`` on
``127.0.0.1:9485``, with its own SQLite database. Reset copies a baseline file
back with SQLite's backup API.

Synthetic service-desk tickets only. Throwaway local passwords.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from typing import Any
from urllib.request import urlopen

C = "onboard-trac"
IMAGE = "localhost/apigen-trac:1.6"
PORT = 9485
BASE = f"http://127.0.0.1:{PORT}"
ENV = "/srv/trac"
DB = f"{ENV}/db/trac.db"
BASELINE = f"{ENV}/db/baseline.db"
USERS = {
    "admin": "synthetic-admin-pass",
    "triager": "synthetic-triager-pass",
}


def podman(*args: str, input: str | None = None, timeout: float = 300, check: bool = True) -> str:
    proc = subprocess.run(
        ["podman", *args], input=input, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args[:3])} failed: {proc.stderr[-400:]}")
    return proc.stdout


def admin(*args: str) -> str:
    return podman("exec", C, "trac-admin", ENV, *args)


def admin_quiet(*args: str) -> None:
    """trac-admin where "already so" is fine (removing an absent option)."""
    podman("exec", C, "trac-admin", ENV, *args, check=False)


def tsql(query: str, *, write: bool = False, params: tuple = ()) -> list[dict[str, Any]]:
    script = (
        "import json,sqlite3,sys\n"
        f"c=sqlite3.connect('file:{DB}' + ('' if {write!r} else '?mode=ro'), uri=True, timeout=10)\n"
        "c.row_factory=sqlite3.Row\n"
        "cur=c.execute(sys.argv[1], json.loads(sys.argv[2]))\n"
        "rows=[dict(r) for r in cur.fetchall()] if cur.description else []\n"
        "c.commit(); print(json.dumps(rows))\n"
    )
    return json.loads(podman("exec", C, "python", "-c", script, query, json.dumps(list(params))) or "[]")


def wait_http(path: str = "/", timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(BASE + path, timeout=5) as r:
                if r.status < 500:
                    return
        except Exception:  # noqa: BLE001 - starting up
            time.sleep(1)
    raise RuntimeError("Trac did not answer")


def exists() -> bool:
    return subprocess.run(["podman", "container", "exists", C]).returncode == 0


def host_db_dir() -> str:
    import os

    return os.path.join(os.environ.get("OE_SCRATCH", ""), "trac-db")


def up() -> None:
    """Start the container with its database directory on the host.

    The engineer's verify.yaml reads that file directly (``sqlite_database``);
    the podman VM shares /Users over virtiofs.
    """
    import os

    if exists():
        podman("start", C)
    else:
        host = host_db_dir()
        if not os.path.exists(os.path.join(host, "trac.db")):
            podman("create", "--name", C + "-seed", IMAGE)
            os.makedirs(host, exist_ok=True)
            podman("cp", f"{C}-seed:{ENV}/db/.", host)
            podman("rm", C + "-seed")
        podman("run", "-d", "--name", C, "-p", f"127.0.0.1:{PORT}:8000",
               "-v", f"{host}:{ENV}/db", IMAGE)
        # A second, non-admin account for the second installation's role change.
        line = "triager:{SHA}" + base64.b64encode(
            __import__("hashlib").sha1(USERS["triager"].encode()).digest()
        ).decode()
        podman("exec", C, "sh", "-c", f"echo '{line}' >> {ENV}/htpasswd")
    wait_http()


def stop() -> None:
    podman("stop", C, check=False)


def snapshot() -> None:
    podman("exec", C, "python", "-c",
           f"import sqlite3;s=sqlite3.connect('{DB}');d=sqlite3.connect('{BASELINE}');s.backup(d);d.close()")


def reset() -> None:
    podman("exec", C, "python", "-c",
           f"import sqlite3;s=sqlite3.connect('{BASELINE}');d=sqlite3.connect('{DB}');s.backup(d);d.close()")


def login(page: Any, user: str = "admin") -> None:
    auth = base64.b64encode(f"{user}:{USERS[user]}".encode()).decode()
    page.context.request.get(BASE + "/login", headers={"Authorization": f"Basic {auth}"})
    page.goto(BASE + "/")
    page.wait_for_load_state("load")
