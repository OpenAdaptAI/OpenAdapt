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

import platform
import re
import sys
from pathlib import Path
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


@click.group(cls=_FlowFirstGroup)
@click.version_option(version=__version__, prog_name="openadapt")
def main():
    """OpenAdapt launcher for the openadapt-flow compiler.

    Compile a demonstrated workflow into deterministic local replay. Healthy
    runs make no model calls; configured checks can halt on ambiguity. Native
    and remote substrates use platform-specific extras and permissions;
    training/evaluation commands are research.

    \b
    Quick Start:
        python -m pip install --upgrade openadapt
        openadapt quickstart

    \b
    Manual Demo Lifecycle:
        openadapt flow demo-record --out rec
        openadapt flow compile rec --out bundle --name demo
        openadapt flow lint bundle --strict
        openadapt flow certify bundle --policy permissive
        openadapt flow replay bundle

    The manual demo is runnable but not certified for consequential work.
    Use `openadapt quickstart` for the effect-verified first run.
    """
    pass


# =============================================================================
# Flow Commands (the demonstration compiler — flagship path)
# =============================================================================


def _invoke_flow(argv: list[str]) -> int:
    """Invoke the canonical engine once and return its exit code."""
    try:
        from openadapt_flow.__main__ import main as flow_main
    except ImportError:
        click.echo("Error: openadapt-flow not installed.", err=True)
        click.echo("Reinstall with: pip install --upgrade openadapt", err=True)
        click.echo("Engine only: pip install openadapt-flow", err=True)
        return 1

    return int(flow_main(argv))


def _run_flow(argv: list[str]) -> None:
    """Delegate to the openadapt-flow CLI, preserving its exit code.

    openadapt-flow exposes an argparse ``main(argv)`` entry point; each
    ``openadapt flow <verb>`` click command reconstructs that verb's argv
    and hands off here so behavior is identical to ``openadapt-flow <verb>``.
    Imported lazily so ``openadapt`` (and ``openadapt flow --help``) work
    even when openadapt-flow isn't installed.
    """
    sys.exit(_invoke_flow(argv))


_DEFAULT_QUICKSTART_DIR = "openadapt-quickstart"
_PYTHON_REMEDY = (
    "OpenAdapt needs Python 3.10\u20133.12. Easiest fix: "
    "curl -LsSf https://astral.sh/uv/install.sh | sh && "
    "uv venv --python 3.12 && uv pip install openadapt\n"
    "Or use the installer script: https://raw.githubusercontent.com/"
    "OpenAdaptAI/openadapt-flow/main/scripts/install.sh"
)


def _echo_python_remedy() -> None:
    """Print the plain-language fix for an unsupported interpreter."""
    click.echo(_PYTHON_REMEDY, err=True)


def _require_supported_python() -> None:
    """Stop before delegation when pip cannot resolve this launcher.

    The requires-python bound means a >=3.13 interpreter fails pip
    resolution with raw resolver noise; give the remedy here instead.
    """
    if sys.version_info >= (3, 13):
        _echo_python_remedy()
        raise click.exceptions.Exit(1)


def _is_externally_managed_error(error: BaseException) -> bool:
    """Match pip's PEP 668 externally-managed-environment failure text."""
    return "externally-managed-environment" in str(error)


