"""``openadapt quickstart``: show a task once, run it, break it.

The first command anyone runs. It demonstrates one task in a synthetic clinic
app (MockMed), compiles it into a program, runs the program five times with a
new note each time and checks every save in the app's database, then makes
one save fail silently: the screen says saved, the database has nothing, and
OpenAdapt stops instead of reporting success.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Callable, Optional

from openadapt.quickstart.story import (
    YOUR_APP_URL,
    NextSteps,
    Outcome,
    display_path,
    run_story,
)


def next_steps(
    hint: Callable[[str], str],
    quote: Callable[[object], str],
    agent_serves_tutorial: bool,
    *,
    keep: Optional[str] = None,
    shadow: Optional[Callable[[], Optional[str]]] = None,
) -> Callable[[Outcome], NextSteps]:
    """The finale's commands, spelled by the launcher's ``hint`` helper.

    ``keep`` and ``shadow`` apply under ``uvx``: the install command, and a
    check (run once, at the finale) for an older ``openadapt`` on PATH.
    """

    def build(outcome: Outcome) -> NextSteps:
        verify = None
        if outcome.seal is not None:
            verify = hint(f"seal verify {quote(display_path(outcome.root))}")
        if agent_serves_tutorial:
            agent = hint("agent serve --allow-run")
        else:
            agent = hint(
                f"agent serve --bundles {quote(display_path(outcome.root / 'bundle'))}"
            )
        return NextSteps(
            verify=verify,
            record=hint(
                f"flow record --backend web --url {YOUR_APP_URL} --out my-task"
            ),
            agent=agent,
            keep=keep,
            shadow=shadow() if shadow is not None else None,
        )

    return build


def run_quickstart(
    root: Path,
    *,
    visible: bool,
    break_it: bool,
    verbose: bool,
    steps: Callable[[Outcome], NextSteps],
    stream: Optional[IO[str]] = None,
    interactive: Optional[bool] = None,
) -> Outcome:
    """Run the quickstart into ``root`` and print it to ``stream``."""

    from openadapt.quickstart.engine import FlowEngine, display_available, open_page
    from openadapt.quickstart.view import make_view

    stream = stream or sys.stdout
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and getattr(stream, "isatty", bool)())
    # A person watching gets a short pause between demonstrated actions; a
    # hidden browser runs at full speed.
    engine = FlowEngine(root, visible=visible, pace_s=0.3 if visible else 0.0)
    return run_story(
        engine,
        make_view(stream),
        next_steps=steps,
        break_it=break_it,
        interactive=interactive,
        verbose=verbose,
        open_page=open_page if display_available() else None,
    )


__all__ = ["next_steps", "run_quickstart"]
