"""Drive the engine's bundled tutorial pieces and return what each run showed.

Every number the quickstart prints comes from here, and everything here comes
from the engine's own artifacts: the recorder's ``events.jsonl``, the compiled
workflow, each run's report, the demo app's database (read out of band, at a
path the app never calls), and the halted run's screenshot. Nothing is
scripted output.

The stages are the tutorial's own (:mod:`openadapt_flow.tutorial`): record
against MockMed's database-backed server, compile with mined record checks,
certify under ``clinical-write``, and run under the Standard profile through
the unchanged fail-closed run gate. The quickstart adds two things: the
program runs five times with a new note each time, and the record checks read
that run's note (``bind_effect_params``) so a new note can be checked.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: Synthetic notes for the five runs. None of them is clinical advice.
RUN_NOTES = (
    "Follow-up in two weeks",
    "Recheck blood pressure next month",
    "Refer to physical therapy",
    "Repeat labs before next visit",
    "No change to the care plan",
)

#: The part of the 1280x800 screenshot that holds the patient banner and the
#: saved message (the same crop the side-by-side page shows).
SCREEN_CROP = (0, 0, 640, 320)

#: How often the step ticker reads the recorder's event log, in seconds.
_TICK_S = 0.05


@dataclass
class DemoStep:
    """One action of the demonstration, as the recorder logged it."""

    label: str
    seconds: float


@dataclass
class RunFacts:
    """One run of the program, read from its own report and the database."""

    index: int
    note: str
    run_dir: Path
    execution_outcome: str
    transaction_outcome: Optional[str]
    checked: bool
    records: int
    model_calls: int
    cost_usd: float
    effects_required: int
    effects_confirmed: int
    seconds: float
    #: The engine's plain-language result for this run ("Done and checked",
    #: "Stopped before saving", ...), from its transaction outcome.
    plain_label: str = ""

    @property
    def report_md(self) -> Path:
        return self.run_dir / "REPORT.md"


@dataclass
class BreakFacts:
    """The rerun against a database that rejects the write."""

    run_dir: Path
    fault: str
    execution_outcome: str
    transaction_outcome: Optional[str]
    screen_showed_success: bool
    screen_text: Optional[str]
    screenshot: Optional[Path]
    new_records: int
    effects_required: int
    effects_refuted: int
    halt_reason: str
    model_calls: int
    seconds: float
    result: Any = field(default=None, repr=False)

    @property
    def stopped(self) -> bool:
        return self.execution_outcome == "HALTED"

    @property
    def report_md(self) -> Path:
        return self.run_dir / "REPORT.md"


@dataclass
class SealFacts:
    """Offline Seals over the verified runs' reports."""

    key_id: str
    sealed: int


def demo_step_label(event: dict[str, Any]) -> Optional[str]:
    """A short label for one recorded action, from what the recorder logged."""

    kind = event.get("kind")
    if kind == "type":
        text = str(event.get("text") or "")
        return f'Type "{text}"' if text else "Type"
    if kind in ("click", "double_click"):
        structural = event.get("structural") or {}
        name = str(structural.get("name") or "").strip()
        return f"Click {name}" if name else "Click"
    if kind == "key":
        return f"Press {event.get('key') or 'a key'}"
    return None


class StepTicker:
    """Read the recorder's ``events.jsonl`` while it is being written.

    The recorder appends one line per committed action, so a second thread
    that tails the file reports each step as it happens without any change to
    the engine.
    """

    def __init__(self, events: Path, on_step: Callable[[DemoStep], None]) -> None:
        self._events = events
        self._on_step = on_step
        self._offset = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="quickstart-ticker")
        self._thread.daemon = True
        self.steps: list[DemoStep] = []

    def __enter__(self) -> "StepTicker":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self._drain()

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_S):
            self._drain()

    def _drain(self) -> None:
        try:
            with self._events.open("r", encoding="utf-8") as handle:
                handle.seek(self._offset)
                text = handle.read()
        except FileNotFoundError:
            return
        complete, _, _partial = text.rpartition("\n")
        if not complete and not text.endswith("\n"):
            return
        self._offset += len((complete + "\n").encode("utf-8"))
        for line in complete.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            label = demo_step_label(event)
            if label is None:
                continue
            step = DemoStep(label=label, seconds=float(event.get("t") or 0.0))
            self.steps.append(step)
            self._on_step(step)


