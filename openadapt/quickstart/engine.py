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
program runs on a new note (the demonstration typed another), and the record
checks read that run's note (``bind_effect_params``) so a new note can be checked.
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

from openadapt.quickstart.scene import MOCKMED

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
    #: True when this run is a repeat: its first try, in a worker, stalled
    #: and was stopped, or its first try never got a ready screen.
    repeated: bool = False
    #: True when the run stopped before its first action because the app's
    #: screen never finished drawing (see :func:`starting_state_not_ready`).
    not_ready: bool = False
    #: Why it stopped, in plain words, when ``not_ready``.
    explanation: str = ""

    @property
    def report_md(self) -> Path:
        return self.run_dir / "REPORT.md"


#: The engine's words for a run its readiness gate stopped before step one.
_NOT_READY_MARK = "starting state not ready"


def starting_state_not_ready(
    execution_outcome: object, transaction_outcome: object, halt_reason: object
) -> bool:
    """True when a run stopped before its first action, on an unready screen.

    The readiness gate halts a run whose starting screen never settles (still
    loading, animating, or, for a visible window, not drawing at all). Only
    ``HALTED_BEFORE_EFFECT`` counts: that outcome proves nothing was written,
    so running again in a fresh folder is safe.
    """

    return (
        str(execution_outcome) == "HALTED"
        and transaction_outcome == "HALTED_BEFORE_EFFECT"
        and _NOT_READY_MARK in str(halt_reason or "")
    )


def not_ready_explanation(visible: bool) -> str:
    """Why a run stopped before its first click, in plain words."""

    if visible:
        return (
            "The browser window never finished drawing, so OpenAdapt stopped "
            "before its first click and saved nothing. A window that is "
            "covered or on another desktop doesn't draw. Run it again without "
            "--watch, or keep the window in view."
        )
    return (
        "The app's page never finished loading, so OpenAdapt stopped before "
        "its first click and saved nothing. Run it again; if it happens "
        "again, the report says what kept changing."
    )


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
    #: True only when the run's own evidence shows it stopped at the check
    #: this act sets off (the record check refuting the save, or the app's
    #: own check the program waits on). A stop for any other reason, or one
    #: whose cause the evidence doesn't show, stays False, and the views then
    #: describe a generic stop instead of the demonstration.
    stop_attributed: bool = False
    #: True when this run stopped before its first action because the app's
    #: screen never finished drawing, even on a second try.
    not_ready: bool = False
    #: Why it stopped, in plain words, when ``not_ready``.
    explanation: str = ""
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


