"""
Command-line interface for OpenAdapt Agent.

Provides commands for starting, managing, and monitoring agent sessions.
"""

import click
import logging

from openadapt_agent import __version__
from openadapt_agent.config import AgentConfig, SafetyMode
from openadapt_agent.session import SessionManager, SessionState


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--config", "-c",
    type=click.Path(exists=True),
    help="Path to configuration file",
)
@click.pass_context
def main(ctx, config):
    """OpenAdapt Agent - Production execution engine for GUI automation."""
    ctx.ensure_object(dict)

    if config:
        from pathlib import Path
        ctx.obj["config"] = AgentConfig.from_file(Path(config))
    else:
        ctx.obj["config"] = AgentConfig()


@main.command()
@click.argument("goal")
@click.option(
    "--safety-mode", "-s",
    type=click.Choice(["disabled", "permissive", "standard", "strict", "paranoid"]),
    default="standard",
    help="Safety enforcement mode",
)
@click.option(
    "--max-steps", "-n",
    type=int,
    default=100,
    help="Maximum steps before automatic stop",
)
@click.pass_context
def start(ctx, goal, safety_mode, max_steps):
    """Start a new agent session with the specified goal.

    Example:
        openadapt-agent start "Open Notepad and type hello world"
    """
    config = ctx.obj["config"]
    config.safety_mode = SafetyMode(safety_mode)
    config.execution.max_steps = max_steps

    click.echo(f"Starting agent session...")
    click.echo(f"  Goal: {goal}")
    click.echo(f"  Safety mode: {safety_mode}")
    click.echo(f"  Max steps: {max_steps}")

    # TODO: Initialize policy and executor
    click.echo("\nNote: Full execution requires a trained policy from openadapt-ml")
    click.echo("Install with: pip install openadapt-agent[ml]")


@main.command()
@click.pass_context
def status(ctx):
    """Check the status of running agent sessions."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    active = manager.get_active_sessions()

    if not active:
        click.echo("No active sessions")
        return

    click.echo(f"Active sessions: {len(active)}")
    for session in active:
        click.echo(f"\n  Session: {session.session_id[:8]}...")
        click.echo(f"  State: {session.state.value}")
        click.echo(f"  Goal: {session.goal[:50]}...")
        click.echo(f"  Steps: {session.step_count}")
        if session.duration_seconds:
            click.echo(f"  Duration: {session.duration_seconds:.1f}s")


@main.command()
@click.argument("session_id")
@click.pass_context
def pause(ctx, session_id):
    """Pause a running session."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    try:
        session = manager.load_session(session_id)
        session.pause()
        manager.save_session(session)
        click.echo(f"Paused session {session_id}")
    except FileNotFoundError:
        click.echo(f"Session not found: {session_id}", err=True)


@main.command()
@click.argument("session_id")
@click.pass_context
def resume(ctx, session_id):
    """Resume a paused session."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    try:
        session = manager.load_session(session_id)
        session.resume()
        manager.save_session(session)
        click.echo(f"Resumed session {session_id}")
    except FileNotFoundError:
        click.echo(f"Session not found: {session_id}", err=True)


@main.command()
@click.argument("session_id", required=False)
@click.pass_context
def stop(ctx, session_id):
    """Stop a running or paused session."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    if not session_id:
        # Stop all active sessions
        active = manager.get_active_sessions()
        if not active:
            click.echo("No active sessions to stop")
            return

        for session in active:
            session.stop(reason="CLI stop command")
            manager.save_session(session)
            click.echo(f"Stopped session {session.session_id}")
    else:
        try:
            session = manager.load_session(session_id)
            session.stop(reason="CLI stop command")
            manager.save_session(session)
            click.echo(f"Stopped session {session_id}")
        except FileNotFoundError:
            click.echo(f"Session not found: {session_id}", err=True)


@main.group()
def session():
    """Session management commands."""
    pass


@session.command("list")
@click.option(
    "--state", "-s",
    type=click.Choice(["created", "running", "paused", "completed", "failed", "stopped"]),
    help="Filter by session state",
)
@click.option("--limit", "-n", type=int, default=20, help="Maximum sessions to show")
@click.pass_context
def session_list(ctx, state, limit):
    """List sessions."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    state_filter = SessionState(state) if state else None
    sessions = manager.list_sessions(state=state_filter, limit=limit)

    if not sessions:
        click.echo("No sessions found")
        return

    click.echo(f"Sessions ({len(sessions)} shown):\n")

    for s in sessions:
        status_icon = {
            SessionState.RUNNING: "->",
            SessionState.PAUSED: "||",
            SessionState.COMPLETED: "OK",
            SessionState.FAILED: "XX",
            SessionState.STOPPED: "--",
            SessionState.CREATED: "..",
        }.get(s.state, "??")

        duration = f"{s.duration_seconds:.0f}s" if s.duration_seconds else "-"
        click.echo(f"  [{status_icon}] {s.session_id[:8]} | {s.step_count:3d} steps | {duration:>6} | {s.goal[:40]}...")


@session.command("show")
@click.argument("session_id")
@click.pass_context
def session_show(ctx, session_id):
    """Show details of a specific session."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    try:
        session = manager.load_session(session_id)
    except FileNotFoundError:
        click.echo(f"Session not found: {session_id}", err=True)
        return

    click.echo(f"Session: {session.session_id}")
    click.echo(f"  State: {session.state.value}")
    click.echo(f"  Goal: {session.goal}")
    click.echo(f"  Created: {session.created_at}")
    if session.started_at:
        click.echo(f"  Started: {session.started_at}")
    if session.completed_at:
        click.echo(f"  Completed: {session.completed_at}")
    click.echo(f"  Steps: {session.step_count}")
    if session.duration_seconds:
        click.echo(f"  Duration: {session.duration_seconds:.1f}s")
    if session.completion_reason:
        click.echo(f"  Reason: {session.completion_reason}")
    if session.metadata:
        click.echo(f"  Metadata: {session.metadata}")


@session.command("delete")
@click.argument("session_id")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@click.pass_context
def session_delete(ctx, session_id, force):
    """Delete a session."""
    config = ctx.obj["config"]
    manager = SessionManager(session_dir=config.session_dir)

    if not force:
        click.confirm(f"Delete session {session_id}?", abort=True)

    try:
        manager.delete_session(session_id)
        click.echo(f"Deleted session {session_id}")
    except FileNotFoundError:
        click.echo(f"Session not found: {session_id}", err=True)


@main.command()
def info():
    """Show agent configuration and status."""
    from openadapt_agent import has_safety_module

    click.echo("OpenAdapt Agent Info")
    click.echo(f"  Version: {__version__}")
    click.echo(f"  Safety module available: {has_safety_module()}")

    # Check for optional dependencies
    deps = {
        "openadapt-ml": "openadapt_ml",
        "openadapt-grounding": "openadapt_grounding",
        "openadapt-capture": "openadapt_capture",
        "openadapt-tray": "openadapt_tray",
        "openadapt-evals": "openadapt_evals",
    }

    click.echo("\nOptional dependencies:")
    for name, module in deps.items():
        try:
            __import__(module)
            status = "installed"
        except ImportError:
            status = "not installed"
        click.echo(f"  {name}: {status}")


if __name__ == "__main__":
    main()
