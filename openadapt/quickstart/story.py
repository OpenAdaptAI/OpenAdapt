"""The quickstart in three acts: show it once, run it, break it.

This module decides what happens and in what order. The engine adapter
(:mod:`openadapt.quickstart.engine`) does the work and measures it; the view
(:mod:`openadapt.quickstart.view`) decides how it looks. Every number a view
prints arrives here from the engine, so a change in what a run did changes the
output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from openadapt.quickstart.engine import (
    RUN_NOTES,
    BreakFacts,
    DemoStep,
    RunFacts,
    SealFacts,
)
from openadapt.quickstart.scene import MOCKMED, Scene

#: The placeholder app address in the "try it on your own app" command.
YOUR_APP_URL = "https://your-app.example"


def display_path(path: Path, cwd: Optional[Path] = None) -> Path:
    """``path`` relative to the current folder when it is inside it.

    Commands printed with it work from the folder the quickstart ran in.
    """

    base = (cwd or Path.cwd()).resolve()
    try:
        return path.resolve().relative_to(base)
    except ValueError:
        return path


class Engine(Protocol):
    """What the story needs from the engine (a fake one in tests)."""

    root: Path
    visible: bool
    #: The app's words (MockMed's when an engine has none).
    scene: Scene
    #: Act 2's inputs, one run each.
    inputs: Sequence[str]

    def __enter__(self) -> "Engine": ...

    def __exit__(self, *exc: object) -> None: ...

    def record(self, on_step: Callable[[DemoStep], None]) -> list[DemoStep]: ...

    def compile(self) -> int: ...

    @property
    def program_name(self) -> str: ...

    def note_param(self) -> bool: ...

    def start_workers(self) -> None: ...

    @property
    def parallel(self) -> int: ...

    def run_all(
        self, notes: Sequence[str], on_finish: Callable[[RunFacts], None]
    ) -> None: ...

    def before_prompt(self) -> None: ...

    def break_it(self) -> BreakFacts: ...

    def read_screen(self, broken: BreakFacts) -> Optional[str]: ...

    def seal(self, runs: list[RunFacts]) -> Optional[SealFacts]: ...

    def write_page(self, first: RunFacts, broken: BreakFacts) -> Optional[Path]: ...


@dataclass
class NextSteps:
    """The commands the finale prints, each spelled to work as printed."""

    verify: Optional[str]
    record: str
    agent: str
    #: Under ``uvx`` only: the command that installs this version for good.
    keep: Optional[str] = None
    #: Under ``uvx`` only: a warning when the shell's ``openadapt`` is another
    #: version, which the plain commands would reach instead.
    shadow: Optional[str] = None


@dataclass
class Outcome:
    """Everything one quickstart showed, for the finale and the tests."""

    root: Path
    demo_steps: list[DemoStep] = field(default_factory=list)
    program_steps: int = 0
    program_name: str = ""
    act1_seconds: float = 0.0
    runs: list[RunFacts] = field(default_factory=list)
    act2_seconds: float = 0.0
    broken: Optional[BreakFacts] = None
    seal: Optional[SealFacts] = None
    page: Optional[Path] = None
    page_opened: bool = False
    total_seconds: float = 0.0

    @property
    def checked(self) -> int:
        return sum(1 for run in self.runs if run.checked)

    @property
    def model_calls(self) -> int:
        calls = sum(run.model_calls for run in self.runs)
        if self.broken is not None:
            calls += self.broken.model_calls
        return calls

    @property
    def cost_usd(self) -> float:
        return sum(run.cost_usd for run in self.runs)


class View(Protocol):
    def intro(self, root: Path, visible: bool) -> None: ...

    def starting(self) -> Any: ...

    def setup_progress(self, text: str, milestone: bool) -> None: ...

    def setup_done(self, text: str) -> None: ...

    def act(self, number: int, title: str, subtitle: str) -> None: ...

    def demo_step(self, step: DemoStep) -> None: ...

    def compiling(self) -> Any: ...

    def compiled(self, outcome: Outcome) -> None: ...

    def running(self, total: int, parallel: int) -> Any: ...

    def progress(self, finished: int, total: int, checked: int) -> None: ...

    def run_row(self, run: RunFacts, total: int) -> None: ...

    def runs_total(self, outcome: Outcome) -> None: ...

    def break_setup(self) -> None: ...

    def confirm(self, prompt: str) -> None: ...

    def breaking(self) -> Any: ...

    def break_result(self, broken: BreakFacts) -> None: ...

    def finale(self, outcome: Outcome, steps: NextSteps) -> None: ...

    def detail(self, lines: list[str]) -> None: ...


def _engine_detail_run(run: RunFacts, scene: Scene) -> list[str]:
    if not scene.screen_claims_success:
        return [
            f"run {run.index}: execution {run.execution_outcome}, transaction "
            f"{run.transaction_outcome}, record checks "
            f"{run.effects_confirmed}/{run.effects_required} confirmed through "
            f"OpenEMR's REST API, {run.records} chart(s) for this referral in "
            f"OpenEMR's database; report {run.report_md}",
        ]
    return [
        f"run {run.index}: execution {run.execution_outcome}, transaction "
        f"{run.transaction_outcome}, profile standard, record checks "
        f"{run.effects_confirmed}/{run.effects_required} confirmed by an "
        f"independent read of the database, {run.records} record(s) after the "
        f"run; report {run.report_md}",
    ]


def run_story(
    engine: Engine,
    view: View,
    *,
    next_steps: Callable[[Outcome], NextSteps],
    break_it: bool = True,
    interactive: bool = False,
    verbose: bool = False,
    open_page: Optional[Callable[[Path], bool]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """Run the three acts against ``engine`` and show them through ``view``."""

    outcome = Outcome(root=engine.root)
    scene = getattr(engine, "scene", MOCKMED)
    started = clock()
    paused = 0.0
    view.intro(engine.root, engine.visible)
    set_progress = getattr(engine, "set_progress", None)
    if set_progress is not None:
        set_progress(view.setup_progress)
    with view.starting():
        engine.__enter__()
    try:
        ready = getattr(engine, "setup_summary", None)
        if ready:
            view.setup_done(ready)
        # Act 1: show it once. Act 2's workers get ready meanwhile.
        engine.start_workers()
        view.act(1, "Show it once", _act1_subtitle(engine.visible, scene))
        act_start = clock()
        outcome.demo_steps = engine.record(view.demo_step)
        with view.compiling():
            outcome.program_steps = engine.compile()
        outcome.program_name = engine.program_name
        outcome.act1_seconds = clock() - act_start
        view.compiled(outcome)
        if verbose:
            view.detail(
                [
                    "recording "
                    f"{getattr(engine, 'recording_dir', engine.root / 'recording')}",
                    f"program {engine.root / 'bundle'} "
                    + getattr(
                        engine,
                        "program_detail",
                        "(certified under clinical-write; record checks mined "
                        "from the demonstration)",
                    ),
                ]
            )

        # Act 2: run it.
        inputs = getattr(engine, "inputs", RUN_NOTES)
        total = len(inputs)
        view.act(
            2,
            "Run it",
            _act2_subtitle(total, engine.note_param(), engine.parallel, scene),
        )
        act_start = clock()

        def finished(run: RunFacts) -> None:
            outcome.runs.append(run)
            view.run_row(run, total)
            if verbose:
                view.detail(_engine_detail_run(run, scene))
            view.progress(len(outcome.runs), total, outcome.checked)

        with view.running(total, engine.parallel):
            engine.run_all(inputs, finished)
        outcome.act2_seconds = clock() - act_start
        view.runs_total(outcome)
        # Sign the checked runs' reports while Act 3 runs; the finale waits.
        sealing = _in_background(lambda: engine.seal(list(outcome.runs)))

        # Act 3: break it.
        paging = None
        if break_it:
            view.act(3, "Break it", scene.act3_subtitle)
            view.break_setup()
            if interactive:
                waited = clock()
                engine.before_prompt()
                view.confirm(scene.confirm_prompt)
                paused += clock() - waited
            with view.breaking():
                outcome.broken = engine.break_it()
                broken = outcome.broken
                if outcome.runs:
                    # The page needs only the runs; build it while the
                    # screenshot is read.
                    paging = _in_background(
                        lambda: engine.write_page(outcome.runs[0], broken)
                    )
                broken.screen_text = engine.read_screen(broken)
            view.break_result(outcome.broken)
            if verbose:
                view.detail(scene.break_detail(outcome.broken))

        outcome.seal = sealing.result()
        if paging is not None:
            outcome.page = paging.result()
            if outcome.page is not None and open_page is not None:
                outcome.page_opened = bool(open_page(outcome.page))
    finally:
        engine.__exit__(None, None, None)
    outcome.total_seconds = clock() - started - paused
    view.finale(outcome, next_steps(outcome))
    return outcome


def _in_background(work: Callable[[], Any]) -> Any:
    """Start ``work`` on a thread; ``.result()`` waits and re-raises."""

    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quickstart")
    future = executor.submit(work)
    executor.shutdown(wait=False)
    return future


def _act2_subtitle(
    total: int, varied: bool, parallel: int, scene: Scene = MOCKMED
) -> str:
    # An engine that can't give a run its own note replays the demonstrated
    # one, and the header says so.
    what = (
        f"the program on {total} new {scene.inputs_noun}"
        if varied
        else f"the program {total} times"
    )
    if parallel >= total:
        return f"{what}, all at once"
    if parallel > 1:
        return f"{what}, {parallel} at a time"
    return what


def _act1_subtitle(visible: bool, scene: Scene = MOCKMED) -> str:
    if not scene.act1_live:
        return scene.act1_where
    where = "watch the browser window" if visible else "in a hidden browser"
    return f"{scene.act1_where}, {where}"


__all__ = [
    "YOUR_APP_URL",
    "display_path",
    "Engine",
    "NextSteps",
    "Outcome",
    "View",
    "run_story",
]
