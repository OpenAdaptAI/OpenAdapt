"""``openadapt quickstart --app openemr``: the three acts on a real OpenEMR.

The default quickstart uses the bundled MockMed: no Docker, about 22 seconds.
This mode runs the same story on OpenEMR 8.0.0.3 in Docker, with the certified
referral program behind the site's OpenEMR Seals:

1. Show it once: the referral as it was demonstrated once in OpenEMR, from
   the recording the program was compiled from.
2. Run it: synthetic referrals at once, one clerk account and browser each,
   every run through the certified run path (the Standard run gate, the REST
   record check) and read back from OpenEMR's database.
3. Break it: OpenEMR gets an existing chart, then a referral with the same
   name and another birth date. OpenEMR's duplicate check lists the match, and
   the program stops before saving. The database has no new chart.

The OpenEMR fixture lives in the repository (``packages/flow/benchmark``),
not in the published packages, so this mode runs from a clone. The fixture's
state stays in the user's cache folder, and a running copy is reused.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt.quickstart.engine import (
    BreakFacts,
    DemoStep,
    RunFacts,
    SealFacts,
    return_focus_to_terminal,
    seal_runs,
)
from openadapt.quickstart.scene import OPENEMR

DOCKER_INSTALL_URL = "https://docs.docker.com/get-docker/"
CLONE_URL = "https://github.com/OpenAdaptAI/OpenAdapt"

#: A referral takes 20 to 30 seconds alone and longer five at a time. One
#: that hasn't reported after this long has stalled.
STALL_S = 240.0
#: Workers sign in before they report ready.
READY_TIMEOUT_S = 240.0

#: The duplicate-check dialog in the 1280x800 screenshot.
DIALOG_CROP = (204, 30, 1078, 262)

#: Field labels for the demonstration's typed values.
FIELD_LABELS = {
    "title": "Title",
    "fname": "First name",
    "lname": "Last name",
    "DOB": "Date of birth",
    "sex": "Sex",
    "street": "Street",
    "city": "City",
    "state_label": "State",
    "postal_code": "Postal code",
    "phone_home": "Home phone",
    "email": "Email",
    "country_code": "Country",
}


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def docker_problem(
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> Optional[str]:
    """One line saying why Docker can't run OpenEMR here, or None."""

    if which("docker") is None:
        return (
            "OpenEMR mode needs Docker, which isn't installed. Install Docker "
            f"Desktop ({DOCKER_INSTALL_URL}), then run this again."
        )
    try:
        info = run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        info = None
    if info is None or info.returncode != 0:
        return (
            "OpenEMR mode needs Docker, which is installed but not running. "
            "Start Docker Desktop, then run this again."
        )
    try:
        compose = run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        compose = None
    if compose is None or compose.returncode != 0:
        return (
            "OpenEMR mode needs Docker Compose v2 (docker compose), which "
            f"isn't available. Install Docker Desktop ({DOCKER_INSTALL_URL}), "
            "then run this again."
        )
    return None


def _has_fixture(flow: Path) -> bool:
    return (flow / "benchmark" / "openemr_local" / "quickstart.py").is_file() and (
        flow / "scripts" / "openemr_local_demo.py"
    ).is_file()