@dataclass
class SealPages:
    """The offline Seal pages the finale wrote (``seal.html`` in a run folder)."""

    #: The first checked run's page: the one the finale opens.
    verified: Optional[Path] = None
    #: The stopped run's page, when Act 3 ran.
    broken: Optional[Path] = None


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
    facts = RunFacts(
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
        not_ready=starting_state_not_ready(
            report.execution_outcome,
            report.transaction_outcome,
            getattr(getattr(report, "halt", None), "reason", ""),
        ),
    )
    if facts.not_ready:
        facts.explanation = not_ready_explanation(visible)
    return facts


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

    #: Act 2's inputs: one new note per run. One run keeps the quickstart as
    #: fast as the engine's own tutorial; Act 2's worker processes start only
    #: for more than one.
    inputs = RUN_NOTES[:1]
    scene = MOCKMED

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

    def readback_fields(self) -> list[tuple[str, Optional[str], Optional[str]]]:
        """The fields each run's record check reads back, from the compiled bundle.

        One ``(field, literal, param)`` per field the save's ``record_written``
        check matches: a fixed value, or the run input it is bound to. Empty
        when the bundle has no such check or can't be read.
        """

        return readback_fields(self.bundle_dir / "workflow.json")

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

        count = min(worker_count(), len(self.inputs))
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
        retry: list[tuple[int, Optional[str], str]] = []

        def finish(run: RunFacts) -> None:
            # A run that never got a ready screen wrote nothing; it runs once
            # more, here, in a new folder, and only that try is reported.
            if run.not_ready and not run.repeated:
                retry.append(
                    (run.index, jobs[run.index - 1][1], f"{run.run_dir.name}-retry")
                )
                return
            on_finish(run)

        leftover = jobs
        pool = self._started_pool()
        if pool is not None:
            leftover = pool.run(
                jobs,
                bundle_dir=self.bundle_dir,
                root=self.root,
                on_finish=finish,
            )
            # Act 2 is their only job: let them shut down during Act 3.
            pool.release()
        for index, note, folder in leftover:
            run = self.run(index, note, folder)
            run.repeated = folder != f"run-{index}"
            finish(run)
        for index, note, folder in retry:
            run = self.run(index, note, folder)
            run.repeated = True
            on_finish(run)

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
        """Rerun the same program while the database rejects the save.

        A run that never got a ready screen (it stopped before its first
        action and wrote nothing) runs once more in ``run-broken-retry``.
        """

        broken = self._break_once(self.root / "run-broken")
        if broken.not_ready:
            again = self._break_once(self.root / "run-broken-retry")
            again.seconds += broken.seconds
            return again
        return broken

    def _break_once(self, run_dir: Path) -> BreakFacts:
        from openadapt_flow.demo_proof import _final_frame, _load_report
        from openadapt_flow.tutorial import _run_break_it, consequential_step

        assert self.workflow is not None
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
        broken = BreakFacts(
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
            # The record check read the save back and refuted it: the
            # silent failure this act injects, not some other refusal.
            stop_attributed=int(result.effects_refuted) > 0,
            not_ready=starting_state_not_ready(
                result.execution_outcome,
                result.transaction_outcome,
                result.halt_reason,
            ),
            result=result,
        )
        if broken.not_ready:
            broken.explanation = not_ready_explanation(self.visible)
        return broken

    def read_screen(self, broken: BreakFacts) -> Optional[str]:
        """What the halted run's screen said.

        The saved message read off its screenshot (:func:`read_screen_text`),
        else the success text the engine itself observed on screen when it
        checked the step, else None.
        """

        text = None
        if broken.screenshot is not None:
            early = getattr(self, "_early", None)
            if early is not None:
                text = early.text_for(broken.screenshot)
            if text is None:
                text = read_screen_text(broken.screenshot)
        if text is None:
            claim = getattr(broken.result, "screen_claim_text", None)
            text = str(claim).strip() if claim else None
        return text

    # -- finale -----------------------------------------------------------

    def seal(self, runs: list[RunFacts]) -> Optional[SealFacts]:
        """Sign each checked run's report with this computer's Seal key.

        Engines without offline Seals return None, and the quickstart prints
        no Seal and no command to check one.
        """

        return seal_runs(runs)

    def seal_broken(self, broken: BreakFacts) -> bool:
        """Sign the stopped run's report too; True once its Seal is written.

        Its Seal says the report is the one the run wrote, stop and all.
        Engines without offline Seals sign nothing.
        """

        try:
            from openadapt_flow.offline_seal import seal_home, seal_run
        except ImportError:
            return False
        seal_run(broken.run_dir, home=seal_home(), enqueue=False)
        write_run_receipts([broken.run_dir])
        return (broken.run_dir / "seal.json").is_file()

    def seal_pages(
        self,
        runs: list[RunFacts],
        broken: Optional[BreakFacts],
        command: Optional[Callable[[Path], str]] = None,
    ) -> SealPages:
        """Write the Seal pages: the first checked run's and the stopped run's.

        Only a sealed run gets a page. Each page is the offline viewer with
        that run's files in it (:mod:`openadapt.seal_view`); nothing leaves
        this computer. Engines without offline Seals get no pages.
        """

        try:
            from openadapt.seal_view import bundle_graph, write_seal_page
        except ImportError:
            return SealPages()
        program = bundle_graph(self.bundle_dir)
        about = {"app": "MockMed, a synthetic clinic app", "job": "Add a triage note"}

        def page(run_dir: Path, kicker: str) -> Path:
            return write_seal_page(
                run_dir,
                program=program,
                about={**about, "kicker": kicker},
                host={
                    "footer": (
                        "A run of the program you compiled, on your computer, in "
                        "MockMed with synthetic data, signed with your computer's "
                        "Seal key and checked here against that key. This page "
                        "makes no network requests; nothing was uploaded."
                    ),
                },
                commands=command(run_dir) if command else None,
            )

        pages = SealPages()
        # Runs finish in any order; the page is run 1's when it checked.
        first = min(
            (run for run in runs if run.checked), key=lambda r: r.index, default=None
        )
        if first is not None and (first.run_dir / "seal.json").is_file():
            pages.verified = page(first.run_dir, f"Run {first.index} of {len(runs)}")
        if broken is not None and (broken.run_dir / "seal.json").is_file():
            pages.broken = page(broken.run_dir, "The run with the failed save")
        return pages

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


