"""Worklist throughput on a local OpenEMR test copy (synthetic data only).

    python -m benchmark.openemr_throughput.harness run \\
        --bundle BUNDLE --fixture-state STATE --tag TAG --workers N --items M \\
        [--window K] [--halt-at I] [--profile]

What one run does:

1. Setup, not timed: reset the fixture database to its verified baseline,
   add 8 synthetic clerk accounts (``oa_clerk1`` .. ``oa_clerk8``), seed the
   existing chart Rosa Delgado, and generate M distinct made-up referrals.
2. Timed: :func:`openadapt_flow.runtime.worklist_pool.run_worklist_pool` over
   the M referrals on N workers. Each worker opens its own browser context
   and signs in as its own clerk (counted in the time). For each item it
   claims, it closes any open patient chart, opens the blank New Patient
   form, and replays the compiled bundle with the engine's own read-back of
   the saved chart through the OpenEMR REST API.
3. Judge, not timed: the test setup reads every item back from the database
   with SQL (12 of 12 fields, exactly one new chart, none for unrun items).

``--halt-at I`` replaces item I with a possible duplicate of the seeded
chart; the engine must halt on it and the pool must stop every worker.
``--profile`` (N = 1 only) records an exclusive-time profile per item.

The fixture is :mod:`benchmark.openemr_local.fixture` (pinned OpenEMR 8.0.0.3
images) with its own compose project and ports, set by environment:
``THROUGHPUT_UI_PORT`` (HTTP) and ``THROUGHPUT_API_PORT`` (HTTPS). The bundle
is the referral program compiled with DOM identity; it is not in this
repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]

#: The existing chart a possible-duplicate referral collides with.
EXISTING_ROSA = dict(
    title="Mrs.", fname="Rosa", lname="Delgado", dob="1958-03-27", sex="Female",
    street="311 BIRCH COURT", city="EXAMPLETON", state="MA", state_label="Massachusetts",
    postal_code="02139", phone_home="555-0119", email="r.delgado.prior@example.invalid",
)
#: Same name, different date of birth: OpenEMR offers the existing chart and
#: the engine must stop (the r6 case of the referral timing benchmark).
POSSIBLE_DUPLICATE = dict(
    title="Mrs.", fname="Rosa", lname="Delgado", dob="1958-03-22", sex="Female",
    street="7 MILL ROAD", city="EXAMPLETON", state="MA", state_label="Massachusetts",
    postal_code="02139", phone_home="555-0188", email="rosa.delgado@example.invalid",
)

# First names are 4+ letters: the identity check refuses to bind a parameter
# value shorter than 4 characters (see docs/LIMITS.md).
FIRST = {
    "Female": ["Adela", "Beatrix", "Clarice", "Dorinda", "Edwina", "Florence", "Gisela",
               "Harriet", "Isadora", "Juniper", "Katrina", "Lorena", "Marisol", "Nerissa",
               "Ophelia", "Perpetua"],
    "Male": ["Ambrose", "Barnaby", "Casimir", "Desmond", "Emrys", "Fitzroy", "Gideon",
             "Horatio", "Ignatius", "Jasper", "Leopold", "Lucian", "Montague", "Nathaniel",
             "Oswin", "Peregrine"],
}
LAST = ["Quillfern", "Rowanby", "Sedgewick", "Thornvale", "Umberly", "Vantree", "Wexmoor",
        "Yarrowby"]
STREETS = ["LANTERN COURT", "SYNTHETIC WAY", "EXAMPLE ROAD", "PLACEHOLDER LANE"]
CLERKS = [f"oa_clerk{i}" for i in range(1, 9)]


def synthetic_referrals(n: int) -> list[dict[str, str]]:
    """n distinct made-up patients (555-01xx phones, example.invalid emails)."""
    out = []
    for i in range(n):
        sex = "Female" if i % 2 == 0 else "Male"
        fname = FIRST[sex][(i // 2) % 16]
        last = LAST[(i // 32) % 8] + ("" if i < 256 else str(i // 256))
        out.append(dict(
            title="Ms." if sex == "Female" else "Mr.", fname=fname,
            lname=f"{last}{chr(65 + (i % 26))}",
            dob=f"19{50 + (i % 45):02d}-{1 + (i % 12):02d}-{1 + (i % 27):02d}", sex=sex,
            street=f"{10 + i} {STREETS[i % 4]}", city="EXAMPLETON", state="MA",
            state_label="Massachusetts", postal_code="02139",
            phone_home=f"555-01{i % 100:02d}",
            email=f"{fname.lower()}.{last.lower()}.{i}@example.invalid"))
    return out


# -- fixture ---------------------------------------------------------------


def make_fixture(state: Path) -> Any:
    from benchmark.openemr_local import fixture as fx

    ui_port = os.environ.get("THROUGHPUT_UI_PORT", "9911")
    api_port = os.environ.get("THROUGHPUT_API_PORT", "9910")
    fx.LOCK_PATH = state.parent / "environment.lock.json"

    class ThroughputFixture(fx.OpenEMRFixture):
        """Same pinned images, compose file and baseline; own project and ports."""

        @property
        def ui_base_url(self) -> str:
            return f"http://127.0.0.1:{ui_port}"

        @property
        def api_base_url(self) -> str:
            return f"https://127.0.0.1:{api_port}"

        def _validate_runtime_env(self) -> None:
            v = self._runtime_values()
            assert v.get("OPENEMR_HTTP_PORT") == ui_port
            assert v.get("OPENEMR_HTTPS_PORT") == api_port
            assert v.get("OPENEMR_IMAGE") == self.lock["services"]["openemr"]
            assert v.get("MARIADB_IMAGE") == self.lock["services"]["mariadb"]
            self.runtime_env.chmod(0o600)

    return ThroughputFixture(state)


def add_clerks(fixture: Any, n: int = 8) -> list[str]:
    """Clone the fixture's actor user into n clerks (own username and ACL row)."""
    stmts = ["USE openemr"]
    for i, name in enumerate(CLERKS[:n], 1):
        aro = 100 + i
        stmts += [
            "DROP TEMPORARY TABLE IF EXISTS t_u",
            "CREATE TEMPORARY TABLE t_u SELECT * FROM users WHERE username='openadapt_actor'",
            f"UPDATE t_u SET id=0, uuid=NULL, username='{name}', fname='Clerk', lname='{i}'",
            "INSERT INTO users SELECT * FROM t_u",
            f"SET @uid=(SELECT id FROM users WHERE username='{name}')",
            "INSERT INTO users_secure (id,username,password,last_update_password) "
            f"SELECT @uid,'{name}',password,last_update_password FROM users_secure "
            "WHERE username='openadapt_actor'",
            "INSERT INTO gacl_aro (id,section_value,value,order_value,name,hidden) "
            f"VALUES ({aro},'users','{name}',10,'Clerk {i}',0)",
            f"INSERT INTO gacl_groups_aro_map (group_id,aro_id) VALUES (11,{aro})",
            f"INSERT INTO `groups` (name,user) VALUES ('Default','{name}')",
        ]
    stmts.append(f"UPDATE gacl_aro_seq SET id={100 + n}")
    fixture._db(";\n".join(stmts) + ";")
    count = fixture._db(
        "SELECT COUNT(*) FROM openemr.users WHERE username LIKE 'oa_clerk%'"
    ).decode().strip()
    if count != str(n):
        raise RuntimeError(f"expected {n} clerks, found {count}")
    return CLERKS[:n]


