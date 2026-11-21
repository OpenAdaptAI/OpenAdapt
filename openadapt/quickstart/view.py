"""How the quickstart looks: a short story in the terminal, or plain lines.

Five steps, each a plain-English line and a dim detail: the app starts, the
task is shown once, it becomes a program, the program runs on new inputs, and
the next save is broken on purpose. Then the person's Seal and one next step.

Both views say the same things in the same words (the helpers below). The
live view adds spinners that resolve to check marks, a card that ticks off
the read-back field by field, the screen and the record side by side, a
drawn Seal and a clickable link to it. The plain view prints one line per
fact with no color and no cursor moves, for logs, pipes, CI, ``NO_COLOR`` and
``TERM=dumb``. The engine's exact terms stay in each run's ``REPORT.md``.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from pathlib import Path
from typing import IO, Any, Iterator, Mapping, Optional, Sequence

from openadapt.quickstart.engine import BreakFacts, DemoStep, RunFacts
from openadapt.quickstart.notices import (
    Notices,
    collect_notices,
    key_line,
    plaintext_line,
)
from openadapt.quickstart.scene import MOCKMED, Scene
from openadapt.quickstart.seal_link import SealCard, read_card
from openadapt.quickstart.story import (
    REAL_SEALS_URL,
    NextSteps,
    Outcome,
    display_path,
)

#: Width of the run-note column, so times line up.
_LABEL_WIDTH = 40
#: The number of steps in the story (the app, show, compile, run, break).
STEPS = 5
#: Below this many columns the screen and the record stack instead of sitting
#: side by side.
_SIDE_BY_SIDE = 80
#: The command that prints every other next step.
MORE_COMMAND = "quickstart --more"
#: The pause before each read-back field ticks, on a live terminal only.
_TICK_S = 0.08


# ---------------------------------------------------------------------------
# Words shared by both views
# ---------------------------------------------------------------------------


class Marks:
    """The status marks, with ASCII stand-ins for terminals without them."""

    def __init__(self, encoding: Optional[str]) -> None:
        try:
            "✓■·—✗›◆●─".encode(encoding or "utf-8")
            unicode = True
        except (LookupError, UnicodeEncodeError):
            unicode = False
        self.ok = "✓" if unicode else "OK"
        self.stop = "■" if unicode else "STOP"
        self.dot = "·" if unicode else "-"
        self.no = "✗" if unicode else "X"
        self.arrow = "›" if unicode else ">"
        self.save = "◆" if unicode else "*"
        self.step = "●" if unicode else "o"
        self.line = "─" if unicode else "-"


def seconds(value: float) -> str:
    return f"{value:.1f}s"


def compiled_line(outcome: Outcome, scene: Scene = MOCKMED) -> str:
    return scene.compiled_line(outcome.program_steps)


def run_status(run: RunFacts, marks: Marks, scene: Scene = MOCKMED) -> str:
    if run.checked:
        again = " (on a second try)" if run.repeated else ""
        return f"{marks.ok} {scene.checked_in}{again}"
    return f"{marks.stop} {run.plain_label or run.execution_outcome}"


def runs_total_line(outcome: Outcome, marks: Marks) -> str:
    dot = f" {marks.dot} "
    return dot.join(
        [
            f"{outcome.checked} of {len(outcome.runs)} saved and checked",
            f"{outcome.model_calls} AI calls",
            f"${outcome.cost_usd:.2f}",
            seconds(outcome.act2_seconds),
        ]
    )


def database_line(broken: BreakFacts, scene: Scene = MOCKMED) -> str:
    return scene.database_line(broken)


def bot_verdict(broken: BreakFacts, marks: Marks) -> Optional[str]:
    """What a tool that trusts the screen would report, when the screen said so."""

    return f"{marks.ok} Done" if broken.screen_showed_success else None


def openadapt_verdict(broken: BreakFacts, marks: Marks, scene: Scene = MOCKMED) -> str:
    if broken.stopped:
        return f"{marks.stop} {scene.verdict(broken)}"
    return f"{broken.execution_outcome.title()} (expected a stop)"


def why_line(broken: BreakFacts, scene: Scene = MOCKMED) -> str:
    return scene.why_line(broken)


def aha_line(broken: BreakFacts, scene: Scene = MOCKMED) -> Optional[str]:
    """The one-line summary, only when the run showed all of it."""

    return scene.aha_line(broken)


def reconcile_line(broken: BreakFacts) -> Optional[str]:
    """The engine's own next step for a stop that needs a person, when it does."""

    if broken.transaction_outcome != "RECONCILIATION_REQUIRED":
        return None
    return (
        "A save may still have gone through, so a person checks the record "
        "before anything is retried."
    )


def ready_detail(scene: Scene, summary: Optional[str]) -> str:
    return summary or scene.ready_detail


def show_detail(steps: Sequence[DemoStep], subtitle: str) -> str:
    """``6 actions, in a hidden browser``: the count, then where it happened."""

    count = f"{len(steps)} action" + ("" if len(steps) == 1 else "s")
    where = subtitle.rpartition(", ")[2]
    return f"{count}, {where}" if where else count


