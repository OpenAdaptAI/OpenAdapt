"""How the quickstart looks: a live terminal view, or plain lines.

Both views say the same things in the same words (the helpers below); the
rich view adds color, spinners, a box around the receipt, and the halted
run's screenshot on terminals that can draw images. The plain view prints one
line per fact, for logs, pipes, CI, ``NO_COLOR`` and ``TERM=dumb``.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path
from typing import IO, Any, Iterator, Mapping, Optional

from openadapt.quickstart.engine import BreakFacts, DemoStep, RunFacts
from openadapt.quickstart.story import NextSteps, Outcome, display_path

#: Width of the step and note column, so times line up.
_LABEL_WIDTH = 40


# ---------------------------------------------------------------------------
# Words shared by both views
# ---------------------------------------------------------------------------


class Marks:
    """The two status marks, with ASCII stand-ins for terminals without them."""

    def __init__(self, encoding: Optional[str]) -> None:
        try:
            "✓■·—".encode(encoding or "utf-8")
            unicode = True
        except (LookupError, UnicodeEncodeError):
            unicode = False
        self.ok = "✓" if unicode else "OK"
        self.stop = "■" if unicode else "STOP"
        self.dot = "·" if unicode else "-"


def seconds(value: float) -> str:
    return f"{value:.1f}s"


def compiled_line(outcome: Outcome) -> str:
    return f"Compiled into a program: {outcome.program_steps} steps."


def run_status(run: RunFacts, marks: Marks) -> str:
    if run.checked:
        return f"{marks.ok} checked in the database"
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


def database_line(broken: BreakFacts) -> str:
    count = broken.new_records
    return f"{count} new record" + ("" if count == 1 else "s")


def bot_verdict(broken: BreakFacts, marks: Marks) -> Optional[str]:
    """What a tool that trusts the screen would report, when the screen said so."""

    return f"{marks.ok} Done" if broken.screen_showed_success else None


def openadapt_verdict(broken: BreakFacts, marks: Marks) -> str:
    if broken.stopped:
        return f"{marks.stop} Stopped before reporting success"
    return f"{broken.execution_outcome.title()} (expected a stop)"


def why_line(broken: BreakFacts) -> str:
    if broken.stopped and broken.new_records == 0:
        return (
            "OpenAdapt reads the record back from the database after each save. "
            "It found nothing, so it stopped."
        )
    if broken.stopped:
        return "OpenAdapt read the record back, and it didn't match, so it stopped."
    return "OpenAdapt did not stop. Treat this build as broken."


def aha_line(broken: BreakFacts) -> Optional[str]:
    """The one-line summary, only when the run showed all of it."""

    if not (
        broken.stopped and broken.screen_showed_success and broken.new_records == 0
    ):
        return None
    text = broken.screen_text
    end = text.lower().find("saved") if text else -1
    if text and end >= 0:
        screen = f'The screen said "{text[: end + len("saved")]}".'
    else:
        screen = "The screen showed its saved message."
    return (
        f"{screen} The database had nothing. A bot that trusts the screen "
        "reports Done. OpenAdapt stopped."
    )


def receipt_rows(outcome: Outcome, marks: Marks) -> list[tuple[str, str]]:
    dot = f" {marks.dot} "
    rows = [
        ("Program", f"{outcome.program_name}{dot}{outcome.program_steps} steps"),
        (
            "Runs",
            f"{outcome.checked} of {len(outcome.runs)} saved and checked in the "
            "database",
        ),
        (
            "Cost",
            f"{outcome.model_calls} AI calls{dot}${outcome.cost_usd:.2f}{dot}"
            f"{seconds(outcome.total_seconds)} in all",
        ),
    ]
    if outcome.seal is not None:
        reports = "report" if outcome.seal.sealed == 1 else "reports"
        rows.append(
            (
                "Seal",
                f"{outcome.seal.key_id}{dot}{outcome.seal.sealed} {reports} signed",
            )
        )
    return rows


def details_line(outcome: Outcome) -> str:
    """One line: where every run's full report is, and the side-by-side page."""

    root = display_path(outcome.root)
    line = f"Details: {root}, one REPORT.md per run"
    if outcome.page is not None:
        page = display_path(outcome.page)
        line += f"; side-by-side page {page}"
        if outcome.page_opened:
            line += ", opened in your browser"
    return line


def next_step_lines(steps: NextSteps) -> list[str]:
    lines = [
        f"Try it on your own app: {steps.record}",
        f"Give your agent this as a tool: {steps.agent}",
    ]
    if steps.keep:
        lines.append(f"Keep it: {steps.keep}")
    return lines


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


def make_view(stream: Optional[IO[str]] = None, *, environ=None) -> Any:
    stream = stream or sys.stdout
    if wants_plain(stream, environ):
        return PlainView(stream)
    try:
        return RichView(stream, environ=environ)
    except ImportError:  # rich is a launcher dependency; plain lines still work
        return PlainView(stream)