SQL_FIELDS = ("id", "title", "fname", "lname", "DOB", "sex", "street", "city", "state",
              "postal_code", "phone_home", "email", "country_code")


def sql_patients(fixture: Any) -> list[dict[str, str]]:
    out = fixture._db(
        "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id"
    ).decode()
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
    ]


def container_started(fixture: Any) -> str:
    import subprocess

    project = fixture.lock["project"]
    names = [f"{project}-openemr-1", f"{project}-db-1"]
    r = subprocess.run(
        ["podman", "inspect", "-f", "{{.Name}}={{.State.StartedAt}}/{{.State.Running}}", *names],
        capture_output=True, text=True,
    )
    return r.stdout.strip().replace("\n", "|")


# -- load ------------------------------------------------------------------


def wait_quiet(threshold: float, max_wait_s: float, sustain: int = 4) -> dict[str, Any]:
    """Start only once the 1-minute load average stays below ``threshold``."""
    t0 = time.time()
    ok = 0
    while True:
        one = os.getloadavg()[0]
        ok = ok + 1 if one < threshold else 0
        if ok >= sustain or time.time() - t0 > max_wait_s:
            return {"threshold": threshold, "waited_s": round(time.time() - t0, 1),
                    "gave_up": ok < sustain, "load_at_start": os.getloadavg()}
        time.sleep(5.0)


