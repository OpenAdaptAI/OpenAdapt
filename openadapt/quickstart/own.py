"""``openadapt quickstart --own``: your own task, from recording to a tool.

The demo quickstart shows OpenAdapt on a task it brings. This path is the
next step: you record a task once, OpenAdapt compiles it into a program,
runs it with two new inputs and checks each save, and the run ends with
what to do with the program.

1. **Record.** In the bundled sample app (synthetic data, on this computer),
   or in your own web app with ``--url``. Recording yourself opens a browser
   window, so it happens only after you say yes at a terminal with a screen.
   Without one, OpenAdapt records the sample task itself in a hidden browser
   and says so. ``--url`` always needs you, so without a screen it stops with
   one line.
2. **Compile** the recording into a program. Healthy runs make no model calls.
3. **Run** it twice with new inputs, hidden (``--watch`` shows the window).
   Each run reads the save back from the app's records and gets a receipt.
4. **Share** (sample app only). One y/N question, default No, uploads the two
   runs' signed summaries; if you're signed in, they're claimed into your
   workspace. Nothing is uploaded without that yes.
5. **Next.** Give your agent the program as an MCP tool, or publish a program
   as an API endpoint.

Every effect goes through an injected engine, ``ask`` and ``say``, so tests
drive the whole path without a browser or a network.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

#: The two new inputs for the sample app's runs (synthetic notes).
OWN_NOTES = (
    "Synthetic note: follow-up call booked for Tuesday",
    "Synthetic note: lab results reviewed, no change",
)
#: Cloud's guide for publishing a program as an endpoint; publish prints the
#: endpoint and a curl example (Cloud's dashboardNextStep.ts uses the same).
WRAP_PUBLISH_GUIDE_URL = (
    "https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/docs/"
    "WRAP_PUBLISH.md"
)
YES = ("y", "yes")


class OwnTaskError(RuntimeError):
    """A reason the own-task path stopped, in one plain line."""


@dataclass
class OwnResult:
    bundle_dir: Path
    runs: list[Any] = field(default_factory=list)
    link: Optional[str] = None
    claimed: bool = False


def _answer(ask: Callable[[str], str], prompt: str) -> bool:
    try:
        return ask(prompt).strip().lower() in YES
    except EOFError:
        return False


class SampleApp:
    """The sample app and the engine around it: the demo's own FlowEngine."""

    def __init__(self, root: Path, *, visible: bool) -> None:
        from openadapt.quickstart.engine import FlowEngine

        self.engine = FlowEngine(root, visible=visible)

    def __enter__(self) -> "SampleApp":
        self.engine.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.engine.__exit__(*exc)

    @property
    def bundle_dir(self) -> Path:
        return self.engine.bundle_dir

    @property
    def app_url(self) -> Optional[str]:
        return self.engine._base_url

    def record_yourself(self) -> None:
        from openadapt_flow.tutorial import record_tutorial_interactive

        assert self.engine._base_url is not None
        record_tutorial_interactive(self.engine._base_url, self.engine.recording_dir)

    def record_for_you(self, on_step: Callable[[Any], None]) -> None:
        self.engine.record(on_step)

    def compile(self) -> int:
        return self.engine.compile()

    def run(self, index: int, note: str) -> Any:
        return self.engine.run(index, note, folder=f"own-run-{index}")

    def seal(self, runs: list[Any]) -> Any:
        return self.engine.seal(runs)


def run_own(
    root: Path,
    *,
    url: Optional[str] = None,
    visible: bool = False,
    can_record_here: bool,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    hint: Callable[[str], str],
    quote: Callable[[Path], str],
    sample_factory: Callable[..., Any] = SampleApp,
    own_app: Optional[Callable[..., OwnResult]] = None,
    share: Optional[Callable[[list[Path]], tuple[Optional[str], bool]]] = None,
) -> OwnResult:
    """Run the own-task path; returns what it made. Raises OwnTaskError."""
    if url is not None:
        if not can_record_here:
            raise OwnTaskError(
                "Recording your own app opens a browser window, and this "
                "terminal has no screen. Run it on a computer with one."
            )
        if own_app is None:
            own_app = record_own_app
        result = own_app(root, url=url, visible=visible, ask=ask, say=say)
        _ending(result, say=say, hint=hint, quote=quote, app_url=url)
        return result

    say("Your own task, in the sample app (synthetic data, on this computer).")
    with sample_factory(root, visible=visible) as app:
        say("")
        say("1. Record the task once")
        if can_record_here and _answer(
            ask, "   Do it yourself in a browser window? [y/N] "
        ):
            app.record_yourself()
        else:
            say("   OpenAdapt does it once in a hidden browser:")
            app.record_for_you(lambda step: say(f"     {step.label}"))
        steps = app.compile()
        say("")
        say(f"2. Compiled into a {steps}-step program. Runs make no AI model calls.")
        say("")
        say("3. Run it with two new inputs")
        runs = []
        for index, note in enumerate(OWN_NOTES, start=1):
            run = app.run(index, note)
            runs.append(run)
            say(f'   "{note}"')
            say(
                f"     {run.plain_label}: {run.effects_confirmed} of "
                f"{run.effects_required} record checks, {run.model_calls} model calls"
            )
        app.seal(runs)
        result = OwnResult(bundle_dir=app.bundle_dir, runs=runs)
        app_url = app.app_url
    if share is not None and all(getattr(r, "checked", False) for r in runs):
        say("")
        say("4. Share the runs")
        result.link, result.claimed = share([Path(r.run_dir) for r in runs])
    _ending(result, say=say, hint=hint, quote=quote, app_url=app_url, sample=True)
    return result