# ---------------------------------------------------------------------------
# Plain lines
# ---------------------------------------------------------------------------


class PlainView:
    """One line per fact, printed as it happens. No color, no cursor moves."""

    def __init__(self, stream: IO[str]) -> None:
        self.stream = stream
        self.marks = Marks(getattr(stream, "encoding", None))

    def _say(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    @contextlib.contextmanager
    def _quiet(self) -> Iterator[None]:
        yield

    def intro(self, root: Path, visible: bool) -> None:
        self._say(
            "OpenAdapt quickstart: show a task once, run it, then watch it "
            "catch a save that didn't happen."
        )
        self._say(f"Synthetic data only. Files go to {display_path(root)}")

    def starting(self):
        return self._quiet()

    def act(self, number: int, title: str, subtitle: str) -> None:
        self._say()
        self._say(f"{number}  {title} {self.marks.dot} {subtitle}")

    def demo_step(self, step: DemoStep) -> None:
        self._say(
            f"   {self.marks.ok} {step.label:<{_LABEL_WIDTH}} {seconds(step.seconds)}"
        )

    def compiling(self):
        return self._quiet()

    def compiled(self, outcome: Outcome) -> None:
        self._say(f"   {compiled_line(outcome)} {seconds(outcome.act1_seconds)}")

    def running(self, index: int, total: int, checked: int):
        return self._quiet()

    def run_row(self, run: RunFacts, total: int) -> None:
        self._say(
            f"   {run.index}/{total}  {run.note:<{_LABEL_WIDTH - 6}} "
            f"{run_status(run, self.marks)}  {seconds(run.seconds)}"
        )

    def runs_total(self, outcome: Outcome) -> None:
        self._say(f"   {runs_total_line(outcome, self.marks)}")

    def break_setup(self) -> None:
        self._say(
            "   The app will show its saved message. The database will reject the save."
        )

    def confirm(self, prompt: str) -> None:
        self.stream.write(f"   {prompt} ")
        self.stream.flush()
        try:
            input()
        except EOFError:
            self._say()

    def breaking(self):
        return self._quiet()

    def break_result(self, broken: BreakFacts) -> None:
        quote = broken.screen_text
        shown = f'"{quote}"' if quote else "a saved message"
        self._say(f"   What the screen showed:    {shown}")
        self._say(f"   What the database showed:  {database_line(broken)}")
        bot = bot_verdict(broken, self.marks)
        if bot:
            self._say(f"   A screen-trusting bot:     {bot}")
        self._say(
            f"   OpenAdapt:                 {openadapt_verdict(broken, self.marks)}"
            f"  {seconds(broken.seconds)}"
        )
        self._say(f"   Why: {why_line(broken)}")
        aha = aha_line(broken)
        if aha:
            self._say()
            self._say(aha)

    def finale(self, outcome: Outcome, steps: NextSteps) -> None:
        self._say()
        self._say("Receipt")
        for name, value in receipt_rows(outcome, self.marks):
            self._say(f"  {name:<9}  {value}")
        if steps.verify:
            self._say(f"Check it: {steps.verify}")
        self._say(details_line(outcome))
        for line in next_step_lines(steps):
            self._say(line)
        if steps.shadow:
            self._say(steps.shadow)

    def detail(self, lines: list[str]) -> None:
        for line in lines:
            self._say(f"   {line}")


# ---------------------------------------------------------------------------
# Live terminal
# ---------------------------------------------------------------------------


class RichView:
    """Color, spinners, a boxed receipt, and the screenshot where possible."""

    def __init__(
        self, stream: IO[str], *, environ=None, width: Optional[int] = None
    ) -> None:
        from rich.console import Console

        from openadapt.quickstart.images import detect_image_protocol

        self.console = Console(
            file=stream, highlight=False, soft_wrap=False, width=width
        )
        self.marks = Marks(getattr(stream, "encoding", None) or "utf-8")
        self.image_protocol = detect_image_protocol(environ, isatty=True)

    def _status(self, text: str):
        return self.console.status(f"[dim]{text}[/]", spinner="dots")

    def intro(self, root: Path, visible: bool) -> None:
        self.console.print(
            "[bold]OpenAdapt quickstart[/]: show a task once, run it, then watch "
            "it catch a save that didn't happen."
        )
        self.console.print(
            f"[dim]Synthetic data only. Files go to {display_path(root)}[/]"
        )

    def starting(self):
        return self._status("Starting the synthetic clinic app")

    def act(self, number: int, title: str, subtitle: str) -> None:
        self.console.print()
        self.console.print(
            f"[bold reverse] {number} [/] [bold]{title}[/]  [dim]{subtitle}[/]"
        )

    def demo_step(self, step: DemoStep) -> None:
        self.console.print(
            f"   [green]{self.marks.ok}[/] {_escape(step.label):<{_LABEL_WIDTH}} "
            f"[dim]{seconds(step.seconds)}[/]"
        )

    def compiling(self):
        return self._status("Compiling the demonstration into a program")

    def compiled(self, outcome: Outcome) -> None:
        self.console.print(
            f"   [bold]{compiled_line(outcome)}[/] [dim]{seconds(outcome.act1_seconds)}[/]"
        )

    def running(self, index: int, total: int, checked: int):
        return self._status(
            f"Running {index} of {total} {self.marks.dot} {checked} checked so far"
        )

    def run_row(self, run: RunFacts, total: int) -> None:
        color = "green" if run.checked else "red"
        self.console.print(
            f"   [dim]{run.index}/{total}[/]  {_escape(run.note):<{_LABEL_WIDTH - 6}} "
            f"[{color}]{run_status(run, self.marks)}[/]  [dim]{seconds(run.seconds)}[/]"
        )

    def runs_total(self, outcome: Outcome) -> None:
        self.console.print(f"   [bold]{runs_total_line(outcome, self.marks)}[/]")

    def break_setup(self) -> None:
        self.console.print(
            "   The app will show its saved message. The database will reject the save."
        )

    def confirm(self, prompt: str) -> None:
        try:
            self.console.input(f"   [bold]{prompt}[/] [dim](Enter)[/] ")
        except EOFError:
            self.console.print()

    def breaking(self):
        return self._status("Running the program once more, checking the database")

    def _right_column(self, broken: BreakFacts) -> list[str]:
        width = 52
        lines = [
            "[bold]What the database showed[/]",
            f"[bold red]{database_line(broken)}[/]",
            "",
        ]
        bot = bot_verdict(broken, self.marks)
        if bot:
            lines.append(f"A screen-trusting bot  [red]{bot}[/]")
        lines.append(
            f"OpenAdapt              [bold green]{openadapt_verdict(broken, self.marks)}[/]"
            f"  [dim]{seconds(broken.seconds)}[/]"
        )
        lines.append("")
        lines.extend(f"[dim]{part}[/]" for part in _wrap(why_line(broken), width))
        return lines

    def break_result(self, broken: BreakFacts) -> None:
        from rich.panel import Panel
        from rich.table import Table

        right = self._right_column(broken)
        drawn = False
        if self.image_protocol and broken.screenshot is not None:
            drawn = self._draw_beside_screenshot(broken, right)
        if not drawn:
            quote = broken.screen_text or "a saved message"
            box = Panel(
                f"[green]{_escape(quote)}[/]",
                title="What the screen showed",
                title_align="left",
                width=36,
                padding=(1, 2),
            )
            grid = Table.grid(padding=(0, 3))
            grid.add_column()
            grid.add_column()
            grid.add_row(box, "\n".join(right))
            self.console.print()
            self.console.print(grid, crop=False)
        aha = aha_line(broken)
        if aha:
            self.console.print()
            self.console.print(f"[bold]{_escape(aha)}[/]")

    def _draw_beside_screenshot(self, broken: BreakFacts, right: list[str]) -> bool:
        from openadapt.quickstart.engine import SCREEN_CROP
        from openadapt.quickstart.images import beside_image, crop_png, image_sequence

        try:
            png = crop_png(broken.screenshot, SCREEN_CROP)
        except Exception:  # noqa: BLE001 - fall back to the text box
            return False
        cols, rows = 40, 10
        rendered = [self._ansi(line) for line in right]
        self.console.print()
        self.console.print(
            "   [bold]What the screen showed[/] [dim](its screenshot)[/]"
        )
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

    def finale(self, outcome: Outcome, steps: NextSteps) -> None:
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        for name, value in receipt_rows(outcome, self.marks):
            table.add_row(name, _escape(value))
        self.console.print()
        self.console.print(
            Panel(table, title="Receipt", title_align="left", expand=False)
        )
        if steps.verify:
            self.console.print(f"[dim]Check it:[/] [bold]{_escape(steps.verify)}[/]")
        self.console.print(f"[dim]{_escape(details_line(outcome))}[/]")
        for line in next_step_lines(steps):
            label, _, command = line.partition(": ")
            self.console.print(f"{label}: [bold cyan]{_escape(command)}[/]")
        if steps.shadow:
            self.console.print(f"[yellow]{_escape(steps.shadow)}[/]")

    def detail(self, lines: list[str]) -> None:
        for line in lines:
            self.console.print(f"   [dim]{_escape(line)}[/]")


def _escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


__all__ = [
    "Marks",
    "PlainView",
    "RichView",
    "aha_line",
    "make_view",
    "wants_plain",
]