def action_chain(steps: Sequence[DemoStep], marks: Marks) -> str:
    """The demonstrated actions in order: ``Open › New Encounter › …``."""

    def short(label: str) -> str:
        return label[len("Click ") :] if label.startswith("Click ") else label

    return f" {marks.arrow} ".join(short(step.label) for step in steps)


def show_seconds(steps: Sequence[DemoStep]) -> float:
    return steps[-1].seconds if steps else 0.0


def compile_seconds(outcome: Outcome, scene: Scene = MOCKMED) -> float:
    # A live demonstration is timed inside Act 1; a recorded one (OpenEMR)
    # carries the recording's own times, so Act 1 is the compile alone.
    if not scene.act1_live:
        return outcome.act1_seconds
    return max(0.0, outcome.act1_seconds - show_seconds(outcome.demo_steps))


def program_detail(outcome: Outcome, scene: Scene) -> str:
    noun = "step" if outcome.program_steps == 1 else "steps"
    detail = f"{outcome.program_steps} {noun}"
    if outcome.program_name:
        detail += f", {outcome.program_name}"
    return detail if scene.act1_live else f"{detail}, certified under clinical-write"


def run_heading(subtitle: str) -> tuple[str, str]:
    """Act 2's step line from the story's subtitle.

    ``"the program on 5 new notes, all at once"`` becomes ``("Run it on 5 new
    notes", "all at once")``.
    """

    what, _, how = subtitle.partition(", ")
    what = what.removeprefix("the program ")
    return f"Run it {what}".rstrip(), how


def all_clear_runs(outcome: Outcome) -> bool:
    return bool(outcome.runs) and outcome.checked == len(outcome.runs)


def all_clear(outcome: Outcome) -> bool:
    broke_ok = outcome.broken is None or outcome.broken.stopped
    return all_clear_runs(outcome) and broke_ok


def clean_title(outcome: Outcome) -> str:
    if all_clear_runs(outcome):
        return "Saved and checked"
    return f"{outcome.checked} of {len(outcome.runs)} saved and checked"


def readback_run(outcome: Outcome) -> Optional[RunFacts]:
    """The first run whose record checks all confirmed."""

    return next(
        (
            run
            for run in sorted(outcome.runs, key=lambda r: r.index)
            if run.checked and run.effects_confirmed == run.effects_required
        ),
        None,
    )


def readback_rows(outcome: Outcome) -> list[tuple[str, str]]:
    """That run's record, field by field, as its record check matched it.

    A field bound to the run's input shows that run's input.
    """

    run = readback_run(outcome)
    if run is None:
        return []
    rows = []
    for name, literal, param in outcome.readback:
        if literal is not None:
            rows.append((name, literal))
        elif param is not None:
            rows.append((name, f'"{run.note}"'))
    return rows


def readback_heading(run: RunFacts, scene: Scene) -> str:
    where = scene.record_title
    if where.startswith("The "):
        where = "the " + where[4:]
    return f"The record, read back from {where}"


def clean_footer(outcome: Outcome, marks: Marks) -> str:
    dot = f" {marks.dot} "
    return dot.join(
        [
            (
                f"{outcome.checked} of {len(outcome.runs)} runs"
                if len(outcome.runs) != 1
                else f"{outcome.checked} of 1 run"
            ),
            f"{outcome.model_calls} model calls",
            f"${outcome.cost_usd:.2f}",
            seconds(outcome.act2_seconds),
        ]
    )


def seal_card(outcome: Outcome) -> Optional[SealCard]:
    page = outcome.seal_pages.verified
    if outcome.seal is None or page is None:
        return None
    return read_card(page.parent)


def card_outcome(card: Optional[SealCard]) -> str:
    if card is None:
        return "Signed"
    return "Saved and checked" if card.outcome == "VERIFIED" else card.outcome.title()


def seal_rows(outcome: Outcome, card: Optional[SealCard], marks: Marks) -> list[str]:
    """The Seal's lines: what it says, then how it is signed."""

    dot = f" {marks.dot} "
    lines = []
    if card is not None:
        checks = "record check" + ("" if card.effects_required == 1 else "s")
        lines.append(
            dot.join(
                [
                    f"{card.steps_ok} of {card.steps_total} steps",
                    f"{card.effects_confirmed} of {card.effects_required} {checks}",
                    f"{card.model_calls} model calls",
                ]
            )
        )
        lines.append(f"ed25519 over report sha256 {card.short_digest}")
    if outcome.seal is not None:
        reports = "report" if outcome.seal.sealed == 1 else "reports"
        lines.append(
            f"key {outcome.seal.key_id}{dot}{outcome.seal.sealed} {reports} signed"
        )
    return lines


def seal_lines(outcome: Outcome) -> list[tuple[str, str]]:
    """The person's own Seal page, then where the Seals of real runs are."""

    page = outcome.seal_pages.verified
    if page is None:
        return []
    return [
        ("Your Seal", str(display_path(page))),
        ("See real Seals from OpenEMR runs", REAL_SEALS_URL),
    ]


def seal_page_note(outcome: Outcome) -> str:
    note = "checks the signature in your browser, offline; nothing is uploaded"
    if outcome.seal_page_opened:
        return "opened in your browser; it " + note
    return note


