"""The apps a lifecycle run drives, and every event the harness injects into them.

OpenEMR 8.0.0.3 runs as its own Compose project (``openadapt-openemr-lifecycle``,
loopback ports 9421 for the browser and 9420 for the API) from the pinned
fixture in ``benchmark/openemr_local``, with synthetic data only. Nothing here
touches another session's copy.

Injected events, each logged with exactly what was changed:

- other clerks: a chart created through the REST API as the actor account,
  or an unrelated chart's home phone changed through SQL;
- forced logout: every PHP session file in the app container is deleted;
- slow save: a ``BEFORE INSERT`` trigger on ``patient_data`` that sleeps for
  the item's patient only;
- failed save: the same trigger raising an error, a set number of times (the
  counter lives in a non-transactional table, so it survives the rollback);
- UI changes, served to every client as a vendor release would be:
  - ``theme``: a stylesheet that changes colours, fonts and spacing on the
    New Patient form and its duplicate-check popup;
  - ``relabel``: the save button "Confirm Create New Patient" becomes
    "Save new patient", and "Create New Patient" becomes "Check and create";
  - ``reorder``: the demographics layout (``layout_options``) puts Birth Sex
    before DOB, and Home Phone and Contact Email before Address;
  - ``interstitial``: a notice over the New Patient form that must be
    dismissed ("Continue") before the form takes input.
  A version bump is not run: the lock pins one OpenEMR image and a second
  pinned image would need its own baseline (see the report's limits).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
PROJECT = "openadapt-openemr-lifecycle"
UI_PORT = "9421"
API_PORT = "9420"
WEBROOT = "/var/www/localhost/htdocs/openemr"
FORM = f"{WEBROOT}/interface/new/new_comprehensive.php"
POPUP = f"{WEBROOT}/interface/new/new_search_popup.php"
DRIFT_JS = f"{WEBROOT}/public/lifecycle-drift.js"
SQL_FIELDS = (
    "id",
    "pid",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)

THEME_CSS = (
    "body{background:#fbf6ee !important;font-family:Georgia,serif !important;}"
    ".card,.card-header,.nav-tabs .nav-link{background:#efe4d2 !important;}"
    ".btn-primary{background:#6b3d8f !important;border-color:#6b3d8f !important;}"
    "label,.label_custom{color:#5a3218 !important;}"
    ".form-control{border-radius:0 !important;border-color:#a08060 !important;}"
)
INTERSTITIAL_JS = r"""
(function(){
  if (!/new_comprehensive\.php/.test(location.pathname)) return;
  function show(){
    if (document.getElementById('oa-lifecycle-notice')) return;
    var d=document.createElement('div');
    d.id='oa-lifecycle-notice';
    d.setAttribute('role','dialog');
    d.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center';
    d.innerHTML='<div style="background:#fff;padding:24px;max-width:420px;border-radius:6px;font:15px sans-serif">'+
      '<h3 style="margin-top:0">Registration form updated</h3>'+
      '<p>Contact details are now checked when you save. Review the new layout before you continue.</p>'+
      '<button type="button" class="btn btn-primary" id="oa-lifecycle-continue">Continue</button></div>';
    document.body.appendChild(d);
    document.getElementById('oa-lifecycle-continue').onclick=function(){d.remove();};
  }
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',show); else show();
})();
"""


def podman_env() -> dict[str, str]:
    """Docker Compose pointed at the benchmark Podman machine."""

    env = dict(os.environ)
    if not env.get("DOCKER_HOST"):
        sock = subprocess.run(
            [
                "podman",
                "machine",
                "inspect",
                "openadapt-benchmark",
                "--format",
                "{{.ConnectionInfo.PodmanSocket.Path}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if sock:
            env["DOCKER_HOST"] = f"unix://{sock}"
    return env


def make_fixture(state_dir: Path) -> Any:
    import benchmark.openemr_local.fixture as fx

    class LifecycleFixture(fx.OpenEMRFixture):
        def __init__(self, state: Path) -> None:
            super().__init__(state)
            self.lock = {**self.lock, "project": PROJECT}

        @property
        def ui_base_url(self) -> str:
            return f"http://127.0.0.1:{UI_PORT}"

        @property
        def api_base_url(self) -> str:
            return f"https://127.0.0.1:{API_PORT}"

        def _expected(self) -> dict[str, str]:
            return {
                "OPENEMR_IMAGE": self.lock["services"]["openemr"],
                "MARIADB_IMAGE": self.lock["services"]["mariadb"],
                "OPENEMR_ACTOR_USER": "openadapt_actor",
                "OPENEMR_HTTP_PORT": UI_PORT,
                "OPENEMR_HTTPS_PORT": API_PORT,
            }

        def _validate_runtime_env(self) -> None:
            values = self._runtime_values()
            for k, v in self._expected().items():
                if values.get(k) != v:
                    raise fx.FixtureError(f"runtime.env {k} differs")
            self.runtime_env.chmod(0o600)

        def runtime_preflight(self) -> None:
            # The shared machine's rules (~/oa/scratch/fixture-lease/README.md):
            # at least 15 GiB free and a 1-minute load under 40 before any
            # container starts. The pinned images are already in the Podman
            # machine.
            import shutil

            free = shutil.disk_usage("/System/Volumes/Data").free / 2**30
            if free < 15:
                raise fx.FixtureError(f"only {free:.1f} GiB free on the host")
            load = os.getloadavg()[0]
            if load >= 40:
                raise fx.FixtureError(f"1-minute load is {load:.0f}; wait for < 40")

        def prepare(self) -> None:
            import secrets

            self._protect_state_dir()
            if not self.runtime_env.exists():
                values = {
                    **self._expected(),
                    "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
                    "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
                    "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
                }
                self._write_private_exclusive(
                    self.runtime_env,
                    (
                        "# generated local synthetic fixture; never commit\n"
                        + "".join(f"{k}={v}\n" for k, v in values.items())
                    ).encode(),
                )
            super().prepare()

        def _run(self, argv, *, input_bytes=None, timeout_s=300.0):  # type: ignore[override]
            # Same as the base, with Compose pointed at the Podman machine.
            if argv[:2] == ["docker", "compose"] or argv[:1] == ["docker"]:
                proc = subprocess.run(
                    argv,
                    input=input_bytes,
                    capture_output=True,
                    timeout=timeout_s,
                    env=podman_env(),
                    check=False,
                )
                if proc.returncode != 0:
                    raise fx.FixtureError(
                        f"{' '.join(argv[:4])} failed: {proc.stderr.decode()[-800:]}"
                    )
                return proc
            return super()._run(argv, input_bytes=input_bytes, timeout_s=timeout_s)

    return LifecycleFixture(state_dir)


class OpenEMRApp:
    """The app adapter the harness drives: reset, read, inject, change."""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = Path(
            state_dir
            or os.environ.get("LIFECYCLE_OPENEMR_STATE")
            or Path.home() / "oa" / "scratch" / "lifecycle" / "openemr-state"
        )
        self.fx = make_fixture(self.state_dir)
        self.db_container = f"{PROJECT}-db-1"
        self.app_container = f"{PROJECT}-openemr-1"
        self.ui_url = self.fx.ui_base_url
        self.api_url = self.fx.api_base_url
        self.drift_on: list[str] = []
        self._fault_lock = threading.Lock()
        from benchmark.openemr_throughput.harness import CLERKS

        #: Added before the baseline snapshot, so every reset keeps them.
        self._clerks: list[str] = list(CLERKS[:3])

    # -- bring-up ----------------------------------------------------------

    def bootstrapped(self) -> bool:
        return all(
            p.is_file()
            for p in (
                self.fx.runtime_env,
                self.fx.clients_path,
                self.fx.snapshot_path,
                self.fx.snapshot_hash_path,
            )
        )

    def bring_up(self, say: Any = print) -> str:
        if self.bootstrapped():
            say("starting the existing copy")
            self.fx._compose("up", "-d", timeout_s=900.0)
            self.fx._wait_ready(timeout_s=900.0)
            return "restarted"
        self.fx.runtime_preflight()
        say("checking the pinned OpenEMR release")
        self.fx.prepare()
        self.fx.image_identity()
        say("starting; OpenEMR installs itself on first start")
        self.fx._compose("up", "-d", timeout_s=900.0)
        self.fx._wait_ready(timeout_s=1800.0)
        self.fx._verify_running_source()
        say("adding synthetic API clients")
        self.fx.bootstrap()
        self.add_clerks(3)
        self.fx.snapshot()
        return "created"

    def describe(self) -> dict[str, Any]:
        return {
            "app": "openemr",
            "version": "8.0.0.3",
            "project": PROJECT,
            "ui_url": self.ui_url,
            "api_url": self.api_url,
            "images": self.fx.lock["services"],
            "baseline_sha256": self.fx.baseline_hash() if self.bootstrapped() else None,
        }

    def extra(self) -> dict[str, Any]:
        """What arms may use. ``rest_read_session()`` returns an httpx client
        with a read-only bearer token (``user/patient.rs``), the same REST
        read-back OpenAdapt's record check uses; ``rest_write_session()`` one
        with the actor's create/update scope. Arms never get the oracle's SQL."""

        return {
            "fixture_state": str(self.state_dir),
            "fixture_lock": str(self.state_dir / "environment.lock.json"),
            "project": PROJECT,
            "ui_port": int(UI_PORT),
            "api_port": int(API_PORT),
            "rest_read_session": lambda: self.fx.token_session("oracle"),
            "rest_write_session": lambda: self.fx.token_session("actor"),
        }

    def point_hooks_here(self) -> None:
        """verify.yaml's hooks (sign-in, REST read-back) read these variables."""

        lock = self.state_dir / "environment.lock.json"
        lock.write_text(json.dumps(self.fx.lock, indent=2) + "\n")
        os.environ["OPENEMR_FIXTURE_STATE"] = str(self.state_dir)
        os.environ["OPENEMR_FIXTURE_LOCK"] = str(lock)
        os.environ["OPENEMR_UI_URL"] = self.ui_url
        os.environ["OPENEMR_API_URL"] = self.api_url
        os.environ.update({k: v for k, v in podman_env().items() if k == "DOCKER_HOST"})

    # -- accounts ------------------------------------------------------------

    def add_clerks(self, n: int) -> list[str]:
        from benchmark.openemr_throughput.harness import add_clerks

        self._clerks = add_clerks(self.fx, n)
        return self._clerks

    def accounts(self) -> list[tuple[str, str]]:
        pw = self.fx._runtime_values()["OPENEMR_ACTOR_PASSWORD"]
        users = ["openadapt_actor"] + [
            c for c in self._clerks if c != "openadapt_actor"
        ]
        return [(u, pw) for u in users]

    # -- direct database channel (the oracle's) -----------------------------

    def sql(self, statement: str, *, timeout_s: float = 120.0) -> str:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-i",
                self.db_container,
                "sh",
                "-c",
                'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot --batch --raw '
                "--skip-column-names",
            ],
            input=statement.encode(),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"sql failed: {proc.stderr.decode()[-500:]}")
        return proc.stdout.decode()

    def rows(self) -> list[dict[str, str]]:
        out = self.sql(
            "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id;"
        )
        return [
            {
                k: ("" if v == "NULL" else v)
                for k, v in zip(SQL_FIELDS, line.split("\t"))
            }
            for line in out.splitlines()
            if line
        ]

    def app_exec(self, script: str, *, timeout_s: float = 60.0) -> str:
        proc = subprocess.run(
            ["podman", "exec", self.app_container, "sh", "-c", script],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"app exec failed: {proc.stderr.decode()[-500:]}")
        return proc.stdout.decode()

    # -- state ---------------------------------------------------------------

    def reset(self) -> str:
        self.clear_all_events()
        return self.fx.reset()

    def seed_existing(self, people: list[dict[str, str]]) -> list[str]:
        return [self.create_chart(p, who="seed") for p in people]

    def create_chart(self, fields: dict[str, str], *, who: str) -> str:
        """A chart created through the REST API (another clerk, the operator
        doing an item by hand, or the seed). Returns its patient_data id."""

        body = {
            "title": fields["title"],
            "fname": fields["fname"].strip(),
            "lname": fields["lname"].strip(),
            "DOB": fields["DOB"],
            "sex": fields["sex"],
            "street": fields["street"],
            "city": fields["city"],
            "state": "MA",
            "postal_code": fields["postal_code"],
            "phone_home": fields.get("phone_home", ""),
            "email": fields["email"],
            "country_code": fields.get("country_code", "USA"),
        }
        before = {r["id"] for r in self.rows()}
        session = self.fx.token_session("actor")
        try:
            resp = session.post(f"{self.api_url}/apis/default/api/patient", json=body)
        finally:
            session.close()
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"{who} create returned HTTP {resp.status_code}")
        new = [r["id"] for r in self.rows() if r["id"] not in before]
        if len(new) != 1:
            raise RuntimeError(f"{who} create made {len(new)} rows")
        return new[0]

    # -- item-level events ------------------------------------------------------

    def inject(
        self,
        inj: dict[str, Any],
        truth: Any,
        *,
        harness: set[str],
        stop: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        event = inj["event"]
        if event == "other_clerk_creates":
            cid = self.create_chart(inj["person"], who="other clerk")
            return {"event": event, "chart_id": cid}
        if event == "force_logout":
            n = self.app_exec(
                "n=$(ls /tmp/sess_* 2>/dev/null | wc -l); rm -f /tmp/sess_*; echo $n"
            )
            return {"event": event, "sessions_ended": int(n.strip() or 0)}
        if event == "other_clerk_edits_unrelated":
            # wait a moment so the edit lands while the item is under way
            if stop is not None:
                stop.wait(5.0)
            rows = [r for r in self.rows() if r["id"] not in harness]
            target = rows[truth.seq % len(rows)] if rows else None
            if target is None:
                return {"event": event, "touched_ids": []}
            phone = f"555-01{truth.seq % 100:02d}"
            self.sql(
                f"UPDATE openemr.patient_data SET phone_home='{phone}' "
                f"WHERE id={int(target['id'])};"
            )
            return {"event": event, "touched_ids": [target["id"]], "phone_home": phone}
        if event in ("slow_save", "failed_save"):
            email = truth.person["email"]
            assert re.fullmatch(r"[a-z0-9.@-]+", email)
            if event == "slow_save":
                body = f"IF NEW.email = '{email}' THEN SET @oa_slept = SLEEP({int(inj['seconds'])}); END IF;"
            else:
                body = (
                    f"IF NEW.email = '{email}' AND (SELECT n FROM lifecycle.fault_budget "
                    f"WHERE k='{email}') > 0 THEN "
                    f"UPDATE lifecycle.fault_budget SET n = n - 1 WHERE k='{email}'; "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'lifecycle: injected save failure'; "
                    "END IF;"
                )
            self.sql(
                "CREATE DATABASE IF NOT EXISTS lifecycle; "
                "CREATE TABLE IF NOT EXISTS lifecycle.fault_budget "
                "(k VARCHAR(190) PRIMARY KEY, n INT) ENGINE=Aria TRANSACTIONAL=0; "
                f"REPLACE INTO lifecycle.fault_budget VALUES ('{email}', {int(inj.get('times', 0))}); "
                "DROP TRIGGER IF EXISTS openemr.oa_lifecycle_fault; "
                "CREATE TRIGGER openemr.oa_lifecycle_fault BEFORE INSERT ON openemr.patient_data "
                f"FOR EACH ROW BEGIN {body} END;"
            )
            return {"event": event, "trigger": body}
        raise ValueError(f"unknown injection {event}")

    def clear_item_events(self) -> list[str]:
        out = self.sql(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_NAME='oa_lifecycle_fault';"
        )
        if out.strip():
            self.sql("DROP TRIGGER IF EXISTS openemr.oa_lifecycle_fault;")
            return ["oa_lifecycle_fault"]
        return []

    # -- UI changes ----------------------------------------------------------------

    def _ensure_originals(self) -> None:
        self.app_exec(
            f"for f in {FORM} {POPUP}; do [ -f $f.oa-orig ] || cp -p $f $f.oa-orig; done"
        )

    def _write_drift_js(self) -> None:
        parts = []
        if "theme" in self.drift_on:
            parts.append(
                "(function(){var s=document.createElement('style');s.textContent="
                + json.dumps(THEME_CSS)
                + ";(document.head||document.documentElement).appendChild(s);})();"
            )
        if "interstitial" in self.drift_on:
            parts.append(INTERSTITIAL_JS)
        js = "\n".join(parts)
        self.app_exec(f"cat > {DRIFT_JS} <<'OAEOF'\n{js}\nOAEOF\nchmod 644 {DRIFT_JS}")

    def apply_drift(self, kind: str) -> dict[str, Any]:
        self._ensure_originals()
        if kind not in self.drift_on:
            self.drift_on.append(kind)
        changed: dict[str, Any] = {"kind": kind}
        if kind in ("theme", "interstitial"):
            self._write_drift_js()
            tag = '<script src="/public/lifecycle-drift.js"></script>'
            for f in (FORM, POPUP):
                self.app_exec(
                    f"grep -q lifecycle-drift.js {f} || "
                    f"sed -i 's#</body>#{tag}</body>#' {f}"
                )
            changed["files"] = [FORM, POPUP, DRIFT_JS]
        elif kind == "relabel":
            self.app_exec(
                f"sed -i \"s/xla('Confirm Create New Patient')/xla('Save new patient')/\" {POPUP}; "
                f"sed -i \"s/xlt('Create New Patient')/xlt('Check and create')/; "
                f"s/xla('Create New Patient')/xla('Check and create')/; "
                f"s/xlj('Create New Patient')/xlj('Check and create')/\" {FORM}"
            )
            changed["labels"] = {
                "Confirm Create New Patient": "Save new patient",
                "Create New Patient": "Check and create",
            }
        elif kind == "reorder":
            sql = (
                "UPDATE openemr.layout_options SET seq=85 WHERE form_id='DEM' AND field_id='sex';"
                "UPDATE openemr.layout_options SET seq=0 WHERE form_id='DEM' AND field_id='phone_home';"
                "UPDATE openemr.layout_options SET seq=0 WHERE form_id='DEM' AND field_id='email';"
            )
            self.sql(sql)
            changed["sql"] = sql
        else:
            raise ValueError(f"unknown UI change {kind}")
        return changed

    def clear_all_events(self) -> None:
        self.drift_on = []
        try:
            self.app_exec(
                f"for f in {FORM} {POPUP}; do [ -f $f.oa-orig ] && cp -p $f.oa-orig $f; done; "
                f"rm -f {DRIFT_JS}; true"
            )
        except (RuntimeError, subprocess.SubprocessError):
            pass
        try:
            self.clear_item_events()
            self.sql(
                "UPDATE openemr.layout_options SET seq=100 WHERE form_id='DEM' AND field_id='sex';"
                "UPDATE openemr.layout_options SET seq=11 WHERE form_id='DEM' AND field_id='phone_home';"
                "UPDATE openemr.layout_options SET seq=14 WHERE form_id='DEM' AND field_id='email';"
            )
        except (RuntimeError, subprocess.SubprocessError):
            pass


def make_app(name: str) -> Any:
    if name == "fake":
        from .fake import FakeApp

        return FakeApp()
    if name == "openemr":
        app = OpenEMRApp()
        app.point_hooks_here()
        return app
    raise ValueError(f"unknown app {name}")


def main() -> int:
    """Bring the lifecycle OpenEMR copy up (and set it up on first use)."""

    import argparse

    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("command", choices=("up", "status", "down"))
    args = p.parse_args()
    app = OpenEMRApp()
    if args.command == "up":
        t = time.monotonic()
        how = app.bring_up()
        print(json.dumps({"how": how, "seconds": round(time.monotonic() - t, 1)}))
    elif args.command == "status":
        print(json.dumps({"bootstrapped": app.bootstrapped(), "ui": app.ui_url}))
    else:
        app.fx._compose("stop", timeout_s=300.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
