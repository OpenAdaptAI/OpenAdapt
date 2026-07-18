"""Unified CLI for OpenAdapt ecosystem.

Usage:
    openadapt flow record --url <app> --out rec   # record a workflow once
    openadapt flow compile rec --out bundle        # compile it
    openadapt flow replay bundle                    # run it, local, $0
    openadapt flow lint bundle
    openadapt flow certify bundle --policy clinical-write

    openadapt capture start --name my-task
    openadapt capture stop
    openadapt capture list
    openadapt capture view <name>

    openadapt train --capture my-task --model qwen3vl-2b
    openadapt train status
    openadapt train stop

    openadapt eval --checkpoint model.pt --benchmark waa
    openadapt eval --agent api-claude --benchmark waa

    openadapt serve --port 8080

    openadapt version
    openadapt doctor
"""

import sys
from typing import Optional

import click

from openadapt.version import __version__


class _FlowFirstGroup(click.Group):
    """Command group that lists ``flow`` first in ``--help``.

    The demonstration compiler is the flagship, so it should lead the
    command listing instead of sorting alphabetically behind ``capture``.
    Everything else keeps its normal alphabetical order.
    """

    def list_commands(self, ctx):
        commands = super().list_commands(ctx)
        return sorted(commands, key=lambda name: (name != "flow", name))


_FLOW_PASSTHROUGH_COMMANDS = {
    "induce": "Induce a parameterized program from multiple recordings.",
    "run": "Run a bundle under a fail-closed deployment configuration.",
    "resume": "Resume a durably paused run from its verified checkpoint.",
    "approve": "Authorize a durably paused run to resume.",
    "bench": "Benchmark deterministic replay against the bundled fixture.",
    "benchmark": "Compare replay with an optional computer-use agent arm.",
    "disambiguate": "Resolve compile-time ambiguity without guessing.",
    "emit-skill": "Emit an Agent Skills folder for a bundle.",
    "emit-mcp": "Emit a standalone MCP server for a bundle.",
    "teach": "Teach a governed correction after a halt.",
    "connect": "Connect this computer to an authenticated Cloud workspace.",
    "login": "Validate and store a hosted ingest token.",
    "sanitize": "Create a verified sanitized derivative locally.",
    "review-sanitized": "Review original and sanitized content locally.",
    "approve-sanitized": "Approve and freeze exact sanitized bytes.",
    "validate-hosted": "Bind local evidence to an expiring hosted challenge.",
    "push": "Upload an approved sanitized derivative.",
    "report-break": "Upload a schema-minimized halt diagnostic.",
}


class _FlowPassthroughGroup(click.Group):
    """Delegate engine commands not wrapped by this compatibility launcher."""

    def list_commands(self, ctx):
        commands = set(super().list_commands(ctx))
        commands.update(_FLOW_PASSTHROUGH_COMMANDS)
        return sorted(commands)

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        @click.command(
            name=cmd_name,
            help=_FLOW_PASSTHROUGH_COMMANDS.get(
                cmd_name, "Delegate this command to openadapt-flow."
            ),
            context_settings={
                "ignore_unknown_options": True,
                "allow_extra_args": True,
                "help_option_names": [],
            },
        )
        @click.pass_context
        def passthrough(command_ctx):
            _run_flow([cmd_name, *command_ctx.args])

        return passthrough


@click.group(cls=_FlowFirstGroup)
@click.version_option(version=__version__, prog_name="openadapt")
def main():
    """OpenAdapt - Beta launcher for the openadapt-flow compiler.

    Compile a demonstrated workflow into deterministic local replay. Healthy
    runs make no model calls; configured checks can halt on ambiguity. Native
    capture is experimental, and training/evaluation commands are research.

    \b
    Quick Start:
        openadapt flow demo-record --out rec
        openadapt flow compile rec --out bundle --name demo
        openadapt flow lint bundle
        openadapt flow replay bundle
    """
    pass


# =============================================================================
# Flow Commands (the demonstration compiler — flagship path)
# =============================================================================


def _run_flow(argv: list[str]) -> None:
    """Delegate to the openadapt-flow CLI, preserving its exit code.

    openadapt-flow exposes an argparse ``main(argv)`` entry point; each
    ``openadapt flow <verb>`` click command reconstructs that verb's argv
    and hands off here so behavior is identical to ``openadapt-flow <verb>``.
    Imported lazily so ``openadapt`` (and ``openadapt flow --help``) work
    even when openadapt-flow isn't installed.
    """
    try:
        from openadapt_flow.__main__ import main as flow_main
    except ImportError:
        click.echo("Error: openadapt-flow not installed.", err=True)
        click.echo("Reinstall with: pip install --upgrade openadapt", err=True)
        click.echo("Engine only: pip install openadapt-flow", err=True)
        sys.exit(1)

    sys.exit(flow_main(argv))