def record_own_app(
    root: Path,
    *,
    url: str,
    visible: bool,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    params: tuple[str, ...] = (),
    script: Optional[Callable[[Any, Callable[[], None]], None]] = None,
) -> OwnResult:
    """Record a task in your own web app, compile it and run it once.

    ``script`` drives the recording headless (tests); otherwise a browser
    window opens on ``url`` after a yes, and the recording ends when you
    close it.
    """
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.interactive_recorder import record_interactive

    recording_dir = root / "recording"
    bundle_dir = root / "bundle"
    say(f"Your own task, in {url}.")
    say("")
    say("1. Record the task once")
    if script is None:
        if not _answer(
            ask,
            "   A browser window opens on your app. Do the task once, then "
            "close the window. Open it? [y/N] ",
        ):
            raise OwnTaskError("Not recorded: no window was opened.")
    root.mkdir(parents=True, exist_ok=True)
    record_interactive(
        url,
        recording_dir,
        param_fields=params,
        headless=script is not None,
        script=script,
    )
    workflow = compile_recording(recording_dir, bundle_dir, name="own-task")
    say("")
    say(
        f"2. Compiled into a {len(workflow.steps)}-step program. "
        "Runs make no AI model calls."
    )
    return OwnResult(bundle_dir=bundle_dir)


def _ending(
    result: OwnResult,
    *,
    say: Callable[[str], None],
    hint: Callable[[str], str],
    quote: Callable[[Path], str],
    app_url: Optional[str],
    sample: bool = False,
) -> None:
    serve = (
        f"agent serve --bundles {quote(result.bundle_dir)} --mode production "
        "--allow-run"
    )
    if not sample and app_url:
        serve += f" --url {app_url}"
    say("")
    say("Next: put your program to work")
    say("  Give your agent this program as a tool:")
    say(f"    claude mcp add openadapt -- {hint(serve)}")
    if sample:
        say(
            "    The sample app runs only during the quickstart. With your own "
            "app the tool works any time:"
        )
        say(f"    {hint('quickstart --own --url https://your-app.example')}")
    say("  Or publish a program as an API endpoint. Publishing prints the")
    say("  endpoint and a curl example, and each call returns a receipt:")
    say(f"    {WRAP_PUBLISH_GUIDE_URL}")
    if result.link:
        say(f"  Your runs: {result.link}")
    elif sample:
        say(
            f"  Keep the runs in your workspace: {hint('login')}, then "
            f"{hint('seal publish')} for each run folder."
        )


def share_runs(
    run_dirs: list[Path],
    *,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    command: Callable[[str], str],
) -> tuple[Optional[str], bool]:
    """Offer the runs' Seals (one y/N, default No), then claim them if signed in.

    Returns the link (or None when nothing was sent) and whether the runs are
    now in a workspace. Never raises: the runs already worked."""
    from openadapt import seal_claim, seal_publish

    try:
        origin = seal_publish.endpoint()
        if not seal_publish.hosted_seals_live(origin):
            say(
                "   Sharing is not available right now; the runs stay on this computer."
            )
            return None, False
        link = seal_publish.publish_quickstart(
            run_dirs, ask=ask, say=say, command=command
        )
    except seal_publish.PublishError as exc:
        say(f"   {exc}")
        return None, False
    if link is None:
        return None, False
    with contextlib.suppress(Exception):
        from openadapt_flow.hosted import resolve_host, resolve_token

        if resolve_host(None) == origin:
            token = resolve_token(None, host=origin)
            seal_claim.check_destination(origin)
            if seal_claim.claims_live(origin, transport=seal_publish.urllib_transport):
                code = seal_claim.claim_published(
                    origin,
                    token,
                    transport=seal_publish.urllib_transport,
                    say=lambda line: say(f"   {line}"),
                    automatic=True,
                    quiet_when_none=True,
                )
                return link, code == 0
    say(f"   Sign in to keep them in your workspace: {command('login')}")
    return link, False


__all__ = [
    "OWN_NOTES",
    "OwnResult",
    "OwnTaskError",
    "record_own_app",
    "run_own",
    "share_runs",
]