def publish_runs(outcome: Outcome) -> list[Path]:
    """The runs whose Seals the finale offers: the first checked run, then the
    stopped run when Act 3 signed one. The stopped run is the point of the
    quickstart, so it goes online with the checked run or not at all."""

    page = outcome.seal_pages.verified
    if outcome.seal is None or page is None:
        return []
    runs = [page.parent]
    broken = outcome.seal_pages.broken
    if broken is not None and (broken.parent / "seal.json").is_file():
        runs.append(broken.parent)
    return runs


def next_step(steps: NextSteps) -> tuple[str, str, str]:
    """The one next step: its title, the command, and a dim alternative."""

    return (
        "Give your agent this task as a tool",
        f"claude mcp add openadapt -- {steps.agent}",
        f"Any other MCP client: {steps.agent}",
    )


def failure_step(outcome: Outcome) -> tuple[str, str]:
    """When something didn't check out: the report to read first."""

    failed = next((run for run in outcome.runs if not run.checked), None)
    if failed is not None:
        return (
            "A run didn't check out. Read its report",
            str(display_path(failed.report_md)),
        )
    if outcome.broken is not None:
        return (
            "The broken save didn't stop the program. Read its report",
            str(display_path(outcome.broken.report_md)),
        )
    return ("Nothing ran. Read the reports", str(display_path(outcome.root)))


def more_hint(steps: NextSteps) -> str:
    """``openadapt quickstart --more``, spelled like the other commands."""

    agent = steps.agent
    at = agent.find("agent serve")
    head = agent[:at] if at > 0 else "openadapt "
    return f"{head}{MORE_COMMAND}"


def details_line(outcome: Outcome) -> str:
    """One line: where every run's full report is, and the side-by-side page."""

    root = display_path(outcome.root)
    line = f"Done in {seconds(outcome.total_seconds)}. Reports: {root}, one REPORT.md per run"
    if outcome.page is not None:
        page = display_path(outcome.page)
        line += f"; side by side: {page}"
        if outcome.page_opened:
            line += ", opened in your browser"
    return line


def more_lines(steps: NextSteps, root: Path, seal_page: Optional[Path]) -> list[str]:
    """``openadapt quickstart --more``: every next step, one per line."""

    title, command, _ = next_step(steps)
    lines = [
        f"{title} (Claude Code): {command}",
        f"{title} (any MCP client): {steps.agent}",
        f"Try it on your own app: {steps.record}",
    ]
    if steps.verify:
        lines.append(f"Check every Seal again: {steps.verify}")
    if seal_page is not None:
        lines.append(f"Open your Seal (offline): {display_path(seal_page)}")
    lines.append(f"See real Seals from OpenEMR runs: {REAL_SEALS_URL}")
    lines.append(f"Read what each run did: {display_path(root)}, one REPORT.md per run")
    if steps.keep:
        lines.append(f"Keep it installed: {steps.keep}")
    if steps.shadow:
        lines.append(steps.shadow)
    return lines


class _Base:
    """What both views share: the notices and the steps seen so far."""

    scene: Scene
    marks: Marks

    def _init_shared(self) -> None:
        self._notices: Optional[Notices] = None
        self._ready: Optional[str] = None
        self._demo: list[DemoStep] = []
        self._show_subtitle = ""
        self._expected: list[str] = []

    @contextlib.contextmanager
    def collect_notices(self) -> Iterator[None]:
        """Collect the engine's plaintext-write notices while the story runs."""

        with collect_notices() as notices:
            self._notices = notices
            yield

    def _plaintext(self, root: Path) -> list[str]:
        """The finale's at-rest lines: an unencrypted folder, a new key."""
        paths = self._notices.paths if self._notices else []
        created = self._notices.key_created if self._notices else None
        lines = (plaintext_line(paths, display_path(root)), key_line(created))
        return [line for line in lines if line]

    def expect_runs(self, inputs: Sequence[str]) -> None:
        self._expected = list(inputs)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Choosing a view
# ---------------------------------------------------------------------------


def wants_plain(stream: IO[str], environ: Optional[Mapping[str, str]] = None) -> bool:
    """Plain lines when the output isn't a terminal or color is turned off."""

    env = os.environ if environ is None else environ
    if "NO_COLOR" in env and env.get("NO_COLOR", "") != "":
        return True
    if env.get("TERM") == "dumb":
        return True
    isatty = getattr(stream, "isatty", None)
    return not (callable(isatty) and isatty())


def make_view(
    stream: Optional[IO[str]] = None, *, environ=None, scene: Scene = MOCKMED
) -> Any:
    stream = stream or sys.stdout
    if wants_plain(stream, environ):
        return PlainView(stream, scene=scene)
    try:
        return RichView(stream, environ=environ, scene=scene)
    except ImportError:  # rich is a launcher dependency; plain lines still work
        return PlainView(stream, scene=scene)


# ---------------------------------------------------------------------------
# Plain lines
# ---------------------------------------------------------------------------


