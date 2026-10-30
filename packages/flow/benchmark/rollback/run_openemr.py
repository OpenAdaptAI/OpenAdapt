"""Live compensating rollback on the local OpenEMR fixture.

Needs the shared fixture lease (``~/oa/scratch/fixture-lease``) and
``DOCKER_HOST`` pointing at the podman socket the fixtures use. Runs one
pinned OpenEMR stack of its own (ports 9560/9561, compose project
``openadapt-openemr-rollback``), cloned from an existing fixture state, and
one headless Chromium at a time.

The workflow is the scoreboard's compiled New Patient referral (it starts on
the signed-in blank form, ``scoreboard/tasks/openemr.py``). The rollback
reads the whole ``openemr`` database in one read-only transaction through one
``docker exec`` session (MariaDB root, as the RL grader reads it;
``rollback_readonly_db``). The run is made to stop before the save by one
added step whose screen check never passes, so the form is filled and nothing
is saved. Two reads of that stop:

- ``before_save_no_profile``: every changed row counts as the run's;
- ``before_save_app_profile``: OpenEMR's shared bookkeeping profile
  (``openadapt_flow.app_profiles``, ``openemr``; the same rules the verifier's
  frame and the RL grader use) is applied by the read itself.

Synthetic data only (example.invalid, 555-01xx); no model calls.

    source ~/oa/scratch/independent-verification/env.sh   # DOCKER_HOST only
    cd packages/flow && PYTHONPATH=../types:. python benchmark/rollback/run_openemr.py \\
        --bundle <scoreboard program bundle> --from-state <fixture>/state \\
        --out benchmark/rollback/results
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
for path in (str(HERE), str(FLOW)):
    if path not in sys.path:
        sys.path.insert(0, path)
# Another checkout's ``benchmark`` package can be installed on the path; this
# harness imports this repository's.
_pkg = types.ModuleType("benchmark")
_pkg.__path__ = [str(FLOW / "benchmark")]
sys.modules["benchmark"] = _pkg

#: OpenEMR's audit log as attribution reads it (docs/VERIFY_RUN.md, Records
#: outside the target): the table and the write kind derived from the log.
AUDIT_QUERY = (
    "SELECT l.id, l.user, l.patient_id, SUBSTRING_INDEX(l.event, '-', -1) AS kind, "
    "CASE WHEN IFNULL(e.encrypt, 'No') = 'Yes' THEN NULL ELSE "
    "LOWER(REPLACE(REGEXP_SUBSTR(l.comments, "
    "'(?i)(?<=insert into |update |delete from |replace into )[`a-z0-9_.]+'), "
    "'`', '')) END AS tbl "
    "FROM log l LEFT JOIN log_comment_encrypt e ON e.log_id = l.id"
)

UI_PORT, API_PORT = "9561", "9560"
PROJECT = "openadapt-openemr-rollback"
SAVE_STEP = "step_034"
BEFORE_SAVE_STEP = "step_033"


def _clone_state(source: Path, root: Path) -> Path:
    """Copy a fixture state (baseline, runtime env, lock) onto our ports."""
    state = root / "state"
    if state.exists():
        return state
    state.mkdir(parents=True, mode=0o700)
    for name in ("baseline.sql", "baseline.sql.sha256", "oauth-clients.json"):
        shutil.copy2(source / name, state / name)
    env = (source / "runtime.env").read_text()
    env = re.sub(r"(?m)^OPENEMR_HTTP_PORT=.*$", f"OPENEMR_HTTP_PORT={UI_PORT}", env)
    env = re.sub(r"(?m)^OPENEMR_HTTPS_PORT=.*$", f"OPENEMR_HTTPS_PORT={API_PORT}", env)
    target = state / "runtime.env"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(env)
    lock = json.loads((source.parent / "environment.lock.json").read_text())
    lock["project"] = PROJECT
    (root / "environment.lock.json").write_text(json.dumps(lock, indent=2))
    return state


def _fixture(root: Path) -> Any:
    from benchmark.openemr_local import fixture as fx

    fx.LOCK_PATH = root / "environment.lock.json"

    class PortFixture(fx.OpenEMRFixture):
        @property
        def ui_base_url(self) -> str:
            return f"http://127.0.0.1:{UI_PORT}"

        @property
        def api_base_url(self) -> str:
            return f"https://127.0.0.1:{API_PORT}"

        def _validate_runtime_env(self) -> None:
            v = self._runtime_values()
            assert v.get("OPENEMR_HTTP_PORT") == UI_PORT
            assert v.get("OPENEMR_HTTPS_PORT") == API_PORT
            self.runtime_env.chmod(0o600)

    return PortFixture(root / "state")


def _plain_bundle(out: Path, workflow: Any) -> None:
    """Save a harness-edited copy as an ordinary bundle.

    The source is the scoreboard's agent-compiled program; the copy here adds
    one step that stops the run on a test copy. It is not an agent draft, so
    the JIT admission gate (what the scoreboard tests) isn't in play: the
    draft markers and origin are dropped. The rollback is what's measured.
    """
    from openadapt_flow.jit.lifecycle import ADMISSION_FILE, DRAFT_MARKER

    workflow.origin = None
    workflow.undo_steps = []
    workflow.save(out)
    for name in (DRAFT_MARKER, ADMISSION_FILE):
        (out / name).unlink(missing_ok=True)


def _variant(bundle: Path, out: Path, *, before: str) -> Path:
    from openadapt_flow.ir import Step, Workflow

    shutil.copytree(bundle, out)
    workflow = Workflow.load(out)
    index = next(i for i, s in enumerate(workflow.steps) if s.id == before)
    workflow.steps.insert(
        index,
        Step.model_validate(
            {
                "id": "stop_here",
                "intent": "a later check that never passes (made to fail)",
                "action": "key",
                "key": "Escape",
                "expect": [
                    {
                        "kind": "text_present",
                        "text": "Chart signed and locked",
                        "timeout_s": 2.0,
                    }
                ],
            }
        ),
    )
    _plain_bundle(out, workflow)
    return out


def _after_save(bundle: Path, out: Path) -> Path:
    from openadapt_flow.ir import Step, Workflow

    shutil.copytree(bundle, out)
    workflow = Workflow.load(out)
    workflow.steps.append(
        Step.model_validate(
            {
                "id": "stop_here",
                "intent": "a later check that never passes (made to fail)",
                "action": "key",
                "key": "Escape",
                "expect": [
                    {
                        "kind": "text_present",
                        "text": "Chart signed and locked",
                        "timeout_s": 2.0,
                    }
                ],
            }
        )
    )
    _plain_bundle(out, workflow)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--from-state", required=True, type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / "oa" / "scratch" / "compensating-rollback" / "openemr",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--keep-up", action="store_true")
    parser.add_argument(
        "--profile",
        default="openemr",
        help="The app's bookkeeping profile (openadapt_flow.app_profiles)",
    )
    args = parser.parse_args(argv)
    if not os.environ.get("DOCKER_HOST"):
        raise SystemExit("set DOCKER_HOST to the fixtures' podman socket first")

    from playwright.sync_api import sync_playwright

    from benchmark.scoreboard.tasks import openemr as task
    from openadapt_flow._browser_setup import launch_chromium
    from openadapt_flow.after_stop import records_reader
    from openadapt_flow.backends.factory import build_backend
    from openadapt_flow.deployment import BackendConfig, build_replayer
    from openadapt_flow.ir import Workflow
    from openadapt_flow.rollback import ReplayerRunner, RollbackPlan
    from openadapt_flow.rollback_state import DatabaseReader
    from openadapt_flow.runtime.effects.frame import FrameAttribution
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    root = args.root
    _clone_state(args.from_state, root)
    fx = _fixture(root)
    rows: dict[str, Any] = {}
    started = time.monotonic()
    fx._compose("up", "-d", "--pull", "never", timeout_s=600.0)
    try:
        # A fresh stack installs OpenEMR on its first start; let that finish
        # before the baseline replaces the database.
        fx._wait_ready(timeout_s=1200.0)
        # The baseline's encryption ``keys`` rows belong to the stack it was
        # taken on; this stack's own install made new ones, which its files
        # match. Keep them and put them back after every reset (as the
        # independent-verification harness does), or every page fails.
        keys_sql = (
            b"USE openemr;\n"
            + fx._compose(
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot openemr keys',
                timeout_s=120.0,
            ).stdout
        )

        def reset() -> None:
            fx.reset()
            fx._db(input_bytes=keys_sql)

        reset()
        container = fx._compose("ps", "-q", "db").stdout.decode().strip()
        import rollback_readonly_db as ro

        def session() -> Any:
            return ro.connect(container, "openemr")

        def audit_rows() -> Any:
            """The audit log through its own one-statement read; None when it
            can't be read (attribution then says nothing)."""
            try:
                cur = one_shot().cursor()
                cur.execute(AUDIT_QUERY)
                names = [d[0] for d in cur.description or []]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except Exception:  # noqa: BLE001 - an unreadable log decides nothing
                return None

        def one_shot() -> Any:
            return ro.OneShotConnection(container, "openemr")

        def before_save(b: Path, o: Path) -> Path:
            return _variant(b, o, before=BEFORE_SAVE_STEP)

        profile = args.profile or None
        # (bundle maker, app profile): every change counts, or the app's own
        # versioned bookkeeping profile (shared with the verifier and the RL
        # grader) excuses what it names.
        cases = {
            "before_save_no_profile": (before_save, None),
            "before_save_app_profile": (before_save, profile),
        }
        if profile is None:
            cases.pop("before_save_app_profile")
        with tempfile.TemporaryDirectory(prefix="rollback-openemr-") as tmp:
            work = Path(tmp)
            for name, (make, case_profile) in cases.items():
                reset()
                # The recording's own synthetic values: the program's screen
                # checks quote them (step_007 checks the recorded last name).
                values = task.item_values(0)
                bundle = make(args.bundle, work / f"bundle-{name}")
                workflow = Workflow.load(bundle)
                verifier = SqlRecordVerifier(
                    one_shot, "SELECT * FROM patient_data", timeout_s=20.0
                )
                plan = RollbackPlan(
                    DatabaseReader(
                        dialect="mysql",
                        connect=session,
                        profile=case_profile,
                        # The browser signs in as the fixture's actor user:
                        # the profile's own-row rules are about that user.
                        actor_names=(
                            (fx._runtime_values()["OPENEMR_ACTOR_USER"],)
                            if case_profile
                            else ()
                        ),
                    ),
                    read_records=records_reader(verifier),
                    # With the profile, the realistic attribution: OpenEMR's
                    # own audit log, the acting user as the run.
                    attribution=(
                        FrameAttribution(
                            kind="audit_read",
                            actor_field="user",
                            self=[fx._runtime_values()["OPENEMR_ACTOR_USER"]],
                            record_field="patient_id",
                            record_key="pid",
                            event_field="kind",
                            write_events=["insert", "update", "delete", "replace"],
                            insert_events=["insert", "replace"],
                            delete_events=["delete"],
                            table_field="tbl",
                            read={"kind": "sql"},
                        )
                        if case_profile
                        else None
                    ),
                    audit_read=audit_rows if case_profile else None,
                )
                with sync_playwright() as p:
                    browser = launch_chromium(p.chromium, headless=True)
                    try:
                        context = browser.new_context(
                            viewport={"width": 1280, "height": 800}
                        )
                        page = context.new_page()
                        task._demo()._preauthenticate_browser(task._PageOnly(page), fx)
                        backend = build_backend(
                            BackendConfig(kind="web", url=fx.ui_base_url), page=page
                        )

                        def make_replayer() -> Any:
                            return build_replayer(
                                backend,
                                allow_egress=False,
                                effect_verifier=verifier,
                                api_actuator=None,
                                durable=False,
                                use_structural=True,
                            )

                        t0 = time.monotonic()
                        before = plan.arm()
                        run_dir = work / f"run-{name}"
                        report = make_replayer().run(
                            workflow,
                            params=values,
                            bundle_dir=bundle,
                            run_dir=run_dir,
                        )
                        report = plan.finish(
                            report,
                            workflow,
                            before=before,
                            run_dir=run_dir,
                            bundle_dir=bundle,
                            runner=ReplayerRunner(make_replayer, None),
                        )
                        seconds = round(time.monotonic() - t0, 1)
                    finally:
                        browser.close()
                rollback = report.rollback
                failed = next((r for r in report.results if not r.ok), None)
                keep = root / "runs" / name
                if keep.exists():
                    shutil.rmtree(keep)
                shutil.copytree(run_dir, keep)
                rows[name] = {
                    "stopped_at": failed.step_id if failed else None,
                    "stop_error": (failed.error or "")[:400] if failed else None,
                    "steps_run": len(report.results),
                    "transaction_outcome": report.transaction_outcome,
                    "rollback_status": rollback.status if rollback else None,
                    "rollback_reason": rollback.reason if rollback else None,
                    "after_stop_diff": rollback.after_stop_diff if rollback else None,
                    "remaining_diff": rollback.remaining_diff if rollback else None,
                    "profile_id": rollback.profile_id if rollback else None,
                    "cross_checked": rollback.cross_checked if rollback else None,
                    "steps": [
                        {"step_id": s.step_id, "decision": s.decision}
                        for s in (rollback.steps if rollback else [])
                    ],
                    "diff_file_written": bool(rollback and rollback.diff_path),
                    "model_calls": report.model_calls,
                    "seconds": seconds,
                }
                print(name, json.dumps(rows[name])[:600], flush=True)
        reset()
    finally:
        if not args.keep_up:
            fx._compose("down", "-v", timeout_s=300.0)
    args.out.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": "openadapt.rollback-measurement/v1",
        "app": "openemr",
        "mode": "live (pinned OpenEMR fixture, real browser, whole-database read)",
        "bundle": "scoreboard openemr_referral program bundle (New Patient)",
        "profile": profile,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.platform(),
        "load_1m": round(os.getloadavg()[0], 2),
        "seconds": round(time.monotonic() - started, 1),
        "rows": rows,
    }
    (args.out / "openemr.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
