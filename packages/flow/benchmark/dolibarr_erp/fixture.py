"""Dolibarr ERP fixture for the record-graded sample environment.

Dolibarr (GPL-3.0) runs from its own upstream image, pinned by digest, in a
separate container. Nothing from Dolibarr is copied into this package: the
seed is our own synthetic business data, posted through Dolibarr's REST API
at bootstrap, and the baseline snapshot is built on the operator's machine.

    python -m benchmark.dolibarr_erp.fixture up         # start db + app, install
    python -m benchmark.dolibarr_erp.fixture bootstrap  # seed + snapshot
    python -m benchmark.dolibarr_erp.fixture reset      # restore the baseline (timed)
    python -m benchmark.dolibarr_erp.fixture down       # remove containers and volumes

Ports (loopback only): the app on ``DOLIBARR_PORT`` (default 9760) and its
database on the next port. The agent's browser is confined to the app's
origin; the grader reads the database on its own port.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

APP_IMAGE = "docker.io/dolibarr/dolibarr:20.0.2@sha256:cd19591476431610c8004e96aa619209dfd7f81e8d0c9cae6d19f565ff161af9"
DB_IMAGE = "docker.io/library/mariadb:11.8@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
# Throwaway credentials of a loopback-only local container; not secrets.
DB_PASS = "synthetic-db-pass"
ADMIN_LOGIN = "admin"
ADMIN_PASS = "synthetic-admin-pass"
SEED_API_KEY = "seed-only-key-0123456789abcdef"
DB_NAME = "dolibarr"
MODULES = "Societe,Product,Service,Propale,Commande,Facture,Banque,Projet,Categorie,Stock,Api"

HERE = Path(__file__).resolve().parent


def cli() -> str:
    """``podman`` or ``docker`` (``OPENADAPT_CONTAINER_CLI`` overrides)."""

    forced = os.environ.get("OPENADAPT_CONTAINER_CLI")
    if forced:
        return forced
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    raise SystemExit("needs podman or docker on PATH")


def port() -> int:
    return int(os.environ.get("DOLIBARR_PORT", "9760"))


def prefix() -> str:
    return os.environ.get("DOLIBARR_SLOT", "oa-doli")


def state_dir() -> Path:
    d = Path(os.environ.get("DOLIBARR_STATE", Path.home() / ".cache" / "openadapt" / "dolibarr-erp")) / prefix()
    d.mkdir(parents=True, exist_ok=True)
    return d


def app_url() -> str:
    return f"http://127.0.0.1:{port()}"


def _run(args: list[str], *, input: Optional[bytes] = None, timeout: float = 300, check: bool = True) -> bytes:
    r = subprocess.run([cli(), *args], input=input, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cli()} {' '.join(args[:4])}... failed: {r.stderr.decode(errors='replace')[-600:]}")
    return r.stdout


def _names() -> dict[str, str]:
    p = prefix()
    return {"net": p, "db": f"{p}-db", "app": f"{p}-app", "dbvol": f"{p}-dbdata", "docvol": f"{p}-docs"}


# -- lifecycle ------------------------------------------------------------------------


def up() -> None:
    n = _names()
    exists = subprocess.run([cli(), "container", "inspect", n["app"]], capture_output=True).returncode == 0
    if exists:
        _run(["start", n["db"]])
        _wait_db()
        _run(["start", n["app"]])
        _wait_http()
        return
    _run(["network", "create", n["net"]], check=False)
    _run(["run", "-d", "--name", n["db"], "--network", n["net"], "--network-alias", "db",
          "-p", f"127.0.0.1:{port() + 1}:3306", "-v", f"{n['dbvol']}:/var/lib/mysql",
          "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}", "-e", f"MARIADB_DATABASE={DB_NAME}", DB_IMAGE])
    _wait_db()
    _run(["run", "-d", "--name", n["app"], "--network", n["net"], "-p", f"127.0.0.1:{port()}:80",
          "-v", f"{n['docvol']}:/var/www/documents",
          "-e", "DOLI_DB_HOST=db", "-e", f"DOLI_DB_NAME={DB_NAME}", "-e", "DOLI_DB_USER=root",
          "-e", f"DOLI_DB_PASSWORD={DB_PASS}", "-e", f"DOLI_ADMIN_LOGIN={ADMIN_LOGIN}",
          "-e", f"DOLI_ADMIN_PASSWORD={ADMIN_PASS}", "-e", "DOLI_INSTALL_AUTO=1", "-e", "DOLI_INIT_DEMO=0",
          "-e", f"DOLI_URL_ROOT={app_url()}", "-e", "DOLI_COMPANY_NAME=Synthetic Supplies",
          "-e", "DOLI_COMPANY_COUNTRYCODE=US", "-e", f"DOLI_ENABLE_MODULES={MODULES}", APP_IMAGE])
    _wait_http(timeout_s=900)
    # The first page load finishes the automatic install; wait for its user.
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            if query("SELECT COUNT(*) AS n FROM llx_user WHERE login=%s", (ADMIN_LOGIN,))[0]["n"]:
                return
        except Exception:  # noqa: BLE001 - tables not there yet
            pass
        time.sleep(3)
    raise RuntimeError("Dolibarr's automatic install did not finish")


def down() -> None:
    n = _names()
    for c in (n["app"], n["db"]):
        _run(["rm", "-f", c], check=False)
    for v in (n["dbvol"], n["docvol"]):
        _run(["volume", "rm", "-f", v], check=False)
    _run(["network", "rm", n["net"]], check=False)


def _wait_http(timeout_s: float = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(app_url() + "/index.php", timeout=5) as r:  # noqa: S310
                if r.status < 500:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise RuntimeError("Dolibarr did not answer")


def _wait_db(timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with connect() as c:
                c.cursor().execute("SELECT 1")
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("the database did not answer")


# -- database access (the grader's channel; never the agent's) ------------------------


@contextmanager
def connect() -> Iterator[Any]:
    import pymysql

    conn = pymysql.connect(host="127.0.0.1", port=port() + 1, user="root", password=DB_PASS,
                           database=DB_NAME, charset="utf8mb4", autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, args: Any = None) -> list[dict[str, Any]]:
    with connect() as c:
        cur = c.cursor()
        cur.execute(sql, args)
        return list(cur.fetchall())


def execute(sql: str, args: Any = None) -> int:
    with connect() as c:
        return c.cursor().execute(sql, args)


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else format(v, "f")
    if isinstance(v, bytes):
        return "0x" + v.hex()
    return str(v)


def read_record() -> dict[str, Any]:
    """Every row of every table, keyed by primary key, in one consistent read.

    ``{"tables": {name: {"key": [cols], "rows": {key: row}}}}``. A table with no
    primary key is keyed by the row's own content (a multiset).
    """

    out: dict[str, Any] = {}
    with connect() as c:
        cur = c.cursor()
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        cur.execute(
            "SELECT TABLE_NAME AS t, COLUMN_NAME AS c FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (DB_NAME,),
        )
        pks: dict[str, list[str]] = {}
        for r in cur.fetchall():
            pks.setdefault(r["t"], []).append(r["c"])
        cur.execute("SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME", (DB_NAME,))
        names = [r["t"] for r in cur.fetchall()]
        for t in names:
            cur.execute(f"SELECT * FROM `{t}`")
            rows = cur.fetchall()
            key = pks.get(t, [])
            table: dict[str, Any] = {}
            for row in rows:
                plain = {k: _plain(v) for k, v in row.items()}
                if key:
                    k = "|".join(str(plain[c]) for c in key)
                else:
                    k = hashlib.sha256(json.dumps(plain, sort_keys=True).encode()).hexdigest()[:16]
                    while k in table:  # identical rows: a multiset
                        k += "+"
                table[k] = plain
            out[t] = {"key": key, "rows": table}
        cur.execute("COMMIT")
    return {"tables": out}


# -- snapshot and reset ---------------------------------------------------------------


def snapshot() -> str:
    """Write the baseline (SQL dump + documents tarball); return its SHA-256."""

    n = _names()
    dump = _run(["exec", n["db"], "mariadb-dump", "-uroot", f"-p{DB_PASS}", "--skip-dump-date",
                 "--single-transaction", "--add-drop-table", "--skip-comments", DB_NAME], timeout=300)
    if len(dump) < 10_000:
        raise RuntimeError("the database dump is implausibly small")
    docs = _run(["exec", n["app"], "tar", "-C", "/var/www/documents", "-cf", "-", "."], timeout=120)
    sd = state_dir()
    (sd / "baseline.sql").write_bytes(dump)
    (sd / "documents.tar").write_bytes(docs)
    digest = hashlib.sha256(dump).hexdigest()
    (sd / "baseline.sha256").write_text(digest + "\n")
    return digest


def reset(**_: Any) -> float:
    """Restore the baseline database and documents; return the seconds it took.

    The app stays up: Dolibarr keeps no state outside the database and the
    documents directory except PHP sessions, and every episode signs in anew.
    """

    started = time.monotonic()
    n = _names()
    sd = state_dir()
    dump = (sd / "baseline.sql").read_bytes()
    if hashlib.sha256(dump).hexdigest() != (sd / "baseline.sha256").read_text().strip():
        raise RuntimeError("baseline hash mismatch; rebuild it with bootstrap")
    _run(["exec", "-i", n["db"], "mariadb", "-uroot", f"-p{DB_PASS}", DB_NAME], input=dump, timeout=300)
    _run(["exec", n["app"], "sh", "-c", "find /var/www/documents -mindepth 1 -delete"], timeout=120)
    _run(["exec", "-i", n["app"], "tar", "-C", "/var/www/documents", "-xf", "-"],
         input=(sd / "documents.tar").read_bytes(), timeout=120)
    _run(["exec", n["app"], "sh", "-c", "chown -R www-data:www-data /var/www/documents; "
          "rm -f /var/lib/php/sessions/sess_* 2>/dev/null; true"], timeout=60)
    return time.monotonic() - started


def record_digest() -> str:
    """A hash of the whole record, for checking that reset is deterministic."""

    rec = read_record()
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()


# -- bootstrap ------------------------------------------------------------------------


def bootstrap() -> str:
    from benchmark.dolibarr_erp import seed

    seed.before_sql(execute)
    execute("UPDATE llx_user SET api_key=%s WHERE login=%s", (SEED_API_KEY, ADMIN_LOGIN))
    try:
        seed.load(app_url() + "/api/index.php", SEED_API_KEY)
    finally:
        execute("UPDATE llx_user SET api_key=NULL WHERE login=%s", (ADMIN_LOGIN,))
    seed.finish_sql(execute)
    return snapshot()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m benchmark.dolibarr_erp.fixture")
    ap.add_argument("command", choices=["up", "bootstrap", "snapshot", "reset", "down", "digest", "time-reset"])
    ap.add_argument("--n", type=int, default=5)
    a = ap.parse_args(argv)
    if a.command == "up":
        up()
        print(f"Dolibarr is up at {app_url()}")
    elif a.command == "bootstrap":
        print("baseline sha256", bootstrap())
    elif a.command == "snapshot":
        print("baseline sha256", snapshot())
    elif a.command == "reset":
        print(f"reset in {reset():.1f} s")
    elif a.command == "down":
        down()
    elif a.command == "digest":
        print(record_digest())
    elif a.command == "time-reset":
        rows = []
        for i in range(a.n):
            # Dirty the record a little first, so the reset has work to undo.
            execute("UPDATE llx_societe SET phone='000' WHERE rowid=(SELECT MIN(rowid) FROM llx_societe)")
            s = reset()
            rows.append({"run": i + 1, "seconds": round(s, 2), "record_sha256": record_digest()})
            print(json.dumps(rows[-1]), flush=True)
        same = len({r["record_sha256"] for r in rows}) == 1
        print(json.dumps({"max_seconds": max(r["seconds"] for r in rows), "deterministic": same}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