@main.command(
    "quickstart",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        f"Directory for the recording, bundle, and run report. Default: "
        f"{_DEFAULT_QUICKSTART_DIR}, suffixed -2, -3, ... when taken."
    ),
)
@click.option(
    "--headed", is_flag=True, help="Show the browser while the tutorial runs."
)
@click.option(
    "--simulate-rejected-write",
    "simulate_rejected_write",
    is_flag=True,
    help=(
        "After the verified run, simulate an application that reports success "
        "while its independent source rejects the write."
    ),
)
@click.option(
    "--break-it",
    "deprecated_break_it",
    is_flag=True,
    hidden=True,
)
@click.pass_context
def quickstart(
    command_ctx: click.Context,
    out: Optional[Path],
    headed: bool,
    simulate_rejected_write: bool,
    deprecated_break_it: bool,
) -> None:
    """Run a verified local tutorial against the bundled synthetic app.

    This is the shortest path to a real OpenAdapt run. It uses the bundled
    synthetic tutorial, verifies the write through an independent read-only
    system-of-record interface, keeps every artifact on this computer, and
    enables no model or Cloud call. The output directory is never overwritten.
    Any other flags (for example --guided or --interactive-record) pass
    through to the engine tutorial unchanged.
    """
    import os

    _require_supported_python()

    if deprecated_break_it:
        click.echo(
            "Warning: --break-it is deprecated; use --simulate-rejected-write.",
            err=True,
        )
    simulate_rejected_write = simulate_rejected_write or deprecated_break_it

    if out is None:
        root = Path(_DEFAULT_QUICKSTART_DIR).resolve()
        suffix = 2
        while root.exists():
            root = Path(f"{_DEFAULT_QUICKSTART_DIR}-{suffix}").resolve()
            suffix += 1
        click.echo(f"Using output directory: {root}")
    else:
        root = out.expanduser().resolve()
        if root.exists():
            raise click.UsageError(
                f"Output already exists: {root}. Pass --out with a new directory."
            )

    argv = [
        "tutorial",
        "--out",
        str(root),
        "--name",
        "local-quickstart",
    ]
    if headed:
        argv.append("--headed")
    if simulate_rejected_write:
        argv.append("--simulate-rejected-write")
    # Engine-owned flags (--guided, --interactive-record, and future engine
    # additions) forward verbatim instead of being whitelisted here.
    argv.extend(command_ctx.args)

    # The bundled tutorial contains only fixed synthetic data. Keep an
    # installed-but-unconfigured privacy provider from blocking this known-safe
    # fixture, then restore the operator's setting immediately.
    scrub = os.environ.get("OPENADAPT_FLOW_SCRUB")
    if scrub in (None, "auto"):
        os.environ["OPENADAPT_FLOW_SCRUB"] = "off"
    try:
        code = _invoke_flow(argv)
    except Exception as error:
        if _is_externally_managed_error(error):
            _echo_python_remedy()
            raise click.exceptions.Exit(1) from error
        raise
    finally:
        if scrub in (None, "auto"):
            if scrub is None:
                os.environ.pop("OPENADAPT_FLOW_SCRUB", None)
            else:
                os.environ["OPENADAPT_FLOW_SCRUB"] = scrub
    if code:
        raise click.ClickException(
            f"The verified tutorial stopped with exit code {code}. "
            f"Any completed artifacts remain in {root}."
        )

    click.echo("\nLocal quickstart complete.")
    click.echo(f"Bundle: {root / 'bundle'}")
    click.echo(f"Run evidence: {root / 'run'}")
    click.echo("Outcome: VERIFIED under the Standard profile.")
    click.echo(
        "The synthetic write was confirmed through a read-only system-of-record API."
    )
    click.echo("No model or Cloud call was enabled.")
    if simulate_rejected_write:
        click.echo(
            "The simulated rejected write HALTED as designed. The application "
            "reported success, but the independent source rejected the write."
        )
        click.echo(f"Simulation evidence: {root / 'run-rejected-write' / 'REPORT.md'}")
    click.echo(f"Inspect qualification gaps: openadapt flow lint {root / 'bundle'}")
    click.echo(
        "Record your first workflow: https://docs.openadapt.ai/guides/record-your-app/"
    )
    click.echo(
        "Connect this computer when you want Cloud history and collaboration: "
        "https://app.openadapt.ai/dashboard/settings/ingest"
    )
    click.echo("Qualify a consequential workflow: https://openadapt.ai/qualify")


_SECRET_REFERENCE = re.compile(r"^(?:env:[A-Z][A-Z0-9_]*|keychain:[^/\s]+/[^/\s]+)$")
_SUPPORTED_FLOW_RANGE = ">=1.29.0,<2.0.0"
_RDP_INSTALL_COMMAND = "python -m pip install 'openadapt[rdp]'"


def _supported_flow_version(value: str) -> bool:
    """Return whether a stable Flow version is in the launcher's supported range."""
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?(?:\+[a-zA-Z0-9.-]+)?", value
    )
    if match is None:
        return False
    parsed = tuple(int(part) for part in match.groups())
    return (1, 29, 0) <= parsed < (2, 0, 0)


