"""Unified CLI for OpenAdapt ecosystem.

Usage:
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


@click.group()
@click.version_option(version="1.0.0", prog_name="openadapt")
def main():
    """OpenAdapt - GUI automation with ML.

    Record demonstrations, train models, and evaluate agents.

    \b
    Quick Start:
        openadapt capture start --name my-task   # Record a demonstration
        openadapt train --capture my-task        # Train a model
        openadapt eval --checkpoint model.pt     # Evaluate the model
    """
    pass


# =============================================================================
# Capture Commands
# =============================================================================


@main.group()
def capture():
    """Record GUI demonstrations.

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
    """Train ML models on captured demonstrations.

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
        click.echo(f"  Model: {model}")
        click.echo(f"  Output: {output}")

        # Import and run training
        from openadapt_ml.scripts.train import train_main

        train_main(
            capture=capture,
            model=model,
            config=config,
            output_dir=output,
            open_dashboard=open,
        )

    except ImportError:
        click.echo("Error: openadapt-ml not installed.", err=True)
        click.echo("Install with: pip install openadapt-ml", err=True)
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
    """Evaluate models on benchmarks.

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
    """Serve the training dashboard and viewer.

    \b
    Examples:
        openadapt serve --port 8080
        openadapt serve --output training_output --open
    """
    try:
        click.echo(f"Starting server on port {port}...")
        click.echo(f"Serving from: {output}")

        from openadapt_ml.cloud.local import serve_dashboard

        serve_dashboard(
            output_dir=output,
            port=port,
            open_browser=open,
        )

    except ImportError:
        click.echo("Error: openadapt-ml not installed.", err=True)
        sys.exit(1)


# =============================================================================
# Utility Commands
# =============================================================================


@main.command()
def version():
    """Show version information for all packages."""
    click.echo("OpenAdapt Ecosystem Versions:")
    click.echo("=" * 40)

    packages = [
        ("openadapt", "openadapt"),
        ("openadapt-capture", "openadapt_capture"),
        ("openadapt-ml", "openadapt_ml"),
        ("openadapt-evals", "openadapt_evals"),
        ("openadapt-viewer", "openadapt_viewer"),
        ("openadapt-grounding", "openadapt_grounding"),
        ("openadapt-retrieval", "openadapt_retrieval"),
    ]

    for name, module in packages:
        try:
            mod = __import__(module)
            ver = getattr(mod, "__version__", "unknown")
            click.echo(f"  {name}: {ver}")
        except ImportError:
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

    # Check required packages
    click.echo("\nCore packages:")
    required = [
        "openadapt_capture",
        "openadapt_ml",
        "openadapt_evals",
        "openadapt_viewer",
    ]
    for pkg in required:
        try:
            __import__(pkg)
            click.echo(f"  [OK] {pkg}")
        except ImportError:
            click.echo(f"  [MISSING] {pkg}")

    # Check optional packages
    click.echo("\nOptional packages:")
    optional = [
        "openadapt_grounding",
        "openadapt_retrieval",
    ]
    for pkg in optional:
        try:
            __import__(pkg)
            click.echo(f"  [OK] {pkg}")
        except ImportError:
            click.echo(f"  [--] {pkg} (not installed)")

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
