"""``openadapt quickstart``: show a task once, run it, break it.

The first command anyone runs. It demonstrates one task in a synthetic clinic
app (MockMed), compiles it into a program, runs the program on a new note and
checks the save in the app's database, then makes one save fail
silently: the screen says saved, the database has nothing, and
OpenAdapt stops instead of reporting success.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO, Any, Callable, Optional

from openadapt.quickstart.scene import MOCKMED
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
    offer_publish: Optional[
        Callable[[list[Path], Callable[[str], None], Callable[[str], str]], bool]
    ] = None,
) -> Callable[[Outcome], NextSteps]:
    """The finale's commands, spelled by the launcher's ``hint`` helper.

    ``keep`` and ``shadow`` apply under ``uvx``: the install command, and a
    check (run once, at the finale) for an older ``openadapt`` on PATH.
    ``offer_publish`` is given only in a terminal outside CI, with an engine
    that can build public Seals. At the finale it asks once whether to put
    the checked and stopped runs' Seals online. The offer appears only
    when Cloud accepts hosted Seals.
    """

    def build(outcome: Outcome) -> NextSteps:
        verify = None
        if outcome.seal is not None:
            verify = hint(f"verify {quote(display_path(outcome.root))}")
        if agent_serves_tutorial:
            agent = hint("agent serve --allow-run")
        else:
            agent = hint(
                f"agent serve --bundles {quote(display_path(outcome.root / 'bundle'))}"
            )
        return NextSteps(
            verify=verify,
            verify_run=lambda run_dir: hint(
                f"verify {quote(display_path(run_dir))}"
            ),
            record=hint(
                f"flow record --backend web --url {YOUR_APP_URL} --out my-task"
            ),
            agent=agent,
            keep=keep,
            shadow=shadow() if shadow is not None else None,
            offer_publish=offer_publish,
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
    openemr: Optional[Path] = None,
) -> Outcome:
    """Run the quickstart into ``root`` and print it to ``stream``.

    ``openemr`` is a clone's ``packages/flow`` folder: the three acts then run
    on OpenEMR in Docker instead of the bundled MockMed.
    """

    from openadapt.quickstart.engine import FlowEngine, display_available, open_page
    from openadapt.quickstart.view import make_view

    stream = stream or sys.stdout
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and getattr(stream, "isatty", bool)())
    engine: Any
    if openemr is not None:
        from openadapt.quickstart.openemr import OpenEMREngine

        engine = OpenEMREngine(root, visible=visible, flow=openemr)
    else:
        # A person watching gets a short pause between demonstrated actions;
        # a hidden browser runs at full speed.
        engine = FlowEngine(root, visible=visible, pace_s=0.3 if visible else 0.0)
    view = make_view(stream, scene=getattr(engine, "scene", MOCKMED))
    notices = getattr(view, "collect_notices", None)
    with notices() if notices is not None else contextlib.nullcontext():
        try:
            return run_story(
                engine,
                view,
                next_steps=steps,
                break_it=break_it,
                interactive=interactive,
                verbose=verbose,
                # Only when this output is the terminal a person is reading:
                # a captured or redirected stream never opens a browser.
                open_page=open_page if _shown(stream, display_available) else None,
            )
        finally:
            close = getattr(view, "close", None)
            if close is not None:
                close()


def _shown(stream: IO[str], display_available: Callable[[], bool]) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty()) and display_available()


__all__ = ["next_steps", "run_quickstart"]
