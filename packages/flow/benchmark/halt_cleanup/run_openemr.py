"""Live halt cleanup on the local OpenEMR fixture.

Needs the shared fixture lease (``~/oa/scratch/fixture-lease``) and
``DOCKER_HOST`` pointing at the podman socket the fixtures use. Runs its own
pinned OpenEMR stack (ports 9560/9561, compose project
``openadapt-openemr-rollback``, cloned from an existing fixture state) and one
headless Chromium.

The workflow is the scoreboard's compiled New Patient referral; its first
anchored step (the Title control on the blank New Patient form) is the start
screen. Two cases, each the path a pool worker takes after a halted item
(``halt_cleanup.run_cleanup`` with the bundle's runnable cleanup: re-enter,
then check the start screen):

- ``dialog_open``: an item fills the form and clicks Create New Patient, so
  OpenEMR's duplicate-check confirmation dialog is open, and halts there.
  After the cleanup, the dialog must be gone, the form empty and the start
  screen confirmed; the next item's first steps then run on that session.
- ``wrong_screen``: the form's tab is left on the patient finder and
  re-entry is disabled, so the start screen can't come back. The session
  must be dirty.

Synthetic data only (example.invalid, 555-01xx); no model calls; nothing is
saved (the dialog's Confirm is never pressed).

    source ~/oa/scratch/independent-verification/env.sh   # DOCKER_HOST only
    cd packages/flow && PYTHONPATH=../types:. python benchmark/halt_cleanup/run_openemr.py \\
        --bundle <scoreboard program bundle> --from-state <fixture>/state \\
        --out benchmark/halt_cleanup/results
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
# Another checkout's ``benchmark`` package can be on the path; use this one.
_pkg = types.ModuleType("benchmark")
_pkg.__path__ = [str(FLOW / "benchmark")]
sys.modules["benchmark"] = _pkg

UI_PORT, API_PORT = "9561", "9560"
PROJECT = "openadapt-openemr-rollback"
FINDER_PATH = "/interface/main/finder/dynamic_finder.php"


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
    fd = os.open(state / "runtime.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def _plain(bundle: Path, out: Path) -> Path:
    """The scoreboard program as an ordinary bundle (not an agent draft)."""
    from openadapt_flow.ir import Workflow
    from openadapt_flow.jit.lifecycle import ADMISSION_FILE, DRAFT_MARKER

    shutil.copytree(bundle, out)
    workflow = Workflow.load(out)
    workflow.origin = None
    workflow.save(out)
    for name in (DRAFT_MARKER, ADMISSION_FILE):
        (out / name).unlink(missing_ok=True)
    return out


def _confirm_visible(page: Any) -> bool:
    for frame in page.frames:
        try:
            if frame.locator("#confirmCreate").is_visible():
                return True
        except Exception:  # noqa: BLE001 - a frame may detach while checking
            continue
    return False


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
    args = parser.parse_args(argv)
    if not os.environ.get("DOCKER_HOST"):
        raise SystemExit("set DOCKER_HOST to the fixtures' podman socket first")

    from playwright.sync_api import sync_playwright

    from benchmark.scoreboard.tasks import openemr as task
    from openadapt_flow._browser_setup import launch_chromium
    from openadapt_flow.backends.factory import build_backend
    from openadapt_flow.deployment import BackendConfig
    from openadapt_flow.halt_cleanup import run_cleanup, runnable_cleanup
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    demo = task._demo()
    _clone_state(args.from_state, args.root)
    fx = _fixture(args.root)
    rows: dict[str, Any] = {}
    started = time.monotonic()
    fx._compose("up", "-d", "--pull", "never", timeout_s=600.0)
    try:
        fx._wait_ready(timeout_s=1200.0)
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
        fx.reset()
        fx._db(input_bytes=keys_sql)
        with tempfile.TemporaryDirectory(prefix="halt-cleanup-openemr-") as tmp:
            work = Path(tmp)
            bundle = _plain(args.bundle, work / "bundle")
            workflow = Workflow.load(bundle)
            cleanup = runnable_cleanup(workflow)
            assert cleanup is not None
            values = task.item_values(0)
            with sync_playwright() as p:
                browser = launch_chromium(p.chromium, headless=True)
                try:
                    page = browser.new_context(
                        viewport={"width": 1280, "height": 800}
                    ).new_page()
                    demo._preauthenticate_browser(task._PageOnly(page), fx)

                    def replayer() -> Any:
                        return Replayer(
                            build_backend(
                                BackendConfig(kind="web", url=fx.ui_base_url), page=page
                            )
                        )

                    def open_form() -> None:
                        page.evaluate(
                            "({path, name}) => { window.navigateTab(path, name); "
                            "window.activateTabByName(name, true); }",
                            {"path": demo.FORM_PATH, "name": demo.FORM_FRAME_NAME},
                        )
                        demo._form_frame(page).locator("#form_fname").wait_for(
                            state="visible", timeout=60_000
                        )

                    # Sign-in left the blank form open in its tab. Loading
                    # main.php again doesn't bring the tab shell back (it
                    # needs sign-in's token), so re-entry reopens the form's
                    # tab, as OpenEMR's own menu does.
                    # -- dialog_open: a halted item leaves the confirmation up.
                    form = demo._form_frame(page)
                    form.locator("#form_fname").fill(values["fname"])
                    form.locator("#form_lname").fill(values["lname"])
                    form.locator("#form_DOB").fill(values["DOB"])
                    form.locator("#form_sex").select_option(label=values["sex"])
                    form.locator("#create").click()
                    demo._confirm_create_locator(page)
                    dialog_before = _confirm_visible(page)

                    t0 = time.monotonic()
                    reason = run_cleanup(
                        replayer,
                        workflow,
                        cleanup,
                        bundle_dir=bundle,
                        run_dir=work / "cleanup-dialog",
                        reenter=open_form,
                    )
                    cleanup_s = round(time.monotonic() - t0, 1)
                    form = demo._form_frame(page)
                    form_empty = form.locator("#form_fname").input_value() == ""
                    dialog_after = _confirm_visible(page)
                    # The next item's first steps on the same session.
                    first = workflow.model_copy(
                        update={
                            "steps": [
                                s.model_copy(deep=True) for s in workflow.steps[:7]
                            ],
                            "halt_cleanup": None,
                        }
                    )
                    nxt = replayer().run(
                        first, params=values, bundle_dir=bundle, run_dir=work / "next"
                    )
                    rows["dialog_open"] = {
                        "dialog_open_before_cleanup": dialog_before,
                        "dialog_open_after_cleanup": dialog_after,
                        "form_empty_after_cleanup": form_empty,
                        "session": "clean" if reason is None else "dirty",
                        "dirty_reason": reason,
                        "cleanup_steps": len(cleanup.steps),
                        "cleanup_seconds": cleanup_s,
                        "next_item_first_steps_ok": bool(nxt.success),
                        "next_item_stopped_at": next(
                            (r.step_id for r in nxt.results if not r.ok), None
                        ),
                        "model_calls": int(getattr(nxt, "model_calls", 0) or 0),
                    }
                    print("dialog_open", json.dumps(rows["dialog_open"]), flush=True)

                    # -- wrong_screen: the form's tab shows the patient finder
                    # and there is no way back, so the session is dirty.
                    page.evaluate(
                        "({path, name}) => { window.navigateTab(path, name); "
                        "window.activateTabByName(name, true); }",
                        {"path": FINDER_PATH, "name": demo.FORM_FRAME_NAME},
                    )
                    page.wait_for_timeout(3000)
                    reason = run_cleanup(
                        replayer,
                        workflow,
                        cleanup,
                        bundle_dir=bundle,
                        run_dir=work / "cleanup-wrong",
                        reenter=lambda: None,
                    )
                    rows["wrong_screen"] = {
                        "session": "clean" if reason is None else "dirty",
                        "dirty_reason": reason,
                    }
                    print("wrong_screen", json.dumps(rows["wrong_screen"]), flush=True)
                finally:
                    browser.close()
        fx.reset()
    finally:
        fx._compose("down", "-v", timeout_s=300.0)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "openemr.json").write_text(
        json.dumps(
            {
                "schema": "openadapt.halt-cleanup-measurement/v1",
                "app": "openemr",
                "mode": "live (pinned OpenEMR fixture, real browser)",
                "bundle": "scoreboard openemr_referral program bundle (New Patient)",
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "host": platform.platform(),
                "load_1m": round(os.getloadavg()[0], 2),
                "seconds": round(time.monotonic() - started, 1),
                "rows": rows,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