class LoadSampler:
    def __init__(self, every_s: float = 2.0) -> None:
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, args=(every_s,), daemon=True)
        self._t.start()

    def _loop(self, every_s: float) -> None:
        while not self._stop.is_set():
            self.samples.append(os.getloadavg()[0])
            self._stop.wait(every_s)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._t.join(timeout=5)
        s = self.samples
        return {"load1_mean": round(sum(s) / len(s), 2) if s else None,
                "load1_max": round(max(s), 2) if s else None, "ncpu": os.cpu_count()}


# -- the run ---------------------------------------------------------------


def _demo_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "openemr_local_demo", FLOW / "scripts" / "openemr_local_demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    sys.modules["openemr_local_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


def run(a: argparse.Namespace) -> dict[str, Any]:
    from openadapt_flow._browser_setup import ensure_chromium_installed
    from openadapt_flow.benchmark.openemr_local import (
        OpenEMRPatientOracle,
        SyntheticPatientSpec,
    )
    from openadapt_flow.cli_worklist_pool import WebPoolWorker
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.worklist_pool import PoolPolicy, run_worklist_pool

    if a.profile and a.workers != 1:
        raise SystemExit("--profile needs --workers 1")
    if a.page_quiet_ms is not None:
        from openadapt_flow.vision import settle as settle_mod

        settle_mod.PAGE_QUIET_WINDOW_S = a.page_quiet_ms / 1000.0
    os.environ.setdefault("OPENADAPT_FLOW_BROWSER", "bundled")
    demo = _demo_module()
    out = Path(a.out) / a.tag
    out.mkdir(parents=True, exist_ok=False)
    fixture = make_fixture(Path(a.fixture_state))
    t = time.time()
    digest = fixture.reset()
    clerks = add_clerks(fixture)
    seed = fixture.token_session("actor").post(
        fixture.api_base_url + "/apis/default/api/patient",
        json=dict(SyntheticPatientSpec(**EXISTING_ROSA).fields),
    ).status_code
    rows = synthetic_referrals(a.items)
    if a.halt_at is not None:
        rows[a.halt_at] = dict(POSSIBLE_DUPLICATE)
    before_ids = {r["id"] for r in sql_patients(fixture)}
    bundle = Path(a.bundle)
    workflow = Workflow.load(bundle)
    engine_sha = os.popen(f"git -C {FLOW} rev-parse HEAD").read().strip()
    meta = {
        "tag": a.tag, "workers": a.workers, "items": a.items, "window": a.window,
        "halt_at": a.halt_at, "profile": a.profile, "engine_sha": engine_sha,
        "page_quiet_ms": a.page_quiet_ms or 100,
        "engine_dirty": bool(os.popen(f"git -C {FLOW} status --porcelain -- openadapt_flow").read().strip()),
        "bundle_digest": getattr(workflow.manifest, "content_digest", None),
        "baseline": digest[:12], "seed_status": seed, "setup_s": round(time.time() - t, 2),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ensure_chromium_installed()
    importlib.import_module("openadapt_flow.vision.ocr")._get_engine()  # load the OCR model before the clock, as a warm worker would
    values = fixture._runtime_values()

    class _Clerk:
        def __init__(self, user: str) -> None:
            self.user = user
            self.ui_base_url = fixture.ui_base_url

        def _runtime_values(self) -> dict[str, str]:
            return {**values, "OPENEMR_ACTOR_USER": self.user}

    class _PageOnly:
        def __init__(self, page: Any) -> None:
            self.page = page

    def open_form(page: Any) -> None:
        """Start each item where the recording started: no chart open, blank form."""
        if page.evaluate(
            "() => !!(window.app_view_model && app_view_model.application_data.patient())"
        ):
            # The chart's own close button (OpenEMR clearPatient), no finder.
            with page.expect_response(lambda r: "unset_session_ajax.php" in r.url, timeout=30_000):
                page.evaluate("() => clearPatient(false)")
            page.wait_for_function("() => !app_view_model.application_data.patient()", timeout=30_000)
        frame = page.frame(name=demo.FORM_FRAME_NAME)
        if frame is not None and demo.FORM_PATH in frame.url:
            try:
                if frame.locator("#form_fname").input_value(timeout=2000) == "":
                    return  # sign-in left the blank form open
            except Exception:  # noqa: BLE001 - fall through and reload it
                pass
        page.evaluate(
            "({path, name}) => { window.navigateTab(path, name); window.activateTabByName(name, true); }",
            {"path": demo.FORM_PATH, "name": demo.FORM_FRAME_NAME},
        )
        frame = demo._form_frame(page)
        frame.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        if frame.locator("#form_fname").input_value() != "":
            raise RuntimeError("the New Patient form was not blank")

    sessions: dict[str, Any] = {}

    def run_item(user: str):
        def _run(page: Any, idx: int, row: dict[str, Any], item_dir: Path, pipeline: Any) -> Any:
            spec = SyntheticPatientSpec(**row)
            if user not in sessions:
                sessions[user] = fixture.token_session("oracle")
            oracle = OpenEMRPatientOracle(fixture.api_base_url, sessions[user], spec)
            backend = demo._BasePlaywrightBackend(page)
            prof = None
            if a.profile:
                from benchmark.openemr_throughput.profiler import RunProfiler

                prof = RunProfiler()
                prof.install(backend)
            try:
                with (prof.span("task") if prof else _null()):
                    rep = Replayer(
                        backend, effect_verifier=oracle.verifier, readback_pipeline=pipeline
                    ).run(workflow, params=spec.params(), bundle_dir=bundle, run_dir=item_dir)
            finally:
                if prof:
                    prof.uninstall()
            if prof:
                (item_dir / "profile.json").write_text(json.dumps(
                    {"buckets": prof.buckets(), "calls": prof.summary(),
                     "parents": prof.parents(), "steps": prof.step_buckets()}, indent=1))
            if not a.keep_pngs:
                for png in (item_dir / "steps").glob("*.png"):
                    png.unlink()
            return rep
        return _run

    workers = [
        WebPoolWorker(
            name=user, entry_url=fixture.ui_base_url, run_item_on_page=run_item(user),
            sign_in=(lambda page, user=user: demo._preauthenticate_browser(_PageOnly(page), _Clerk(user))),
            open_entry=open_form, readback_window=a.window,
        )
        for user in clerks[: a.workers]
    ]
    policy = PoolPolicy(workers=a.workers, readback_window=a.window,
                        items_independent=True, max_workers=8)
    free_gb = shutil.disk_usage(str(out)).free / 1e9
    if free_gb < 8:
        raise SystemExit(f"disk below 8 GB ({free_gb:.1f} GB free)")
    gate = wait_quiet(a.gate_load, a.gate_max_wait_s)
    sampler = LoadSampler()
    c0 = container_started(fixture)
    t0 = time.time()
    report = run_worklist_pool(
        rows, workers, run_dir=out / "pool", policy=policy,
        on_event=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True),
    )
    wall = time.time() - t0
    load = sampler.stop()
    c1 = container_started(fixture)

    # Judge (not timed): the test setup's own SQL read-back of every item.
    new_rows = [r for r in sql_patients(fixture) if r["id"] not in before_ids]
    items = []
    for item in report.items:
        spec = SyntheticPatientSpec(**rows[item["idx"]])
        mine = [r for r in new_rows if r["lname"] == spec.lname and r["email"] == spec.email]
        fields = dict(spec.fields)
        match = sum(1 for k, v in fields.items() if len(mine) == 1 and mine[0].get(k) == v)
        duplicate_case = a.halt_at is not None and item["idx"] == a.halt_at
        res = item.get("result") or {}
        items.append({
            "idx": item["idx"], "state": item["state"], "worker": item["worker"],
            "new_charts": len(mine), "readback": f"{match}/{len(fields)}",
            "correct": (len(mine) == 0) if duplicate_case else (len(mine) == 1 and match == len(fields)),
            "duplicate_case": duplicate_case, "wall_s": res.get("wall_s"),
            "readback_mode": res.get("readback"),
            "claimed_at": item["claimed_at"], "finished_at": item["finished_at"],
        })
    ran = [i for i in items if i["state"] != "pending"]
    saved = [i for i in ran if i["correct"] and i["new_charts"] == 1]
    summary = {
        **meta, "wall_s": round(wall, 3), "pool_wall_s": round(report.wall_s, 3),
        "sign_in_s": {w.name: round(w.open_s, 2) for w in report.per_worker},
        "counts": report.counts(), "stop": report.stop, "items_run": len(ran),
        "saved_correct": len(saved),
        "wrong_or_extra_charts": sum(1 for i in items if not i["correct"] and i["state"] != "pending")
        + sum(1 for i in items if i["state"] == "pending" and i["new_charts"]),
        "new_charts_total": len(new_rows),
        "entries_per_hour": round(len(saved) * 3600 / wall, 1),
        "written_after_failed_readback": report.written_after_failed_readback,
        "per_worker": [{"name": w.name, "items": len(w.items), "states": w.states,
                        "busy_s": round(w.busy_s, 1)} for w in report.per_worker],
        **load, "gate": gate, "valid": c0 == c1, "ended": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (out / "items.json").write_text(json.dumps(items, indent=1))
    return summary


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--fixture-state", required=True)
    r.add_argument("--out", required=True, help="evidence root; each run gets OUT/TAG")
    r.add_argument("--tag", required=True)
    r.add_argument("--workers", type=int, required=True)
    r.add_argument("--items", type=int, required=True)
    r.add_argument("--window", type=int, default=0)
    r.add_argument("--halt-at", type=int, default=None)
    r.add_argument("--profile", action="store_true")
    r.add_argument("--page-quiet-ms", type=int, default=None,
                   help="page-quiet settle window (runtime.page_quiet_ms); default 100")
    r.add_argument("--keep-pngs", action="store_true")
    r.add_argument("--gate-load", type=float, default=16.0)
    r.add_argument("--gate-max-wait-s", type=float, default=7200.0)
    r.add_argument("--results", default=str(HERE / "results" / "runs.jsonl"))
    a = ap.parse_args()
    summary = run(a)
    Path(a.results).parent.mkdir(parents=True, exist_ok=True)
    with open(a.results, "a") as fh:
        fh.write(json.dumps({k: v for k, v in summary.items()}, default=str) + "\n")
    print(json.dumps({k: summary[k] for k in (
        "tag", "workers", "window", "wall_s", "items_run", "saved_correct",
        "entries_per_hour", "counts", "stop", "load1_mean", "valid")}, default=str))


if __name__ == "__main__":
    main()