def find_flow_checkout(candidates: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """The ``packages/flow`` folder of a clone that has the OpenEMR fixture.

    The installed engine's own folder comes first (an editable install from a
    clone), then the launcher's.
    """

    if candidates is None:
        found: list[Path] = []
        with contextlib.suppress(ImportError):
            import openadapt_flow

            found.append(Path(openadapt_flow.__file__).resolve().parents[1])
        import openadapt

        found.append(
            Path(openadapt.__file__).resolve().parents[1] / "packages" / "flow"
        )
        candidates = found
    return next((path for path in candidates if _has_fixture(path)), None)


def checkout_problem(flow: Optional[Path]) -> Optional[str]:
    if flow is not None:
        return None
    return (
        "OpenEMR mode runs from a clone of the repository: git clone "
        f"{CLONE_URL} && cd OpenAdapt && pip install -e . -e packages/flow, "
        "then run this again."
    )


def _driver(flow: Path) -> Any:
    """The fixture driver, imported from the clone."""

    path = str(flow)
    if path not in sys.path:
        sys.path.insert(0, path)
    from benchmark.openemr_local import quickstart

    return quickstart


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def demo_step_label(event: dict[str, Any]) -> Optional[str]:
    """A label for one demonstrated action worth showing, else None.

    Shown: each typed value, with its field, and the clicks that move the
    task on. Not shown: the clicks into fields, select-all and Enter keys,
    and scrolls (all of them are still steps of the program).
    """

    kind = event.get("kind")
    if kind == "type":
        field = FIELD_LABELS.get(str(event.get("param") or ""), "")
        text = str(event.get("text") or "")
        return f'{field}: "{text}"' if field else f'Type "{text}"'
    if kind == "click":
        selector = event.get("selector")
        if selector == "#create":
            return "Click Create New Patient"
        if selector == "#confirmCreate":
            return "Click Confirm Create New Patient"
        if event.get("name") == "Contact":
            return "Open the Contact section"
    return None


def run_facts(index: int, facts: dict[str, Any], checked: bool) -> RunFacts:
    return RunFacts(
        index=index,
        note=str(facts["name"]),
        run_dir=Path(facts["run_dir"]),
        execution_outcome=str(facts["execution_outcome"]),
        transaction_outcome=facts["transaction_outcome"],
        checked=checked,
        records=int(facts["charts"]),
        model_calls=int(facts["model_calls"]),
        cost_usd=float(facts["cost_usd"]),
        effects_required=int(facts["effects_required"]),
        effects_confirmed=int(facts["effects_confirmed"]),
        seconds=float(facts["seconds"]),
        plain_label=str(facts["plain_label"]),
    )


def read_dialog(screenshot: Path) -> Optional[str]:
    """The matching chart OpenEMR's duplicate check listed, read off the screen."""

    import re

    from openadapt_flow.vision.ocr import ocr, upscale_png

    from openadapt.quickstart.images import crop_png

    try:
        png = upscale_png(crop_png(screenshot, DIALOG_CROP), 2)
        lines = [str(line.text).strip() for line in ocr(png)]
    except Exception:  # noqa: BLE001 - the screenshot itself still shows it
        return None
    name = next((line for line in lines if "," in line and "Delgado" in line), None)
    born = next(
        (line for line in lines if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line)), None
    )
    if name is None:
        return None
    # OCR can drop the space after the comma ("Delgado,Rosa").
    name = re.sub(r",(?=\S)", ", ", name)
    return f"a matching chart: {name}" + (f", born {born}" if born else "")


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def _worker_main(
    conn: Any,
    visible: bool,
    browser: str,
    flow: str,
    state_dir: str,
    user: str,
) -> None:
    """One clerk: sign in, then run the program for each referral it is sent."""

    os.environ.setdefault("OPENADAPT_FLOW_OCR_THREADS", "2")
    if browser:
        os.environ["OPENADAPT_FLOW_BROWSER"] = browser
    if hasattr(os, "nice"):
        with contextlib.suppress(OSError):
            os.nice(10)
    session = None
    try:
        from openadapt_flow import _prewarm
        from openadapt_flow.backends._browser_process import shared_browser
        from openadapt_flow.ir import Workflow

        _prewarm.start()
        driver = _driver(Path(flow))
        fixture = driver.make_fixture(Path(state_dir))
        driver.point_qualify_hooks_here(fixture)
        with shared_browser(headless=not visible):
            session = driver.Session(fixture, user, headless=not visible)
            session.sign_in()
            session.open_form()
            conn.send(("ready",))
            while True:
                message = conn.recv()
                if message[0] != "run":
                    return
                _, index, key, bundle_dir, run_dir = message
                try:
                    facts = session.run(
                        Workflow.load(Path(bundle_dir)),
                        Path(bundle_dir),
                        key,
                        Path(run_dir),
                        trial=index,
                    )
                except Exception as exc:  # noqa: BLE001 - reported to the parent
                    conn.send(("failed", f"{type(exc).__name__}: {exc}"))
                    continue
                payload = asdict(run_facts(index, facts, driver.checked(facts)))
                payload["run_dir"] = str(payload["run_dir"])
                conn.send(("done", payload))
    except (EOFError, KeyboardInterrupt, BrokenPipeError):
        return
    except Exception as exc:  # noqa: BLE001 - the parent runs the job itself
        with contextlib.suppress(BrokenPipeError, OSError):
            conn.send(("broken", f"{type(exc).__name__}: {exc}"))
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()
        conn.close()


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


_HOW = {
    "reused": "reused the running copy",
    "restarted": "started the existing copy",
    "created": "set up now; later runs reuse it",
}


