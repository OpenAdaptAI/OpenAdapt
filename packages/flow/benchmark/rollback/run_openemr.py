"""Live compensating rollback on the local OpenEMR fixture.

Needs the shared fixture lease (``~/oa/scratch/fixture-lease``) and
``DOCKER_HOST`` pointing at the podman socket the fixtures use. Runs one
pinned OpenEMR stack of its own (ports 9560/9561, compose project
``openadapt-openemr-rollback``), cloned from an existing fixture state, and
one headless Chromium at a time.

The workflow is the scoreboard's compiled New Patient referral (it starts on
the signed-in blank form, ``scoreboard/tasks/openemr.py``). The rollback
reads the whole ``openemr`` database through a SELECT-only user
(``rollback_readonly_db``), skipping the reviewed allow-list of tables
OpenEMR writes on its own. Cases, each made to stop by one added step whose
screen check never passes:

- ``before_save``: the form is filled, nothing is saved;
- ``after_save``: the chart is saved and the bundle declares no
  compensation (OpenEMR, as configured, offers no way to delete a chart).
  In production that save can't be undone, so it runs only after a person
  approves it; the harness plays that approval and shows what the person gets
  when the run then stops: the exact rows to repair.

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
import secrets
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

UI_PORT, API_PORT = "9561", "9560"
PROJECT = "openadapt-openemr-rollback"
SAVE_STEP = "step_034"
BEFORE_SAVE_STEP = "step_033"
#: Tables OpenEMR writes on its own for any signed-in user. Reviewed on the
#: lifecycle proof (measure/phase0-independent, verify.database.yaml).
ALLOW_TABLES = [
    "log",
    "log_comment_encrypt",
    "api_log",
    "sessions",
    "users_secure",
    "uuid_registry",
    "uuid_mapping",
    "sequences",
    "recent_patients",
    "user_settings",
    "clinical_rules_log",
]
RO_USER = "rollback_ro"


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
    workflow.undo_steps = []
    workflow.save(out)
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
    workflow.undo_steps = []
    workflow.save(out)
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
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    root = args.root
    _clone_state(args.from_state, root)
    fx = _fixture(root)
    rows: dict[str, Any] = {}
    started = time.monotonic()
    fx._compose("up", "-d", "--pull", "never", timeout_s=600.0)
    try:
        fx.reset()
        password = secrets.token_urlsafe(24)
        fx._db(
            f"CREATE USER IF NOT EXISTS '{RO_USER}'@'%' IDENTIFIED BY '{password}'; "
            f"ALTER USER '{RO_USER}'@'%' IDENTIFIED BY '{password}'; "
            f"GRANT SELECT ON openemr.* TO '{RO_USER}'@'%'; FLUSH PRIVILEGES;"
        )
        os.environ["ROLLBACK_RO_PASSWORD"] = password
        container = fx._compose("ps", "-q", "db").stdout.decode().strip()
        read = {
            "sql_driver": "rollback_readonly_db",
            "sql_connect_args": {
                "container": container,
                "user": RO_USER,
                "database": "openemr",
            },
            "sql_password_env": "ROLLBACK_RO_PASSWORD",
        }
        import rollback_readonly_db as ro

        def connect() -> Any:
            return ro.connect(container, RO_USER, password, "openemr")

        cases = {
            "before_save": lambda b, o: _variant(b, o, before=BEFORE_SAVE_STEP),
            "after_save": _after_save,
        }
        with tempfile.TemporaryDirectory(prefix="rollback-openemr-") as tmp:
            work = Path(tmp)
            for n, (name, make) in enumerate(cases.items()):
                fx.reset()
                values = task.item_values(900 + n)
                bundle = make(args.bundle, work / f"bundle-{name}")
                workflow = Workflow.load(bundle)
                verifier = SqlRecordVerifier(
                    connect, "SELECT * FROM patient_data", timeout_s=20.0
                )
                plan = RollbackPlan(
                    DatabaseReader(
                        dialect="mysql", read_config=read, allow_tables=ALLOW_TABLES
                    ),
                    read_records=records_reader(verifier),
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
                rows[name] = {
                    "stopped_at": next(
                        (r.step_id for r in report.results if not r.ok), None
                    ),
                    "steps_run": len(report.results),
                    "transaction_outcome": report.transaction_outcome,
                    "rollback_status": rollback.status if rollback else None,
                    "rollback_reason": rollback.reason if rollback else None,
                    "after_stop_diff": rollback.after_stop_diff if rollback else None,
                    "remaining_diff": rollback.remaining_diff if rollback else None,
                    "steps": [
                        {"step_id": s.step_id, "decision": s.decision}
                        for s in (rollback.steps if rollback else [])
                    ],
                    "diff_file_written": bool(rollback and rollback.diff_path),
                    "model_calls": report.model_calls,
                    "seconds": seconds,
                }
                print(name, json.dumps(rows[name])[:600], flush=True)
        fx.reset()
    finally:
        if not args.keep_up:
            fx._compose("down", "-v", timeout_s=300.0)
    args.out.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": "openadapt.rollback-measurement/v1",
        "app": "openemr",
        "mode": "live (pinned OpenEMR fixture, real browser, whole-database read)",
        "bundle": "scoreboard openemr_referral program bundle (New Patient)",
        "allow_tables": ALLOW_TABLES,
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
