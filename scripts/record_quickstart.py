"""Record one real quickstart run as SVG screenshots and plain text.

Runs the bundled MockMed quickstart once in a hidden browser and shows it
through several views at once: the live view at each requested width
(recorded by rich, saved as SVG) and the plain view (saved as text). The
recordings are the terminal's final state; the live view's spinners and
ticks are left out of a still image.

    python scripts/record_quickstart.py --out docs/quickstart/story
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from typing import Any


class TeeView:
    """Shows one story through several views."""

    def __init__(self, views: list[Any]) -> None:
        self.views = views

    def __getattr__(self, name: str) -> Any:
        methods = [getattr(view, name) for view in self.views if hasattr(view, name)]

        def call(*args: Any, **kwargs: Any) -> Any:
            results = [method(*args, **kwargs) for method in methods]
            managers = [r for r in results if hasattr(r, "__enter__")]
            if not managers:
                return None
            stack = contextlib.ExitStack()

            @contextlib.contextmanager
            def all_of() -> Any:
                with stack:
                    for manager in managers:
                        stack.enter_context(manager)
                    yield

            return all_of()

        return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--widths", default="100,60")
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    from rich.console import Console
    from rich.terminal_theme import MONOKAI

    from openadapt import cli
    from openadapt.quickstart import next_steps
    from openadapt.quickstart.engine import FlowEngine
    from openadapt.quickstart.story import run_story
    from openadapt.quickstart.view import PlainView, RichView

    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.run_dir or Path(tempfile.mkdtemp(prefix="quickstart-record-"))
    root = base / "openadapt-quickstart"

    consoles = {}
    views: list[Any] = []
    for width in (int(w) for w in args.widths.split(",")):
        console = Console(
            file=io.StringIO(),
            record=True,
            force_terminal=True,
            color_system="truecolor",
            width=width,
            highlight=False,
            soft_wrap=False,
        )
        consoles[width] = console
        views.append(RichView(console.file, console=console, animate=False))
    plain = io.StringIO()
    views.append(PlainView(plain))
    tee = TeeView(views)

    steps = next_steps(cli._hint, cli._shell_quote, cli._agent_serves_tutorial())
    import os

    os.chdir(base)
    with cli._synthetic_scrub_off(), views[0].collect_notices():
        # The first view collects the engine's notices; the others read them.
        for view in views[1:]:
            view._notices = views[0]._notices
        engine = FlowEngine(root, visible=False)
        outcome = run_story(engine, tee, next_steps=steps, break_it=True)

    for width, console in consoles.items():
        title = f"openadapt quickstart ({width} columns)"
        svg = args.out / f"quickstart-{width}col.svg"
        console.save_svg(str(svg), title=title, theme=MONOKAI, clear=False)
    (args.out / "quickstart-plain.txt").write_text(plain.getvalue(), encoding="utf-8")
    from rich.text import Text

    flat = Console(file=io.StringIO(), record=True, width=120, color_system=None)
    flat.print(Text(plain.getvalue().rstrip("\n")), soft_wrap=True)
    flat.save_svg(
        str(args.out / "quickstart-plain.svg"),
        title="openadapt quickstart | cat (plain output)",
        theme=MONOKAI,
    )
    print(f"recorded into {args.out} from {root}", file=sys.stderr)
    return 0 if outcome.checked == len(outcome.runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