@main.group(cls=_FlowPassthroughGroup)
def flow():
    """Record, compile, and replay workflows (the demonstration compiler).

    Compile a recording into deterministic local replay. Supported drift can
    be re-resolved; configured identity and verification gates halt on failure.

    \b
    Examples:
        openadapt flow demo-record --out rec
        openadapt flow compile rec --out bundle --name demo
        openadapt flow replay bundle
        openadapt flow lint bundle
        openadapt flow certify bundle --policy clinical-write
        openadapt flow sanitize rec --kind recording --out rec-sanitized
        openadapt flow review-sanitized rec-sanitized --original rec
        openadapt flow approve-sanitized rec-sanitized --original rec --reviewer USER
        openadapt flow login --token oai_ingest_...
        openadapt flow push rec-sanitized --kind recording

    \b
    The standalone `openadapt-flow <verb>` command keeps working and behaves
    identically; `openadapt flow <verb>` is the recommended path.
    """
    pass


@main.command("connect")
@click.option(
    "--pairing", default=None, help="Five-minute pairing code from Cloud settings"
)
@click.option("--uri", default=None, help="Exact openadapt://connect desktop deep link")
@click.option(
    "--host", default=None, help="Cloud origin (default: https://app.openadapt.ai)"
)
@click.option("--device-name", default=None, help="Name shown for this computer")
@click.option(
    "--destination-kind",
    type=click.Choice(["openadapt-managed", "customer-managed", "local"]),
    default=None,
    help="Trust class for the pairing destination",
)
@click.option(
    "--trusted-host",
    multiple=True,
    help="Exact allowed customer-managed origin (repeatable)",
)
def connect(pairing, uri, host, device_name, destination_kind, trusted_host):
    """Connect this computer to the signed-in OpenAdapt Cloud workspace.

    The browser creates a five-minute, one-use pairing. This command claims it
    and saves the resulting workspace credential in the OS keychain. It cannot
    execute arbitrary terminal commands or grant browser access to the shell.
    """
    if bool(pairing) == bool(uri):
        raise click.UsageError("Pass exactly one of --pairing or --uri.")
    try:
        from openadapt_flow import hosted

        if not hasattr(hosted, "connect"):
            raise ImportError
    except ImportError:
        click.echo(
            "Error: this connection flow needs a newer openadapt-flow. "
            "Run: pip install --upgrade openadapt",
            err=True,
        )
        raise click.exceptions.Exit(1)

    argv = ["connect", "--pairing", pairing] if pairing else ["connect", "--uri", uri]
    if host:
        argv += ["--host", host]
    if device_name:
        argv += ["--device-name", device_name]
    if destination_kind:
        argv += ["--destination-kind", destination_kind]
    for value in trusted_host:
        argv += ["--trusted-host", value]
    _run_flow(argv)


@flow.command("record")
@click.option("--url", required=True, help="URL of the app to record against")
@click.option("--out", required=True, help="Recording output directory")
@click.option(
    "--secret",
    multiple=True,
    metavar="FIELD",
    help="Mark a typed field as SECRET (value never persisted). Repeatable.",
)
@click.option(
    "--param",
    multiple=True,
    metavar="FIELD",
    help="Record a typed field as a PARAMETER (overridable at replay). Repeatable.",
)
@click.option(
    "--headless", is_flag=True, help="Run the browser headless (scripted/CI recording)"
)
def flow_record(url, out, secret, param, headless):
    """Record YOUR app interactively in a headed browser."""
    argv = ["record", "--url", url, "--out", out]
    for value in secret:
        argv += ["--secret", value]
    for value in param:
        argv += ["--param", value]
    if headless:
        argv.append("--headless")
    _run_flow(argv)