@main.command("deploy")
@click.option(
    "--backend",
    type=click.Choice(["web", "windows", "macos", "linux", "rdp", "citrix"]),
    default="web",
    show_default=True,
    help="The execution surface that this customer deployment will use.",
)
@click.option(
    "--secret-ref",
    multiple=True,
    help="Secret reference only: env:NAME or keychain:service/item. Never pass a secret value.",
)
def deploy(backend: str, secret_ref: tuple[str, ...]) -> None:
    """Check this host and print the safe Flow/Desktop deployment path.

    This launcher command does not create another runtime or service manager.
    It records no secret value, starts no connector, and does not treat an
    incomplete preflight as a healthy deployment. It composes the installed
    Flow connector, operator console, and repair lifecycle instead.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version
    from importlib.util import find_spec

    click.echo("OpenAdapt deployment preflight")
    click.echo("=" * 30)
    click.echo("Environment fingerprint:")
    click.echo(f"  platform: {platform.system()} {platform.release()}")
    click.echo(f"  machine: {platform.machine() or 'unknown'}")
    click.echo(f"  python: {platform.python_version()}")
    try:
        flow_version = dist_version("openadapt-flow")
    except PackageNotFoundError:
        flow_version = "not installed"
    click.echo(f"  openadapt-flow: {flow_version}")
    click.echo(f"  requested backend: {backend}")

    bad_refs = [value for value in secret_ref if not _SECRET_REFERENCE.fullmatch(value)]
    click.echo("\nSecret references:")
    if not secret_ref:
        click.echo("  [--] none supplied (valid for a local-only preflight)")
    for value in secret_ref:
        if value in bad_refs:
            click.echo("  [INVALID] rejected secret reference (value hidden)")
        else:
            click.echo(f"  [OK] {value}")
    if bad_refs:
        raise click.UsageError(
            "Secret references must use env:NAME or keychain:service/item; "
            "do not pass secret values."
        )

    click.echo("\nHealth checks:")
    failures = []
    flow_importable = find_spec("openadapt_flow") is not None
    flow_version_ready = flow_version != "not installed" and _supported_flow_version(
        flow_version
    )
    if flow_importable and flow_version_ready:
        click.echo(
            "  [OK] canonical openadapt-flow engine is installed at a supported version"
        )
    elif not flow_importable or flow_version == "not installed":
        failures.append("flow")
        click.echo("  [MISSING] canonical openadapt-flow engine is not installed")
    else:
        failures.append("flow-version")
        click.echo(
            f"  [UNSUPPORTED] openadapt-flow {flow_version}; this launcher "
            f"requires {_SUPPORTED_FLOW_RANGE}"
        )

    if backend == "web":
        browser_ready = find_spec("playwright") is not None
        if browser_ready:
            click.echo("  [OK] Playwright is installed for the web backend")
        else:
            failures.append("browser")
            click.echo(
                "  [MISSING] the base OpenAdapt install does not contain "
                "Playwright. Run: python -m pip install --upgrade openadapt"
            )
    elif backend == "rdp":
        if find_spec("aardwolf") is not None:
            click.echo("  [OK] RDP transport dependency is installed")
        else:
            failures.append("rdp")
            click.echo(
                "  [MISSING] RDP transport dependency is not installed. Run: "
                + _RDP_INSTALL_COMMAND
            )
    else:
        click.echo(
            f"  [SETUP] {backend} readiness is checked by Flow when the "
            "configured target opens; this guide does not claim it is ready."
        )

    console_ready = all(
        find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "openadapt_types")
    )
    if console_ready:
        click.echo("  [OK] optional local operator console is installed")
    else:
        click.echo("  [--] optional local operator console is not installed")

    if failures:
        raise click.ClickException(
            "Preflight failed. Resolve every [MISSING], [UNSUPPORTED], and "
            "[SETUP] item above before service setup."
        )

    click.echo(
        "\nPreflight passed for the installed components. No service was started."
    )
    click.echo("\nGuided deployment path:")
    click.echo(
        "  1. Re-run full host diagnostics: openadapt doctor --backend " + backend
    )
    click.echo("  2. Open the authenticated Cloud connector settings:")
    click.echo("     https://app.openadapt.ai/dashboard/settings/connectors")
    click.echo(
        "     Create the customer-local connector there and put the issued "
        "references in its environment or OS keychain."
    )
    click.echo(
        "  3. Start one governed poll only after authenticated setup: "
        "openadapt flow connector run --profile deployment.yaml --once"
    )
    if console_ready:
        click.echo(
            "  4. Inspect local health, reports, and halt evidence: "
            "openadapt flow console --bundles bundles --runs runs"
        )
        click.echo(
            "     The console is loopback-only and read-only unless you explicitly "
            "enable its governed actions."
        )
    else:
        click.echo("  4. Optional local console setup:")
        click.echo(
            f"     python -m pip install 'openadapt-flow[console]=={flow_version}'"
        )
        click.echo(
            "     Re-run this preflight after installation before you start the console."
        )
    click.echo(
        "  5. In OpenAdapt Desktop, use the signed-in workspace connection and "
        "open the matching run report; do not use the Desktop view as proof of effect."
    )
    click.echo(
        "  6. Upgrade the launcher and its pinned Flow dependency: "
        "python -m pip install --upgrade openadapt"
    )
    click.echo(
        "  7. Roll back a governed bundle, not an unverified package: "
        "openadapt flow repair rollback --store repair-store"
    )
    click.echo(
        "  8. Uninstall only after retaining required run evidence: "
        "python -m pip uninstall openadapt openadapt-flow"
    )


@main.command(
    "flow",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        # Flow owns its argparse help. Do not let Click consume --help first.
        "help_option_names": [],
    },
)
@click.pass_context
def flow(command_ctx: click.Context) -> None:
    """Run the canonical demonstration compiler and governed runtime.

    Every argument passes to openadapt-flow unchanged. This keeps the launcher
    command list, help, options, validation, and exit codes identical to the
    installed engine version.
    """
    _run_flow(list(command_ctx.args))


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


# =============================================================================
# Capture Commands
# =============================================================================


@main.group()
def capture():
    """Standalone local human GUI capture (optional [capture] extra).

    For a compile-ready workflow recording, prefer `openadapt flow record`.
    These commands observe the current interactive desktop; they do not choose
    or connect a replay target.

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
            if not recorder.wait_for_ready():
                raise click.ClickException(
                    "Capture did not become ready. No successful capture was saved."
                )
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
    """Explain how to stop a capture started in another terminal."""
    raise click.ClickException(
        "No separate capture-stop control channel is available. Stop the capture "
        "with Ctrl+C in the recorder terminal. A separate stop command will remain "
        "unavailable until Capture provides an authenticated, owner-only local "
        "control channel."
    )


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
@click.option(
    "--backend",
    type=click.Choice(["web", "windows", "macos", "linux", "rdp", "citrix"]),
    default=None,
    help="Explain setup for one execution surface.",
)
def doctor(backend: str | None):
    """Check system requirements and dependencies."""
    click.echo("OpenAdapt System Check")
    click.echo("=" * 40)

    # Check Python version
    import platform

    click.echo(f"\nPython: {platform.python_version()}")
    click.echo(f"Platform: {platform.system()} {platform.release()}")

    from importlib.util import find_spec

    failures = []
    click.echo("\nSelected execution surface:")
    if backend == "rdp":
        if find_spec("aardwolf") is not None:
            click.echo(
                "  [OK] rdp: browser support is not required and the RDP "
                "transport dependency is installed. Flow checks the target "
                "and credentials when it opens the connection."
            )
        else:
            failures.append("rdp")
            click.echo(
                "  [MISSING] rdp: the RDP transport dependency is not installed. "
                "Run: " + _RDP_INSTALL_COMMAND
            )
    elif backend and backend != "web":
        click.echo(
            f"  [OK] {backend}: browser support is not required; "
            "no Playwright or Chromium setup will run. The selected "
            "native or remote driver is checked when that surface opens."
        )
    else:
        playwright = find_spec("playwright") is not None
        if not playwright:
            click.echo(
                "  [MISSING] Browser: the base install does not contain "
                "Playwright. Run `python -m pip install --upgrade openadapt`."
            )
        else:
            try:
                from openadapt_flow._browser_setup import _chromium_present

                chromium = _chromium_present()
            except Exception:
                chromium = False
            if chromium:
                click.echo("  [OK] Browser: Playwright and Chromium are ready.")
            else:
                click.echo(
                    "  [READY] Browser: Playwright is installed; the matching "
                    "Chromium downloads automatically on the first web action."
                )

    # Core packages: installed by the base `pip install openadapt`. Only
    # these are treated as required; a missing one is a real problem.
    click.echo("\nCore packages (installed with `pip install openadapt`):")
    core = [
        "openadapt_flow",
        "playwright",
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

    if failures:
        raise click.ClickException(
            "System check failed. Install each required dependency shown as "
            "[MISSING], and then run this command again."
        )


if __name__ == "__main__":
    main()