def seal_runs(runs: list[RunFacts]) -> Optional[SealFacts]:
    """Sign each checked run's report with this computer's Seal key."""

    try:
        from openadapt_flow.offline_seal import seal_home, seal_run
    except ImportError:
        return None
    # Every run gets a receipt, whatever its outcome; signing isn't grading.
    write_run_receipts([run.run_dir for run in runs])
    checked = [run for run in runs if run.checked]
    if not checked:
        return None
    key_id = ""
    for run in checked:
        # The configured Seal key, or one generated under the Seal home on
        # first use (the offline-Seal default).
        seal = seal_run(run.run_dir, home=seal_home(), enqueue=False)
        key_id = str(seal.key_id)
    return SealFacts(key_id=key_id, sealed=len(checked))


def write_run_receipts(run_dirs: list[Path]) -> int:
    """Write each run's ``receipt.json`` (record/run/v1); returns how many exist."""

    try:
        from openadapt_flow.offline_seal import seal_home, signer_for
        from openadapt_flow.receipt_record import write_receipt_record
    except ImportError:
        return 0
    written = 0
    for run_dir in run_dirs:
        if not (Path(run_dir) / "report.json").is_file():
            continue
        try:
            home = seal_home()
            write_receipt_record(Path(run_dir), signer=signer_for(home), home=home)
        except Exception:  # noqa: BLE001 - a receipt failure never ends the finale
            continue
        written += 1
    return written


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


def readback_fields(
    workflow_json: Path,
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """``(field, literal, param)`` for the first ``record_written`` check in a bundle."""

    try:
        workflow = json.loads(workflow_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    for step in workflow.get("steps") or []:
        for effect in step.get("effects") or []:
            if effect.get("kind") != "record_written":
                continue
            fields = []
            for name, binding in (effect.get("match") or {}).items():
                binding = binding if isinstance(binding, dict) else {}
                literal = binding.get("literal")
                param = binding.get("param")
                fields.append(
                    (
                        str(name),
                        None if literal is None else str(literal),
                        None if param is None else str(param),
                    )
                )
            return fields
    return []


def display_available(
    *, platform: Optional[str] = None, environ=None, isatty=None
) -> bool:
    """True when a browser window (or tab) is likely to reach a person.

    The same rule as ``openadapt flow demo``: output on a terminal, and a
    display (X11 or Wayland, or a local macOS or Windows session, not SSH).
    """

    import os
    import sys

    env = os.environ if environ is None else environ
    tty = isatty() if isatty is not None else sys.stdout.isatty()
    if not tty:
        return False
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return True
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    return (platform or sys.platform) in ("darwin", "win32")


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
    "SealPages",
    "StepTicker",
    "demo_step_label",
    "display_available",
    "open_page",
    "read_screen_text",
    "readback_fields",
    "return_focus_to_terminal",
]