@flow.command("demo-record")
@click.option("--out", required=True, help="Recording output directory")
@click.option(
    "--note-text",
    default="Follow-up in 2 weeks; BP recheck.",
    help="Note text typed during the demo (recorded as a parameter)",
)
@click.option("--param-name", default="note", help="Parameter name for the note")
@click.option("--drift", default=None, help="Comma-separated MockMed drift modes")
@click.option("--headed", is_flag=True, help="Run the browser headed")
def flow_demo_record(out, note_text, param_name, drift, headed):
    """Serve the bundled MockMed app and record the canonical triage demo."""
    argv = [
        "demo-record",
        "--out",
        out,
        "--note-text",
        note_text,
        "--param-name",
        param_name,
    ]
    if drift:
        argv += ["--drift", drift]
    if headed:
        argv.append("--headed")
    _run_flow(argv)


@flow.command("compile")
@click.argument("recording")
@click.option("--out", required=True, help="Output bundle directory")
@click.option("--name", required=True, help="Workflow name")
def flow_compile(recording, out, name):
    """Compile a recording directory into a workflow bundle."""
    _run_flow(["compile", recording, "--out", out, "--name", name])


@flow.command("replay")
@click.argument("bundle")
@click.option(
    "--url",
    default=None,
    help="URL of the target app (default: serve the bundled MockMed demo app)",
)
@click.option(
    "--drift",
    default=None,
    help="MockMed drift modes to demo bounded re-resolution (no --url)",
)
@click.option("--run-dir", default=None, help="Run output directory")
@click.option(
    "--param",
    multiple=True,
    metavar="K=V",
    help="Parameter substitution (repeatable)",
)
@click.option(
    "--save-healed-to", default=None, help="Write the healed bundle to this directory"
)
@click.option("--headed", is_flag=True, help="Run the browser headed")
@click.option(
    "--record-video",
    default=None,
    metavar="DIR",
    help="OPT-IN: capture a WebM video of the replay session into DIR",
)
def flow_replay(
    bundle, url, drift, run_dir, param, save_healed_to, headed, record_video
):
    """Replay a bundle (serves the bundled MockMed demo app when no --url)."""
    argv = ["replay", bundle]
    if url:
        argv += ["--url", url]
    if drift:
        argv += ["--drift", drift]
    if run_dir:
        argv += ["--run-dir", run_dir]
    for value in param:
        argv += ["--param", value]
    if save_healed_to:
        argv += ["--save-healed-to", save_healed_to]
    if headed:
        argv.append("--headed")
    if record_video:
        argv += ["--record-video", record_video]
    _run_flow(argv)


@flow.command("lint")
@click.argument("bundle")
@click.option(
    "--strict",
    is_flag=True,
    help="Exit nonzero on warnings too (default: only on errors)",
)
def flow_lint(bundle, strict):
    """Report a bundle's coverage gaps; exits nonzero by severity."""
    argv = ["lint", bundle]
    if strict:
        argv.append("--strict")
    _run_flow(argv)


@flow.command("certify")
@click.argument("bundle")
@click.option(
    "--policy",
    required=True,
    help="Policy YAML path, or a built-in name (permissive, clinical-write)",
)
def flow_certify(bundle, policy):
    """Enforce a safety policy on a bundle (refuse it if it fails)."""
    _run_flow(["certify", bundle, "--policy", policy])


# =============================================================================
# Capture Commands
# =============================================================================


@main.group()
def capture():
    """Experimental native GUI capture (optional extra).

    \b
    Examples:
        openadapt capture start --name login-flow
        openadapt capture stop
        openadapt capture list
        openadapt capture view login-flow
    """
    pass


@capture.command("start")
@click.option("--name", "-n", required=True, help="Name for the capture session")
@click.option("--video/--no-video", default=True, help="Record video")
@click.option("--audio/--no-audio", default=False, help="Record audio")
def capture_start(name: str, video: bool, audio: bool):
    """Start a new capture session."""
    try:
        from openadapt_capture import Recorder

        click.echo(f"Starting capture session: {name}")
        click.echo("Press Ctrl+C (or Ctrl x3) to stop recording...")

        with Recorder(
            f"./{name}",
            task_description=name,
            capture_video=video,
            capture_audio=audio,
        ) as recorder:
            recorder.wait_for_ready()
            click.echo("Recording...")
            try:
                while recorder.is_recording:
                    import time

                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        click.echo(f"\nCapture saved: ./{name}/ ({recorder.event_count} events)")

    except ImportError:
        click.echo("Error: openadapt-capture not installed.", err=True)
        click.echo("Install with: pip install openadapt-capture", err=True)
        sys.exit(1)


@capture.command("stop")
def capture_stop():
    """Stop the current capture session."""
    click.echo("Stopping active capture session...")
    # TODO: Implement stop via signal/file
    click.echo("Note: Use Ctrl+C in the capture terminal to stop")