def _accepts(function: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _effect_counts(report: Any) -> tuple[int, int]:
    envelope = getattr(report, "outcome_envelope", None)
    if envelope is None:
        return 0, 0
    return int(envelope.required_contracts.effect), int(
        envelope.passed_contracts.effect
    )


def read_screen_text(screenshot: Path) -> Optional[str]:
    """Read the saved message off the halted run's screenshot.

    The engine's halt record keeps the on-screen text as its OCR read it at
    screen scale, which can run words together. Reading the same pixels at
    twice the scale gives the words back. Returns the line that says
    "saved", or None when no line does.
    """

    from openadapt_flow.vision.ocr import ocr, upscale_png

    from openadapt.quickstart.images import crop_png

    try:
        png = upscale_png(crop_png(screenshot, SCREEN_CROP), 2)
        lines = [str(line.text).strip() for line in ocr(png)]
    except Exception:  # noqa: BLE001 - the screenshot itself still shows it
        return None
    return next((line for line in lines if "saved" in line.lower()), None)


def run_program(
    *,
    base_url: str,
    workflow: Any,
    bundle_dir: Path,
    run_dir: Path,
    index: int,
    note: Optional[str],
    visible: bool,
) -> RunFacts:
    """Run the compiled program once on a fresh copy of the clinic's database.

    ``note`` is this run's input when the engine accepts run inputs; None
    replays the demonstrated note. The facts come from the run's own report
    and an out-of-band read of the database.
    """

    from openadapt_flow.plain_outcome import plain_result
    from openadapt_flow.receipt import _build_tutorial_receipt, write_receipt
    from openadapt_flow.report import render_run_report
    from openadapt_flow.tutorial import TUTORIAL_NOTE, _records, run_tutorial_workflow

    extra: dict[str, Any] = {}
    if note is not None and _accepts(run_tutorial_workflow, "params"):
        extra["params"] = {"note": note}
    else:
        note = str(workflow.params.get("note", TUTORIAL_NOTE))
    started = time.monotonic()
    report = run_tutorial_workflow(
        base_url=base_url,
        workflow=workflow,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        headed=visible,
        **extra,
    )
    records = _records(base_url)
    seconds = time.monotonic() - started
    render_run_report(run_dir)
    required, confirmed = _effect_counts(report)
    checked = (
        report.execution_outcome == "VERIFIED"
        and report.transaction_outcome == "VERIFIED"
        and required > 0
        and confirmed == required
        and any(record.get("note") == note for record in records)
    )
    if report.execution_outcome == "VERIFIED":
        write_receipt(_build_tutorial_receipt(report), run_dir)
    return RunFacts(
        index=index,
        note=note,
        run_dir=run_dir,
        execution_outcome=str(report.execution_outcome),
        transaction_outcome=report.transaction_outcome,
        checked=checked,
        records=len(records),
        model_calls=int(report.model_calls),
        cost_usd=float(getattr(report, "est_model_cost_usd", 0.0) or 0.0),
        effects_required=required,
        effects_confirmed=confirmed,
        seconds=seconds,
        plain_label=plain_result(
            report.transaction_outcome, report.execution_outcome
        ).label,
    )


class EarlyScreenRead:
    """Read a screenshot on a thread as soon as it appears on disk.

    :meth:`text_for` returns the text only for the same file with the same
    bytes as were read; anything else is read again by the caller.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stop = threading.Event()
        self._digest: Optional[str] = None
        self._text: Optional[str] = None
        self._thread = threading.Thread(target=self._loop, name="quickstart-screen")
        self._thread.daemon = True
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_S):
            if self._path.is_file():
                time.sleep(_TICK_S)  # let the writer finish the file
                digest = _sha256(self._path)
                self._text = read_screen_text(self._path)
                self._digest = digest
                return

    def stop(self) -> None:
        self._stop.set()

    def text_for(self, path: Path) -> Optional[str]:
        self._thread.join(timeout=10)
        if self._digest is None or Path(path).resolve() != self._path.resolve():
            return None
        return self._text if _sha256(Path(path)) == self._digest else None


def _sha256(path: Path) -> Optional[str]:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class FlowEngine:
    """The bundled tutorial, one stage per call, inside one browser process."""

    def __init__(self, root: Path, *, visible: bool, pace_s: float = 0.0) -> None:
        self.root = root
        self.visible = visible
        self.pace_s = pace_s
        self.recording_dir = root / "recording"
        self.bundle_dir = root / "bundle"
        self.workflow: Any = None
        self._stack = contextlib.ExitStack()
        self._base_url: Optional[str] = None
        self._pool: Any = None
        self._starter: Any = None

    # -- lifetime ---------------------------------------------------------

    def __enter__(self) -> "FlowEngine":
        from openadapt_flow import _prewarm
        from openadapt_flow.backends._browser_process import shared_browser
        from openadapt_flow.mockmed.fault_server import serve

        # Load OCR while the browser starts; compiling and every run use it.
        _prewarm.start()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._stack.enter_context(shared_browser(headless=not self.visible))
            base_url, _db, stop = serve()
            self._stack.callback(stop)
        except BaseException:
            self._stack.close()
            raise
        self._base_url = base_url
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    # -- act 1 ------------------------------------------------------------

    def record(self, on_step: Callable[[DemoStep], None]) -> list[DemoStep]:
        """Demonstrate the task once in the browser, reporting each step."""

        from openadapt_flow.tutorial import record_tutorial

        assert self._base_url is not None
        with StepTicker(self.recording_dir / "events.jsonl", on_step) as ticker:
            record_tutorial(
                self._base_url,
                self.recording_dir,
                headed=self.visible,
                presentation_delay_s=self.pace_s,
            )
        return ticker.steps

    def compile(self) -> int:
        """Compile and certify the recording; return the program's step count."""

        from openadapt_flow.compiler import compile_recording
        from openadapt_flow.tutorial import (
            TUTORIAL_WORKFLOW_NAME,
            certify_tutorial,
            consequential_step,
        )

        workflow = compile_recording(
            self.recording_dir,
            self.bundle_dir,
            name=TUTORIAL_WORKFLOW_NAME,
            mine_effects=True,
            bind_effect_params=True,
        )
        consequential_step(workflow)
        certify_tutorial(workflow)
        self.workflow = workflow
        return len(workflow.steps)

    @property
    def program_name(self) -> str:
        return str(getattr(self.workflow, "name", "") or "")

    def note_param(self) -> bool:
        """True when the engine can give each run its own note."""

        from openadapt_flow.tutorial import run_tutorial_workflow

        return _accepts(run_tutorial_workflow, "params")

    # -- act 2 ------------------------------------------------------------

    def run(
        self, index: int, note: Optional[str], folder: Optional[str] = None
    ) -> RunFacts:
        """Run the program once, in this process, on a fresh database."""

        assert self._base_url is not None and self.workflow is not None
        return run_program(
            base_url=self._base_url,
            workflow=self.workflow,
            bundle_dir=self.bundle_dir,
            run_dir=self.root / (folder or f"run-{index}"),
            index=index,
            note=note,
            visible=self.visible,
        )

    def start_workers(self) -> None:
        """Start the worker processes for Act 2 while Act 1 is still running."""

        import threading

        from openadapt.quickstart.workers import (
            WorkerPool,
            worker_browser,
            worker_count,
        )

        count = worker_count()
        if count <= 1 or self._starter is not None:
            return

        def start() -> None:
            # Checking for the bundled browser takes about a second, so it
            # happens here, off the main thread, once for every worker.
            browser = worker_browser(self.visible)
            if browser is not None:
                self._pool = WorkerPool(count, visible=self.visible, browser=browser)

        self._starter = threading.Thread(target=start, name="quickstart-workers")
        self._starter.start()
        self._stack.callback(self._close_pool)

    def _close_pool(self) -> None:
        if self._starter is not None:
            self._starter.join()
        if self._pool is not None:
            self._pool.close()

    def _started_pool(self) -> Any:
        if self._starter is not None:
            self._starter.join()
        return self._pool

    @property
    def parallel(self) -> int:
        """How many runs can go at once: the workers that didn't give up."""

        pool = self._started_pool()
        return max(1, pool.usable()) if pool is not None else 1

    def run_all(
        self, notes: Sequence[str], on_finish: Callable[[RunFacts], None]
    ) -> None:
        """Run the program once per note, in the worker processes when started.

        Each run is a complete checked run in its own browser, against its own
        copy of the clinic's database. A run a worker can't take (a worker
        that failed to start, or one that stalled) runs here instead, so every
        note still gets its own checked run.
        """

        assert self.workflow is not None
        varied = self.note_param()
        jobs = [
            (index, note if varied else None, f"run-{index}")
            for index, note in enumerate(notes, 1)
        ]
        leftover = jobs
        pool = self._started_pool()
        if pool is not None:
            leftover = pool.run(
                jobs,
                bundle_dir=self.bundle_dir,
                root=self.root,
                on_finish=on_finish,
            )
            # Act 2 is their only job: let them shut down during Act 3.
            pool.release()
        for index, note, folder in leftover:
            on_finish(self.run(index, note, folder))

    # -- act 3 ------------------------------------------------------------

    def before_prompt(self) -> None:
        """Give the keyboard back to the terminal before asking for Enter.

        On macOS a visible browser takes focus when its window opens and keeps
        it after the window closes, so Enter would go to the browser. The
        terminal app's bundle ID is in the environment macOS gives programs
        it starts; ``open -b`` brings that app forward without a permission
        prompt. Elsewhere, or without the ID, nothing happens.
        """

        return_focus_to_terminal(visible=self.visible)

    def break_it(self) -> BreakFacts:
        """Rerun the same program while the database rejects the save."""

        from openadapt_flow.demo_proof import _final_frame, _load_report
        from openadapt_flow.tutorial import _run_break_it, consequential_step

        assert self.workflow is not None
        run_dir = self.root / "run-broken"
        save = consequential_step(self.workflow).id
        # The save step's screenshot lands before the engine's wait on the
        # database, so it can be read during that wait.
        self._early = EarlyScreenRead(run_dir / "steps" / f"{save}_after.png")
        started = time.monotonic()
        try:
            result = _run_break_it(
                workflow=self.workflow,
                bundle_dir=self.bundle_dir,
                run_dir=run_dir,
                headed=self.visible,
                say=lambda message: None,
            )
        finally:
            self._early.stop()
        seconds = time.monotonic() - started
        report = _load_report(Path(result.run_dir))
        screenshot = _final_frame(Path(result.run_dir), report) if report else None
        screen_text = None  # read by read_screen(), alongside the page
        return BreakFacts(
            run_dir=Path(result.run_dir),
            fault=result.fault,
            execution_outcome=str(result.execution_outcome),
            transaction_outcome=result.transaction_outcome,
            screen_showed_success=bool(result.screen_claimed_success),
            screen_text=screen_text,
            screenshot=screenshot,
            new_records=int(result.system_of_record_records),
            effects_required=int(result.effects_required),
            effects_refuted=int(result.effects_refuted),
            halt_reason=str(result.halt_reason),
            model_calls=int(getattr(report, "model_calls", 0) or 0),
            seconds=seconds,
            result=result,
        )

    def read_screen(self, broken: BreakFacts) -> Optional[str]:
        """What the halted run's screenshot says (see :func:`read_screen_text`)."""

        if broken.screenshot is None:
            return None
        early = getattr(self, "_early", None)
        if early is not None:
            text = early.text_for(broken.screenshot)
            if text is not None:
                return text
        return read_screen_text(broken.screenshot)

    # -- finale -----------------------------------------------------------

    def seal(self, runs: list[RunFacts]) -> Optional[SealFacts]:
        """Sign each checked run's report with this computer's Seal key.

        Engines without offline Seals return None, and the quickstart prints
        no Seal and no command to check one.
        """

        try:
            from openadapt_flow.offline_seal import seal_home, seal_run, signer_for
        except ImportError:
            return None
        checked = [run for run in runs if run.checked]
        if not checked:
            return None
        signer = signer_for(seal_home())
        key_id = ""
        for run in checked:
            seal = seal_run(run.run_dir, enqueue=False, signer=signer)
            key_id = str(seal.key_id)
        return SealFacts(key_id=key_id, sealed=len(checked))

    def write_page(self, first: RunFacts, broken: BreakFacts) -> Optional[Path]:
        """Write the side-by-side page (``openadapt flow demo``'s page)."""

        from openadapt_flow import demo_proof
        from openadapt_flow.tutorial import TutorialResult

        report = demo_proof._load_report(first.run_dir)
        tier: Optional[int] = None
        for step in getattr(report, "results", []) or []:
            for evidence in step.effect_evidence:
                if (
                    evidence.final_verdict == "confirmed"
                    and evidence.verification_tier is not None
                ):
                    value = int(evidence.verification_tier)
                    tier = value if tier is None else min(tier, value)
        receipts = {
            kind: first.run_dir / f"receipt.{kind}"
            for kind in ("json", "md", "png")
            if (first.run_dir / f"receipt.{kind}").is_file()
        }
        result = TutorialResult(
            recording_dir=self.recording_dir,
            bundle_dir=self.bundle_dir,
            run_dir=first.run_dir,
            execution_outcome=first.execution_outcome,
            transaction_outcome=first.transaction_outcome,
            execution_profile=getattr(report, "execution_profile", None),
            transaction_billable=getattr(report, "transaction_billable", None),
            model_calls=first.model_calls,
            effects_required=first.effects_required,
            effects_confirmed=first.effects_confirmed,
            effect_tier=tier,
            bundle_digest=getattr(report, "bundle_content_digest", None),
            system_of_record_records=first.records,
            receipt_paths=receipts,
            break_it=broken.result,
        )
        evidence = demo_proof.collect_evidence(result, self.root)
        return demo_proof.write_demo_page(evidence)


def return_focus_to_terminal(
    *, visible: bool, platform: Optional[str] = None, environ=None, run=None
) -> bool:
    """Bring the terminal app forward on macOS after a visible browser ran."""

    import os
    import subprocess
    import sys

    env = os.environ if environ is None else environ
    bundle = env.get("__CFBundleIdentifier", "")
    if not visible or (platform or sys.platform) != "darwin" or not bundle:
        return False
    try:
        (run or subprocess.run)(["open", "-b", bundle], check=False, timeout=5)
    except Exception:  # noqa: BLE001 - focus is a convenience only
        return False
    return True


def display_available() -> bool:
    """True when a browser window (or tab) is likely to reach a person."""

    from openadapt_flow.demo_proof import display_available as available

    return available()


def open_page(page: Path) -> bool:
    from openadapt_flow.demo_proof import open_page as opener

    return opener(page)


__all__ = [
    "RUN_NOTES",
    "BreakFacts",
    "DemoStep",
    "FlowEngine",
    "RunFacts",
    "SealFacts",
    "StepTicker",
    "demo_step_label",
    "display_available",
    "open_page",
    "read_screen_text",
    "return_focus_to_terminal",
]