class PlainView(_Base):
    """One line per fact, printed as it happens. No color, no cursor moves."""

    def __init__(self, stream: IO[str], *, scene: Scene = MOCKMED) -> None:
        self.stream = stream
        self.scene = scene
        self.marks = Marks(getattr(stream, "encoding", None))
        self._init_shared()

    def _say(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _step(self, number: int, text: str) -> None:
        self._say(f"[{number}/{STEPS}] {text}")

    @contextlib.contextmanager
    def _quiet(self) -> Iterator[None]:
        yield

    def _flush_ready(self) -> None:
        if self._ready is not None:
            dot = self.marks.dot
            self._step(1, f"{self.marks.ok} {self.scene.ready} {dot} {self._ready}")
            self._ready = None

    def intro(self, root: Path, visible: bool) -> None:
        self._say(f"OpenAdapt quickstart: {self.scene.intro}")
        self._say(f"Synthetic data only. Files go to {display_path(root)}")
        self._say()

    @contextlib.contextmanager
    def starting(self) -> Iterator[None]:
        yield
        self._ready = ready_detail(self.scene, None)

    def setup_progress(self, text: str, milestone: bool) -> None:
        if milestone:
            self._say(f"      {text}")

    def setup_done(self, text: str) -> None:
        self._ready = ready_detail(self.scene, text)

    def act(self, number: int, title: str, subtitle: str) -> None:
        if self._ready is None and number == 1:
            self._ready = ready_detail(self.scene, None)
        self._flush_ready()
        dot = self.marks.dot
        if number == 1:
            self._step(2, f"Show it once {dot} {subtitle}")
        elif number == 2:
            heading, how = run_heading(subtitle)
            self._step(4, f"{heading} {dot} {how}" if how else heading)
        else:
            self._say()
            self._step(5, self.scene.break_title)

    def demo_step(self, step: DemoStep) -> None:
        self._demo.append(step)
        self._say(
            f"      {self.marks.ok} {step.label:<{_LABEL_WIDTH}} {seconds(step.seconds)}"
        )

    def compiling(self):
        return self._quiet()

    def compiled(self, outcome: Outcome) -> None:
        self._step(
            3,
            f"{self.marks.ok} Turn it into a program {self.marks.dot} "
            f"{program_detail(outcome, self.scene)}  {seconds(compile_seconds(outcome, self.scene))}",
        )

    def running(self, total: int, parallel: int):
        return self._quiet()

    def progress(self, finished: int, total: int, checked: int) -> None:
        pass

    def run_row(self, run: RunFacts, total: int) -> None:
        self._say(
            f"      {run.index}/{total}  {run.note:<{_LABEL_WIDTH - 6}} "
            f"{run_status(run, self.marks, self.scene)}  {seconds(run.seconds)}"
        )

    def runs_total(self, outcome: Outcome) -> None:
        self._say(f"      {runs_total_line(outcome, self.marks)}")
        self._say()
        mark = self.marks.ok if all_clear_runs(outcome) else self.marks.stop
        self._say(f"{mark} {clean_title(outcome)}")
        rows = readback_rows(outcome)
        run = readback_run(outcome)
        if rows and run is not None:
            self._say(f"  {readback_heading(run, self.scene)}:")
            width = max(len(name) for name, _ in rows)
            for name, value in rows:
                self._say(f"    {self.marks.ok} {name:<{width}}  {value}")
        self._say(f"  {clean_footer(outcome, self.marks)}")

    def break_setup(self) -> None:
        self._say(f"      {self.scene.break_setup}")

    def _ask(self, prompt: str) -> str:
        self.stream.write(f"  {prompt}")
        self.stream.flush()
        return input()

    def _publish(self, outcome: Outcome, steps: NextSteps) -> None:
        runs = publish_runs(outcome)
        if runs and steps.offer_publish is not None:
            steps.offer_publish(runs, lambda line: self._say(f"  {line}"), self._ask)

    def confirm(self, prompt: str) -> None:
        self.stream.write(f"      {prompt} ")
        self.stream.flush()
        try:
            input()
        except EOFError:
            self._say()

    def breaking(self):
        return self._quiet()

    def break_result(self, broken: BreakFacts) -> None:
        scene = self.scene
        labels = (
            "The screen:",
            f"{scene.record_title}:",
            "A screen-trusting bot:",
            "OpenAdapt:",
        )
        width = max(len(label) for label in labels) + 2
        screen, record, bot_label, openadapt = (f"{label:<{width}}" for label in labels)
        self._say(f"      {screen}{scene.screen_line(broken)}")
        self._say(f"      {record}{database_line(broken, scene)}")
        bot = bot_verdict(broken, self.marks)
        if bot:
            self._say(f"      {bot_label}{bot}")
        self._say(
            f"      {openadapt}{openadapt_verdict(broken, self.marks, scene)}"
            f"  {seconds(broken.seconds)}"
        )
        self._say(f"      Why: {why_line(broken, scene)}")
        reconcile = reconcile_line(broken)
        if reconcile:
            self._say(f"      {reconcile}")
        aha = aha_line(broken, scene)
        if aha:
            self._say()
            self._say(aha)

    def finale(self, outcome: Outcome, steps: NextSteps) -> None:
        self._say()
        card = seal_card(outcome)
        if outcome.seal is not None:
            self._say(f"Your Seal: {card_outcome(card)}")
            for line in seal_rows(outcome, card, self.marks):
                self._say(f"  {line}")
            page = outcome.seal_pages.verified
            if page is not None:
                self._say(f"  Open it: {display_path(page)}")
                self._say(f"    {seal_page_note(outcome)}")
            self._say(f"  Real Seals from OpenEMR runs: {REAL_SEALS_URL}")
            self._publish(outcome, steps)
            self._say()
        if all_clear(outcome):
            title, command, other = next_step(steps)
            self._say(f"Next: {title}")
            self._say(f"  {command}")
            self._say(f"  {other}")
        else:
            title, path = failure_step(outcome)
            self._say(f"Next: {title}")
            self._say(f"  {path}")
        self._say(f"More next steps: {more_hint(steps)}")
        self._say(details_line(outcome))
        for notice in self._plaintext(outcome.root):
            self._say(notice)
        if steps.shadow:
            self._say(steps.shadow)

    def detail(self, lines: list[str]) -> None:
        for line in lines:
            self._say(f"      {line}")


# ---------------------------------------------------------------------------
# Live terminal
# ---------------------------------------------------------------------------


class _Spinner:
    """One live, transient step line: a spinner, the step, a dim detail."""

    def __init__(self, view: "RichView", title: str, detail: str = "") -> None:
        self.view = view
        self.title = title
        self.detail = detail
        self.live: Any = None
        self.spinner: Any = None

    def _render(self) -> Any:
        from rich.table import Table

        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(overflow="ellipsis", no_wrap=True)
        text = f"[bold]{_escape(self.title)}[/]"
        if self.detail:
            text += f"  [dim]{_escape(self.detail)}[/]"
        grid.add_row(self.spinner, text)
        return grid

    def start(self) -> "_Spinner":
        if not self.view.animate:
            return self
        from rich.live import Live
        from rich.spinner import Spinner

        self.spinner = Spinner("dots", style="cyan")
        self.live = Live(
            get_renderable=self._render,
            console=self.view.console,
            refresh_per_second=12.5,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self.live.start()
        return self

    def update(self, detail: str) -> None:
        self.detail = detail

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None


class RichView(_Base):
    """Spinners that resolve to checks, a read-back card, a drawn Seal."""

    def __init__(
        self,
        stream: IO[str],
        *,
        environ=None,
        width: Optional[int] = None,
        scene: Scene = MOCKMED,
        animate: Optional[bool] = None,
        console: Any = None,
    ) -> None:
        from rich.console import Console

        from openadapt.quickstart.images import detect_image_protocol

        self.console = console or Console(
            file=stream, highlight=False, soft_wrap=False, width=width
        )
        self.scene = scene
        self.marks = Marks(getattr(stream, "encoding", None) or "utf-8")
        self.image_protocol = detect_image_protocol(environ, isatty=True)
        if animate is None:
            isatty = getattr(stream, "isatty", None)
            animate = bool(callable(isatty) and isatty() and self.console.is_terminal)
        #: Spinners and the read-back ticks; off for recordings and tests.
        self.animate = animate
        self._spinner: Optional[_Spinner] = None
        self._runs: dict[int, RunFacts] = {}
        self._run_spinner: Any = None
        self._run_title = ""
        self._run_detail = ""
        self._total = 0
        self._init_shared()

    # -- helpers ----------------------------------------------------------

    def _pause(self, value: float) -> None:
        if self.animate:
            time.sleep(value)

    def _spin(self, title: str, detail: str = "") -> _Spinner:
        self._stop_spin()
        self._spinner = _Spinner(self, title, detail).start()
        return self._spinner

    def _stop_spin(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def close(self) -> None:
        self._stop_spin()

    @contextlib.contextmanager
    def _spinning(self, title: str, detail: str = "") -> Iterator[None]:
        self._spin(title, detail)
        try:
            yield
        finally:
            self._stop_spin()

    def _step_line(
        self,
        mark: str,
        style: str,
        title: str,
        detail: str = "",
        took: Optional[float] = None,
    ) -> None:
        from rich.table import Table

        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        text = f"[bold]{_escape(title)}[/]"
        if detail:
            text += f"  [dim]{_escape(detail)}[/]"
        grid.add_row(
            f"[{style}]{mark}[/]",
            text,
            f"[dim]{seconds(took)}[/]" if took is not None else "",
        )
        self.console.print(grid)

    def _flush_ready(self) -> None:
        if self._ready is not None:
            self._step_line(self.marks.ok, "green", self.scene.ready, self._ready)
            self._ready = None

    def _link(self, path: Path, text: str) -> str:
        try:
            uri = path.resolve().as_uri()
        except ValueError:
            return _escape(text)
        return f"[link={uri}]{_escape(text)}[/link]"

    # -- the story --------------------------------------------------------

    def intro(self, root: Path, visible: bool) -> None:
        from rich.rule import Rule

        intro = self.scene.intro
        self.console.print()
        self.console.print(
            f"[bold cyan]{self.marks.save}[/] [bold]OpenAdapt[/] [dim]quickstart[/]"
        )
        self.console.print(_escape(intro[:1].upper() + intro[1:]))
        self.console.print(
            Rule(
                f"[dim]synthetic data {self.marks.dot} files in "
                f"{_escape(str(display_path(root)))}[/]",
                style="dim",
                align="right",
            )
        )

    def starting(self):
        return self._spinning(self.scene.starting)

    def setup_progress(self, text: str, milestone: bool) -> None:
        if self._spinner is not None:
            self._spinner.update(text)

    def setup_done(self, text: str) -> None:
        self._ready = ready_detail(self.scene, text)

    def act(self, number: int, title: str, subtitle: str) -> None:
        if number == 1:
            if self._ready is None:
                self._ready = ready_detail(self.scene, None)
            self._flush_ready()
            self._show_subtitle = subtitle
            self._spin("Show it once", subtitle)
        elif number == 2:
            self._run_title, self._run_detail = run_heading(subtitle)
        else:
            self.console.print()

    def demo_step(self, step: DemoStep) -> None:
        self._demo.append(step)
        if self._spinner is not None:
            self._spinner.update(f"{len(self._demo)}. {step.label}")

    @contextlib.contextmanager
    def compiling(self) -> Iterator[None]:
        self._stop_spin()
        self._step_line(
            self.marks.ok,
            "green",
            "Show it once",
            show_detail(self._demo, self._show_subtitle),
            show_seconds(self._demo),
        )
        if self._demo:
            from rich.padding import Padding

            self.console.print(
                Padding(
                    f"[dim]{_escape(action_chain(self._demo, self.marks))}[/]",
                    (0, 0, 0, 3),
                )
            )
        with self._spinning("Turn it into a program", "compiling the demonstration"):
            yield

    def compiled(self, outcome: Outcome) -> None:
        self._step_line(
            self.marks.ok,
            "green",
            "Turn it into a program",
            program_detail(outcome, self.scene),
            compile_seconds(outcome, self.scene),
        )

    # Act 2: every run's row is on screen from the start and resolves.

    def _run_text(self, run: RunFacts) -> str:
        if run.checked:
            mark = f"[green]{self.marks.ok}[/]"
            status = "checked" + (" on a second try" if run.repeated else "")
        else:
            mark = f"[red]{self.marks.stop}[/]"
            status = run.plain_label or run.execution_outcome
        return (
            f"   {mark} {_escape(run.note):<{_LABEL_WIDTH - 6}} "
            f"[dim]{_escape(status)} {seconds(run.seconds)}[/]"
        )

    def _runs_render(self) -> Any:
        from rich.table import Table

        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(no_wrap=True, overflow="ellipsis")
        finished = len(self._runs)
        checked = sum(1 for run in self._runs.values() if run.checked)
        dot = self.marks.dot
        grid.add_row(
            self._run_spinner,
            f"[bold]{_escape(self._run_title)}[/]  [dim]{_escape(self._run_detail)} "
            f"{dot} {finished} of {self._total} done {dot} {checked} checked[/]",
        )
        for index in range(1, self._total + 1):
            run = self._runs.get(index)
            if run is not None:
                grid.add_row("", self._run_text(run)[1:])
                continue
            note = (
                self._expected[index - 1]
                if index - 1 < len(self._expected)
                else f"run {index}"
            )
            grid.add_row("", f"  [dim]{dot} {_escape(note)}[/]")
        return grid

    @contextlib.contextmanager
    def running(self, total: int, parallel: int) -> Iterator[None]:
        self._total = total
        self._runs = {}
        live = None
        if self.animate:
            from rich.live import Live
            from rich.spinner import Spinner

            self._run_spinner = Spinner("dots", style="cyan")
            live = Live(
                get_renderable=self._runs_render,
                console=self.console,
                refresh_per_second=12.5,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            live.start()
        try:
            yield
        finally:
            if live is not None:
                live.stop()

    def progress(self, finished: int, total: int, checked: int) -> None:
        pass

    def run_row(self, run: RunFacts, total: int) -> None:
        self._runs[run.index] = run

    def runs_total(self, outcome: Outcome) -> None:
        clean = all_clear_runs(outcome)
        checked = f"{outcome.checked} of {len(outcome.runs)} {self.scene.checked_in}"
        detail = (
            f"{self._run_detail} {self.marks.dot} {checked}"
            if self._run_detail
            else checked
        )
        self._step_line(
            self.marks.ok if clean else self.marks.stop,
            "green" if clean else "red",
            self._run_title,
            detail,
            outcome.act2_seconds,
        )
        for run in sorted(outcome.runs, key=lambda r: r.index):
            self.console.print(self._run_text(run))
        self._clean_card(outcome)

    def _clean_card(self, outcome: Outcome) -> None:
        from rich.panel import Panel
        from rich.table import Table

        clean = all_clear_runs(outcome)
        rows = readback_rows(outcome)
        run = readback_run(outcome)
        tone = "green" if clean else "red"

        def card(shown: int) -> Any:
            from rich.console import Group

            parts: list[Any] = []
            if rows and run is not None:
                parts.append(f"[dim]{_escape(readback_heading(run, self.scene))}[/]")
                fields = Table.grid(padding=(0, 2))
                fields.add_column(no_wrap=True)
                fields.add_column(overflow="fold")
                for i, (name, value) in enumerate(rows):
                    mark = (
                        f"[green]{self.marks.ok}[/]"
                        if i < shown
                        else f"[dim]{self.marks.dot}[/]"
                    )
                    fields.add_row(
                        f"  {mark} [dim]{_escape(name)}[/]",
                        f"[bold]{_escape(value)}[/]",
                    )
                parts.extend([fields, ""])
            parts.append(
                f"[bold {tone}]{_escape(clean_footer(outcome, self.marks))}[/]"
            )
            return Panel(
                Group(*parts),
                title=f"[bold {tone}] {_escape(clean_title(outcome))} [/]",
                title_align="left",
                border_style=tone,
                expand=False,
                padding=(1, 2),
            )

        self.console.print()
        if self.animate and rows:
            from rich.live import Live

            with Live(
                card(0),
                console=self.console,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                for shown in range(1, len(rows) + 1):
                    time.sleep(_TICK_S)
                    live.update(card(shown), refresh=True)
        else:
            self.console.print(card(len(rows)))

    def break_setup(self) -> None:
        self._step_line(
            self.marks.arrow,
            "bold yellow",
            self.scene.break_title,
            self.scene.break_setup,
        )

    def _ask(self, prompt: str) -> str:
        return self.console.input(f"[bold]{_escape(prompt)}[/]")

    def confirm(self, prompt: str) -> None:
        try:
            self.console.input(f"   [bold]{prompt}[/] [dim](Enter)[/] ")
        except EOFError:
            self.console.print()

    def breaking(self):
        return self._spinning(self.scene.break_title, self.scene.breaking)

    def break_result(self, broken: BreakFacts) -> None:
        drawn = False
        if self.image_protocol and broken.screenshot is not None:
            drawn = self._draw_beside_screenshot(broken, self._record_lines(broken))
        if not drawn:
            self._side_by_side(broken)
        self._pause(0.25)
        self._verdicts(broken)

    def _record_text(self, broken: BreakFacts) -> str:
        """The record's line: a red cross only where the screen claimed a save."""

        line = _escape(database_line(broken, self.scene))
        if self.scene.screen_claims_success:
            return f"[bold red]{self.marks.no} {line}[/]"
        return f"[bold]{line}[/]"

    def _record_lines(self, broken: BreakFacts) -> list[str]:
        return [
            f"[bold]{_escape(self.scene.record_title)}[/]",
            self._record_text(broken),
        ]

    def _side_by_side(self, broken: BreakFacts) -> None:
        from rich.panel import Panel
        from rich.table import Table

        scene = self.scene
        quote = scene.screen_words(broken)
        claims = scene.screen_claims_success and broken.screen_showed_success
        screen = Panel(
            f"[green]{self.marks.ok}[/] {_escape(quote)}" if claims else _escape(quote),
            title="[bold]The screen[/]",
            title_align="left",
            border_style="green" if claims else "dim",
            padding=(1, 2),
        )
        record = Panel(
            self._record_text(broken),
            title=f"[bold]{_escape(scene.record_title)}[/]",
            title_align="left",
            border_style="red" if scene.screen_claims_success else "dim",
            padding=(1, 2),
        )
        self.console.print()
        if self.console.width >= _SIDE_BY_SIDE:
            half = (self.console.width - 2) // 2
            options = self.console.options.update(width=half)
            height = max(
                len(self.console.render_lines(panel, options, pad=False))
                for panel in (screen, record)
            )
            screen.height = record.height = height
            grid = Table.grid(padding=(0, 2), expand=True)
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            grid.add_row(screen, record)
            self.console.print(grid)
        else:
            self.console.print(screen)
            self.console.print(record)

    def _verdicts(self, broken: BreakFacts) -> None:
        from rich.table import Table

        scene = self.scene
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column()
        bot = bot_verdict(broken, self.marks)
        if bot:
            grid.add_row("[dim]A screen-trusting bot[/]", f"[red]{_escape(bot)}[/]")
        verdict = openadapt_verdict(broken, self.marks, scene)
        style = "bold yellow" if broken.stopped else "bold red"
        grid.add_row(
            "[bold]OpenAdapt[/]",
            f"[{style}]{_escape(verdict)}[/]  [dim]{seconds(broken.seconds)}[/]",
        )
        self.console.print(grid)
        self.console.print(f"[dim]{_escape(why_line(broken, scene))}[/]")
        reconcile = reconcile_line(broken)
        if reconcile:
            self.console.print(f"[dim]{_escape(reconcile)}[/]")
        # Where the screen showed no false success (OpenEMR), the side by
        # side doesn't say what replaying the clicks would have done.
        aha = None if scene.screen_claims_success else aha_line(broken, scene)
        if aha:
            self.console.print()
            self.console.print(_escape(aha))

    def _draw_beside_screenshot(self, broken: BreakFacts, right: list[str]) -> bool:
        from openadapt.quickstart.images import beside_image, crop_png, image_sequence

        try:
            png = crop_png(broken.screenshot, self.scene.screen_crop)
        except Exception:  # noqa: BLE001 - fall back to the text boxes
            return False
        cols, rows = 40, 10
        rendered = [self._ansi(line) for line in right]
        self.console.print()
        self.console.print("[bold]The screen[/] [dim](its screenshot)[/]")
        block = beside_image(
            image_sequence(png, self.image_protocol, cols=cols, rows=rows),
            rendered,
            indent=3,
            cols=cols,
            rows=rows,
        )
        self.console.file.write(block)
        self.console.file.flush()
        return True

    def _ansi(self, markup: str) -> str:
        from rich.console import Console

        buffer = io.StringIO()
        Console(
            file=buffer,
            force_terminal=True,
            color_system=self.console.color_system,
            width=200,
            highlight=False,
        ).print(markup, end="")
        return buffer.getvalue()

    def _seal_emblem(self, card: Optional[SealCard], steps: int) -> str:
        """One pip per program step; the save, read back, is the diamond."""

        total = card.steps_total if card is not None else steps
        if total <= 0:
            return ""
        pips = [f"[green]{self.marks.step}[/]"] * (total - 1)
        pips.append(f"[bold green]{self.marks.save}[/]")
        return f"[dim]{self.marks.line}[/]".join(pips)

    def _seal(self, outcome: Outcome, card: Optional[SealCard]) -> None:
        from rich.panel import Panel
        from rich.table import Table

        body = Table.grid()
        body.add_column(overflow="fold")
        body.add_row(f"[bold green]{self.marks.ok} {_escape(card_outcome(card))}[/]")
        emblem = self._seal_emblem(card, outcome.program_steps)
        if emblem:
            body.add_row(emblem)
        body.add_row("")
        for line in seal_rows(outcome, card, self.marks):
            body.add_row(f"[dim]{_escape(line)}[/]")
        self.console.print(
            Panel(
                body,
                title=f"[bold cyan] {self.marks.save} Your Seal [/]",
                title_align="left",
                border_style="cyan",
                expand=False,
                padding=(1, 3),
            )
        )
        links = Table.grid(padding=(0, 2))
        links.add_column(no_wrap=True)
        links.add_column(overflow="fold")
        page = outcome.seal_pages.verified
        if page is not None:
            links.add_row(
                "[bold]Open it[/]",
                f"[bold cyan]{self._link(page, str(display_path(page)))}[/]",
            )
            links.add_row("", f"[dim]{_escape(seal_page_note(outcome))}[/]")
        links.add_row(
            "[dim]Real Seals[/]",
            f"[cyan][link={REAL_SEALS_URL}]{REAL_SEALS_URL}[/link][/]",
        )
        self.console.print(links)

    def finale(self, outcome: Outcome, steps: NextSteps) -> None:
        from rich.panel import Panel
        from rich.rule import Rule

        self._stop_spin()
        self.console.print()
        if outcome.seal is not None:
            self._seal(outcome, seal_card(outcome))
            runs = publish_runs(outcome)
            if runs and steps.offer_publish is not None:
                spoken: list[bool] = []

                def say(line: str) -> None:
                    # A blank line before the offer, only if there is one.
                    if not spoken:
                        spoken.append(True)
                        self.console.print()
                    self.console.print(_escape(line), soft_wrap=True)

                steps.offer_publish(runs, say, self._ask)
            self.console.print()
        self.console.print(Rule(style="cyan"))
        if all_clear(outcome):
            title, command, other = next_step(steps)
            self.console.print(f"[bold cyan]Next[/]  [bold]{_escape(title)}[/]")
            if self.console.width >= len(command) + 6:
                self.console.print(
                    Panel(
                        f"[bold]{_escape(command)}[/]",
                        border_style="cyan",
                        expand=False,
                        padding=(0, 2),
                    )
                )
            else:
                # Too narrow for a box: one line the terminal wraps, so a
                # copy gets the command without border characters.
                self.console.print(
                    f"[bold cyan]{self.marks.arrow}[/] [bold]{_escape(command)}[/]",
                    soft_wrap=True,
                )
            self.console.print(f"[dim]{_escape(other)}[/]")
        else:
            title, path = failure_step(outcome)
            self.console.print(f"[bold red]Next[/]  [bold]{_escape(title)}[/]")
            self.console.print(f"  [bold]{_escape(path)}[/]")
        self.console.print(
            f"[dim]More next steps:[/] [cyan]{_escape(more_hint(steps))}[/]"
        )
        self.console.print()
        self.console.print(f"[dim]{_escape(details_line(outcome))}[/]")
        for notice in self._plaintext(outcome.root):
            self.console.print(f"[dim]{_escape(notice)}[/]")
        if steps.shadow:
            self.console.print(f"[yellow]{_escape(steps.shadow)}[/]")

    def detail(self, lines: list[str]) -> None:
        for line in lines:
            self.console.print(f"   [dim]{_escape(line)}[/]")


def _escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


__all__ = [
    "MORE_COMMAND",
    "STEPS",
    "Marks",
    "PlainView",
    "RichView",
    "aha_line",
    "make_view",
    "more_lines",
    "seal_lines",
    "wants_plain",
]