@capture.command("list")
@click.option("--path", "-p", default=".", help="Path to search for captures")
def capture_list(path: str):
    """List available captures."""
    try:
        from pathlib import Path

        from openadapt_capture import Capture

        captures_found = 0
        for capture_dir in sorted(Path(path).iterdir()):
            if capture_dir.is_dir() and (capture_dir / "recording.db").exists():
                try:
                    cap = Capture.load(str(capture_dir))
                    desc = cap.task_description or ""
                    n_actions = sum(1 for _ in cap.actions())
                    cap.close()
                    click.echo(f"  {capture_dir.name}  ({n_actions} actions)  {desc}")
                    captures_found += 1
                except Exception:
                    continue

        if captures_found == 0:
            click.echo("No captures found.")
        else:
            click.echo(f"\nTotal: {captures_found} capture(s)")

    except ImportError:
        click.echo("Error: openadapt-capture not installed.", err=True)
        sys.exit(1)


@capture.command("view")
@click.argument("name")
@click.option("--open/--no-open", default=True, help="Open in browser")
def capture_view(name: str, open: bool):
    """View a capture recording."""
    try:
        from pathlib import Path

        from openadapt_capture import create_html

        capture_path = Path(name)
        if not capture_path.exists():
            click.echo(f"Error: Capture not found: {name}", err=True)
            sys.exit(1)

        output_path = capture_path / "viewer.html"
        create_html(str(capture_path), str(output_path))
        click.echo(f"Viewer generated: {output_path}")

        if open:
            import webbrowser

            webbrowser.open(f"file://{output_path.absolute()}")

    except ImportError:
        click.echo("Error: openadapt-capture not installed.", err=True)
        sys.exit(1)


# =============================================================================
# Train Commands
# =============================================================================


@main.group()
def train():
    """Research: train ML models on captured demonstrations.

    \b
    Examples:
        openadapt train start --capture login-flow --model qwen3vl-2b
        openadapt train status
        openadapt train stop
    """
    pass


@train.command("start")
@click.option("--capture", "-c", required=True, help="Path to capture directory")
@click.option("--model", "-m", default="qwen3vl-2b", help="Model to train")
@click.option("--config", help="Path to training config YAML")
@click.option("--output", "-o", default="training_output", help="Output directory")
@click.option("--open/--no-open", default=True, help="Open dashboard in browser")
def train_start(
    capture: str, model: str, config: Optional[str], output: str, open: bool
):
    """Start model training."""
    try:
        click.echo("Starting training...")
        click.echo(f"  Capture: {capture}")
        click.echo(f"  Output: {output}")

        # Import and run training. The model is determined by the config
        # file; --model is kept for backward compatibility only.
        from openadapt_ml.scripts.train import main as train_main

        if not config:
            from openadapt_ml.cloud.local import detect_device

            if "cuda" in detect_device():
                config = "configs/qwen3vl_capture.yaml"
            else:
                config = "configs/qwen3vl_capture_4bit.yaml"
        try:
            from openadapt_ml.cloud.local import resolve_config_path

            config = str(resolve_config_path(config))
        except ImportError:
            # Older openadapt-ml without resolve_config_path; use as-is.
            pass

        from pathlib import Path

        if not Path(config).exists():
            click.echo(f"Error: config not found: {config}", err=True)
            click.echo(
                "Upgrade openadapt-ml so bundled configs resolve, or pass "
                "--config with a path to a training config YAML.",
                err=True,
            )
            sys.exit(1)
        click.echo(f"  Config: {config}")

        train_main(
            config_path=config,
            capture_path=capture,
            output_dir=output,
            open_dashboard=open,
        )

    except ImportError as e:
        click.echo(f"Error: failed to import openadapt-ml ({e}).", err=True)
        click.echo('Install with: pip install "openadapt-ml[training]"', err=True)
        sys.exit(1)


@train.command("status")
@click.option("--output", "-o", default="training_output", help="Output directory")
def train_status(output: str):
    """Check training status."""
    try:
        import json
        from pathlib import Path

        log_path = Path(output) / "training_log.json"
        if not log_path.exists():
            click.echo("No active training found.")
            return

        with open(log_path) as f:
            log = json.load(f)

        status = log.get("status", "unknown")
        epoch = log.get("epoch", 0)
        loss = log.get("loss", 0)
        elapsed = log.get("elapsed_time", 0)

        click.echo(f"Status: {status}")
        click.echo(f"Epoch: {epoch}")
        click.echo(f"Loss: {loss:.4f}")
        click.echo(f"Elapsed: {elapsed / 60:.1f} minutes")

    except Exception as e:
        click.echo(f"Error reading training status: {e}", err=True)
        sys.exit(1)