class OpenEMREngine:
    """The story's engine on OpenEMR: the fixture, the clerks, the program."""

    scene = OPENEMR

    def __init__(
        self,
        root: Path,
        *,
        visible: bool,
        flow: Path,
        state_dir: Optional[Path] = None,
    ) -> None:
        self.root = root
        self.visible = visible
        self.flow = flow
        #: The demonstration's actions, copied from the clone.
        self.recording_dir = root / "demonstration.jsonl"
        self.bundle_dir = root / "bundle"
        self.workflow: Any = None
        self.setup_summary: Optional[str] = None
        self._driver = _driver(flow)
        self._state_dir = state_dir or self._driver.make_fixture().state_dir
        self._fixture: Any = None
        self._progress: Callable[[str, bool], None] = lambda text, milestone: None
        self._stack = contextlib.ExitStack()
        self._pool: Any = None
        self._starter: Optional[threading.Thread] = None
        self._count = 0
        # Leftover runs and Act 3 run on one thread with its own browser
        # (a browser's objects belong to the thread that made it).
        self._main = ThreadPoolExecutor(1, thread_name_prefix="quickstart-openemr")
        self._session: Any = None

    @property
    def inputs(self) -> Sequence[str]:
        return tuple(self._driver.REFERRALS)

    program_detail = (
        "(the certified referral program behind the site's OpenEMR Seals: "
        "qualified 6 of 6 cases under clinical-write; record check: OpenEMR's "
        "REST API)"
    )

    # -- lifetime ---------------------------------------------------------

    def set_progress(self, progress: Callable[[str, bool], None]) -> None:
        self._progress = progress

    def __enter__(self) -> "OpenEMREngine":
        from openadapt_flow import _prewarm

        _prewarm.start()
        self.root.mkdir(parents=True, exist_ok=True)
        self._stack.callback(self._shutdown)
        driver = self._driver
        self._fixture = driver.make_fixture(self._state_dir)
        driver.point_qualify_hooks_here(self._fixture)
        started = time.monotonic()
        phase = {"name": "", "text": "", "since": started}
        lock = threading.Lock()
        done = threading.Event()

        def show() -> None:
            with lock:
                elapsed = time.monotonic() - phase["since"]
                text = phase["text"]
            self._progress(f"{text} {int(elapsed)}s", False)

        def say(name: str, text: str) -> None:
            with lock:
                milestone = name != phase["name"] or text.endswith("downloaded")
                if name != phase["name"]:
                    phase["since"] = time.monotonic()
                phase.update(name=name, text=text)
            self._progress(text, milestone)

        def tick() -> None:
            while not done.wait(1.0):
                show()

        ticker = threading.Thread(target=tick, name="quickstart-setup", daemon=True)
        ticker.start()
        try:
            how = driver.bring_up(self._fixture, say)
            say("setup", "adding synthetic clerk accounts")
            self._count = self._worker_target()
            driver.add_clerks(self._fixture, self._count + 1)
        finally:
            done.set()
            ticker.join()
        self.setup_summary = (
            f"OpenEMR 8.0.0.3 at {self._fixture.ui_base_url}: {_HOW[how]} "
            f"{time.monotonic() - started:.1f}s"
        )
        self.start_workers()
        self._main.submit(self._open_session)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def _shutdown(self) -> None:
        if self._starter is not None:
            self._starter.join()
        if self._pool is not None:
            self._pool.close()
        if self._session is not None:
            with contextlib.suppress(Exception):
                self._main.submit(self._session.close).result(timeout=60)
        self._main.shutdown(wait=False)

    def _worker_target(self) -> int:
        from openadapt.quickstart.workers import worker_count

        return max(1, min(worker_count(), len(self._driver.REFERRALS)))

    def _clerk(self, number: int) -> str:
        return f"oa_clerk{number}"

    def _open_session(self) -> None:
        if self._session is None:
            self._session = self._driver.Session(
                self._fixture, self._clerk(self._count + 1), headless=not self.visible
            )
            self._session.sign_in()

    # -- act 1 ------------------------------------------------------------

    def record(self, on_step: Callable[[DemoStep], None]) -> list[DemoStep]:
        """The demonstration the program was compiled from, step by step."""

        shutil.copyfile(self._driver.DEMONSTRATION, self.recording_dir)
        steps = []
        for event in self._driver.demonstration():
            label = demo_step_label(event)
            if label is None:
                continue
            step = DemoStep(label=label, seconds=float(event.get("t") or 0.0))
            steps.append(step)
            on_step(step)
        return steps

    def compile(self) -> int:
        """Copy the certified program into the output folder; its step count."""

        self.workflow = self._driver.copy_program(self.bundle_dir)
        return len(self.workflow.steps)

    @property
    def program_name(self) -> str:
        return str(getattr(self.workflow, "name", "") or "")

    def note_param(self) -> bool:
        return True

    # -- act 2 ------------------------------------------------------------

    def start_workers(self) -> None:
        from openadapt.quickstart.workers import WorkerPool, worker_browser

        if self._count <= 1 or self._starter is not None:
            return

        def start() -> None:
            browser = worker_browser(self.visible)
            if browser is None:
                return
            self._pool = WorkerPool(
                self._count,
                visible=self.visible,
                browser=browser,
                target=_worker_main,
                worker_args=[
                    (str(self.flow), str(self._state_dir), self._clerk(n))
                    for n in range(1, self._count + 1)
                ],
                stall_s=STALL_S,
                ready_timeout_s=READY_TIMEOUT_S,
            )

        self._starter = threading.Thread(target=start, name="quickstart-workers")
        self._starter.start()

    def _started_pool(self) -> Any:
        if self._starter is not None:
            self._starter.join()
        return self._pool

    @property
    def parallel(self) -> int:
        pool = self._started_pool()
        return max(1, pool.usable()) if pool is not None else 1

    def _run_here(self, index: int, key: str, folder: str) -> RunFacts:
        def work() -> RunFacts:
            self._open_session()
            facts = self._session.run(
                self.workflow, self.bundle_dir, key, self.root / folder, trial=index
            )
            return run_facts(index, facts, self._driver.checked(facts))

        return self._main.submit(work).result()

    def run_all(
        self, keys: Sequence[str], on_finish: Callable[[RunFacts], None]
    ) -> None:
        """One referral per clerk, all at once; any a clerk can't take runs here."""

        assert self.workflow is not None
        jobs = [(index, key, f"run-{index}") for index, key in enumerate(keys, 1)]
        leftover = jobs
        pool = self._started_pool()
        if pool is not None:
            leftover = pool.run(
                jobs, bundle_dir=self.bundle_dir, root=self.root, on_finish=on_finish
            )
            pool.release()
        for index, key, folder in leftover:
            run = self._run_here(index, key, folder)
            run.repeated = folder != f"run-{index}"
            on_finish(run)

    # -- act 3 ------------------------------------------------------------

    def before_prompt(self) -> None:
        return_focus_to_terminal(visible=self.visible)

    def break_it(self) -> BreakFacts:
        """Add an existing chart; run the referral that matches it."""

        assert self.workflow is not None
        driver, fixture = self._driver, self._fixture
        run_dir = self.root / "run-broken"

        def work() -> tuple[dict[str, Any], int, Optional[Path]]:
            self._open_session()
            driver.seed_existing_chart(fixture)
            before = {row["id"] for row in driver.charts(fixture)}
            facts = self._session.run(
                self.workflow, self.bundle_dir, driver.DUPLICATE, run_dir, trial=99
            )
            screenshot = self._session.screenshot(run_dir / "screen.png")
            after = {row["id"] for row in driver.charts(fixture)}
            return facts, len(after - before), screenshot

        facts, new_charts, screenshot = self._main.submit(work).result()
        (run_dir / "quickstart-readback.json").write_text(
            json.dumps(
                {
                    "existing_chart": "Rosa Delgado, born 1958-03-27",
                    "referral": f"{facts['name']}, born {facts['dob']}",
                    "new_charts_in_database": new_charts,
                },
                indent=2,
            )
            + "\n"
        )
        return BreakFacts(
            run_dir=run_dir,
            fault="possible duplicate",
            execution_outcome=str(facts["execution_outcome"]),
            transaction_outcome=facts["transaction_outcome"],
            screen_showed_success=False,
            screen_text=None,
            screenshot=screenshot,
            new_records=new_charts,
            effects_required=sum(len(step.effects) for step in self.workflow.steps),
            effects_refuted=0,
            halt_reason=str(facts["halt_reason"]),
            model_calls=int(facts["model_calls"]),
            seconds=float(facts["seconds"]),
        )

    def read_screen(self, broken: BreakFacts) -> Optional[str]:
        if broken.screenshot is None:
            return None
        return read_dialog(broken.screenshot)

    # -- finale -----------------------------------------------------------

    def seal(self, runs: list[RunFacts]) -> Optional[SealFacts]:
        return seal_runs(runs)

    def write_page(self, first: RunFacts, broken: BreakFacts) -> Optional[Path]:
        return None


__all__ = [
    "DOCKER_INSTALL_URL",
    "OpenEMREngine",
    "checkout_problem",
    "demo_step_label",
    "docker_problem",
    "find_flow_checkout",
    "read_dialog",
]
