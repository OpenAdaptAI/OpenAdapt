"""Reset and system-of-record service for the AVC444 lab (stdlib only).

Runs beside each app, never in the session the harness drives:

- ``pn`` mode on vm-rdhost: reseeds the Patient Notes SQLite record with a
  drift condition, restarts the app in pnuser's session, and dumps every row.
- ``emr`` mode on vm-emr: reads ``patient_data`` from OpenEMR's MariaDB
  through ``docker exec`` and deletes rows a trial created.

The harness asks it for the record after each trial; it never reads the
screen to decide whether a save happened. The listener is bound to the
private address and the host firewall admits only the client VM.

    python oracle_service.py pn  10.30.1.7 8770
    python oracle_service.py emr 10.30.1.6 8771
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PY = r"C:\Program Files\Python312\python.exe"
PN_DB = r"C:\oa\pn_db.py"
CLIENT_IP = "10.30.1.5"
TOKEN_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
EMR_SQL = ["docker", "exec", "-i", "openemr-live-mysql-1", "mariadb", "-uroot", "-proot",
           "openemr", "--batch", "--raw", "-e"]


def pn_reset(drift: str) -> dict:
    if drift not in ("none", "siblings", "decoy", "reorder"):
        raise ValueError(f"unknown drift {drift!r}")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
         "Where-Object { $_.ProcessId -ne $PID -and "
         "$_.CommandLine -like '*patient_notes.ps1*' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        check=True, capture_output=True,
    )
    seeded = subprocess.run([PY, PN_DB, "seed", "--drift", drift], check=True,
                            capture_output=True, text=True).stdout
    subprocess.run(["schtasks", "/run", "/tn", "oa-app-pnuser"], check=True, capture_output=True)
    time.sleep(6)
    return {"seeded": json.loads(seeded), "restarted": True}


def edge_restart() -> dict:
    """Restart OpenEMR's Edge window in emruser's session (cookies kept)."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process msedge -IncludeUserName -ErrorAction SilentlyContinue | "
         "Where-Object { $_.UserName -like '*emruser' } | Stop-Process -Force"],
        check=True, capture_output=True,
    )
    time.sleep(2)
    subprocess.run(["schtasks", "/run", "/tn", "oa-app-emruser"], check=True, capture_output=True)
    time.sleep(8)
    return {"restarted": True}


def pn_all() -> list:
    out = subprocess.run([PY, PN_DB, "all"], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def emr_query(sql: str) -> list[dict]:
    out = subprocess.run(EMR_SQL + [sql], check=True, capture_output=True, text=True).stdout
    lines = out.splitlines()
    if not lines:
        return []
    cols = lines[0].split("\t")
    return [dict(zip(cols, line.split("\t"))) for line in lines[1:]]


def emr_patients(token: str) -> list[dict]:
    if not TOKEN_RE.match(token):
        raise ValueError("bad token")
    return emr_query(
        "SELECT pid, fname, lname, DOB, sex FROM patient_data "
        f"WHERE fname LIKE '%{token}%' OR lname LIKE '%{token}%' ORDER BY pid"
    )


def emr_cleanup(prefix: str) -> dict:
    if not TOKEN_RE.match(prefix) or len(prefix) < 4:
        raise ValueError("bad prefix")
    rows = emr_patients(prefix)
    emr_query(f"DELETE FROM patient_data WHERE fname LIKE '{prefix}%' OR lname LIKE '{prefix}%'")
    return {"deleted": len(rows)}


class Handler(BaseHTTPRequestHandler):
    mode = "pn"

    def _reply(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self) -> object:
        if self.client_address[0] not in (CLIENT_IP, "127.0.0.1"):
            raise PermissionError(self.client_address[0])
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = {
            ("pn", "/reset"): lambda: pn_reset(q.get("drift", "none")),
            ("pn", "/all"): pn_all,
            ("pn", "/edge_restart"): edge_restart,
            ("emr", "/patients"): lambda: emr_patients(q["token"]),
            ("emr", "/cleanup"): lambda: emr_cleanup(q["prefix"]),
        }
        return routes[(self.mode, url.path)]()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._reply(200, self._route())
        except PermissionError:
            self._reply(403, {"error": "forbidden"})
        except Exception as exc:  # noqa: BLE001 -- report, keep serving
            self._reply(500, {"error": repr(exc)})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))


def main(mode: str, host: str, port: str) -> None:
    Handler.mode = mode
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main(*sys.argv[1:])