@train.command("stop")
@click.option("--output", "-o", default="training_output", help="Output directory")
def train_stop(output: str):
    """Stop active training."""
    try:
        from pathlib import Path

        stop_file = Path(output) / "STOP_TRAINING"
        stop_file.touch()
        click.echo("Stop signal sent. Training will stop after current step.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Eval Commands
# =============================================================================


@main.group()
def eval():
    """Research: evaluate computer-use models on benchmarks.

    \b
    Examples:
        openadapt eval run --checkpoint model.pt --benchmark waa
        openadapt eval run --agent api-claude --benchmark waa
        openadapt eval mock --tasks 10
    """
    pass


@eval.command("run")
@click.option("--checkpoint", "-c", help="Path to model checkpoint")
@click.option(
    "--agent",
    "-a",
    type=click.Choice(["api-claude", "api-openai"]),
    help="API agent to use",
)
@click.option("--benchmark", "-b", default="waa", help="Benchmark name")
@click.option("--tasks", "-t", default=10, help="Number of tasks")
@click.option("--server", "-s", help="WAA server URL for live eval")
@click.option("--demo", help="Demo file for agent")
def eval_run(
    checkpoint: Optional[str],
    agent: Optional[str],
    benchmark: str,
    tasks: int,
    server: Optional[str],
    demo: Optional[str],
):
    """Run benchmark evaluation."""
    try:
        from openadapt_evals import (
            ApiAgent,
            WAALiveAdapter,
            WAAMockAdapter,
            compute_metrics,
            evaluate_agent_on_benchmark,
        )

        # Create agent
        if checkpoint:
            click.echo(f"Loading model from: {checkpoint}")
            from openadapt_evals import PolicyAgent

            eval_agent = PolicyAgent(checkpoint_path=checkpoint)
        elif agent:
            provider = "anthropic" if "claude" in agent else "openai"
            click.echo(f"Using API agent: {provider}")

            demo_text = None
            if demo:
                with open(demo) as f:
                    demo_text = f.read()

            eval_agent = ApiAgent(provider=provider, demo=demo_text)
        else:
            click.echo("Error: Specify --checkpoint or --agent", err=True)
            sys.exit(1)

        # Create adapter
        if server:
            click.echo(f"Connecting to: {server}")
            adapter = WAALiveAdapter(server_url=server)
        else:
            click.echo(f"Using mock adapter with {tasks} tasks")
            adapter = WAAMockAdapter(num_tasks=tasks)

        # Run evaluation
        click.echo("Running evaluation...")
        results = evaluate_agent_on_benchmark(eval_agent, adapter, max_steps=15)

        # Compute and display metrics
        metrics = compute_metrics(results)
        click.echo("\nResults:")
        click.echo(f"  Success rate: {metrics['success_rate']:.1%}")
        click.echo(f"  Avg steps: {metrics['avg_steps']:.1f}")
        click.echo(f"  Total tasks: {metrics['total_tasks']}")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)


@eval.command("mock")
@click.option("--tasks", "-t", default=10, help="Number of mock tasks")
@click.option("--output", "-o", default="benchmark_results", help="Output directory")
def eval_mock(tasks: int, output: str):
    """Run mock evaluation for testing."""
    try:
        from openadapt_evals import (
            SmartMockAgent,
            WAAMockAdapter,
            compute_metrics,
            evaluate_agent_on_benchmark,
        )

        click.echo(f"Running mock evaluation with {tasks} tasks...")

        agent = SmartMockAgent()
        adapter = WAAMockAdapter(num_tasks=tasks)
        results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)

        metrics = compute_metrics(results)
        click.echo("\nResults:")
        click.echo(f"  Success rate: {metrics['success_rate']:.1%}")
        click.echo(f"  Avg steps: {metrics['avg_steps']:.1f}")

    except ImportError:
        click.echo("Error: openadapt-evals not installed.", err=True)
        sys.exit(1)


# =============================================================================
# Serve Command
# =============================================================================


@main.command()
@click.option("--port", "-p", default=8080, help="Port to serve on")
@click.option("--output", "-o", default="training_output", help="Output directory")
@click.option("--open/--no-open", default=True, help="Open in browser")
def serve(port: int, output: str, open: bool):
    """Research: serve the training dashboard and viewer.

    \b
    Examples:
        openadapt serve --port 8080
        openadapt serve --output training_output --open
    """
    try:
        click.echo(f"Starting server on port {port}...")
        click.echo(f"Serving from: {output}")

        import argparse
        from pathlib import Path

        from openadapt_ml.cloud import local as oa_local

        # cmd_serve resolves the 'current' run against the module-level
        # TRAINING_OUTPUT constant; point it at the requested directory
        # so --output is honored.
        oa_local.TRAINING_OUTPUT = Path(output)

        sys.exit(
            oa_local.cmd_serve(
                argparse.Namespace(
                    port=port,
                    benchmark=None,
                    no_regenerate=False,
                    start_page=None,
                    quiet=False,
                    open=open,
                )
            )
        )

    except ImportError as e:
        click.echo(f"Error: failed to import openadapt-ml ({e}).", err=True)
        click.echo('Install with: pip install "openadapt-ml[training]"', err=True)
        sys.exit(1)


# =============================================================================
# Utility Commands
# =============================================================================


@main.command()
def version():
    """Show version information for all packages."""
    # Read distribution metadata instead of importing the packages:
    # importing executes package code (openadapt-capture takes a
    # screenshot at import time, which crashes in headless environments
    # like CI), and metadata is what we actually want here.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    click.echo("OpenAdapt Ecosystem Versions:")
    click.echo("=" * 40)

    packages = [
        "openadapt",
        "openadapt-capture",
        "openadapt-ml",
        "openadapt-evals",
        "openadapt-flow",
        "openadapt-viewer",
        "openadapt-grounding",
        "openadapt-retrieval",
    ]

    for name in packages:
        try:
            click.echo(f"  {name}: {dist_version(name)}")
        except PackageNotFoundError:
            click.echo(f"  {name}: not installed")


@main.command()
def doctor():
    """Check system requirements and dependencies."""
    click.echo("OpenAdapt System Check")
    click.echo("=" * 40)

    # Check Python version
    import platform

    click.echo(f"\nPython: {platform.python_version()}")
    click.echo(f"Platform: {platform.system()} {platform.release()}")

    from importlib.util import find_spec

    # Core packages: installed by the base `pip install openadapt`. Only
    # these are treated as required; a missing one is a real problem.
    click.echo("\nCore packages (installed with `pip install openadapt`):")
    core = [
        "openadapt_flow",
    ]
    for pkg in core:
        # find_spec checks installability without executing package code
        # (importing openadapt-capture screenshots at import time, which
        # crashes headless environments)
        if find_spec(pkg) is not None:
            click.echo(f"  [OK] {pkg}")
        else:
            click.echo(
                f"  [MISSING] {pkg} (core dependency — reinstall with "
                f"`pip install openadapt`)"
            )

    # Optional packages: opt-in extras the base install intentionally
    # excludes. A missing extra is expected, not a failure — report how to
    # install it rather than flagging it. Maps import name -> extra name.
    click.echo("\nOptional packages (install with `pip install openadapt[...]`):")
    optional = [
        ("openadapt_capture", "capture"),
        ("openadapt_ml", "ml"),
        ("openadapt_evals", "evals"),
        ("openadapt_viewer", "viewer"),
        ("openadapt_grounding", "grounding"),
        ("openadapt_retrieval", "retrieval"),
        ("openadapt_privacy", "privacy"),
    ]
    for pkg, extra in optional:
        if find_spec(pkg) is not None:
            click.echo(f"  [OK] {pkg}")
        else:
            click.echo(
                f"  [--] {pkg} (optional — install with "
                f"`pip install openadapt[{extra}]`)"
            )

    # Check GPU
    click.echo("\nGPU:")
    try:
        import torch

        if torch.cuda.is_available():
            click.echo(f"  [OK] CUDA available: {torch.cuda.get_device_name(0)}")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            click.echo("  [OK] MPS available (Apple Silicon)")
        else:
            click.echo("  [--] No GPU detected (CPU mode)")
    except ImportError:
        click.echo("  [--] PyTorch not installed")

    # Check API keys
    click.echo("\nAPI Keys:")
    import os

    keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    for key in keys:
        if os.environ.get(key):
            click.echo(f"  [OK] {key} is set")
        else:
            click.echo(f"  [--] {key} not set")


if __name__ == "__main__":
    main()
