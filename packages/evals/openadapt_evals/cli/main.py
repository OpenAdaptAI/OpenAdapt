"""Main CLI entry point for OpenAdapt.

This provides the `oa` command with namespaced subcommands:
- `oa evals` - Benchmark evaluation commands (VM, run, view, etc.)

Future:
- `oa ml` - ML training commands (provided by openadapt-ml)

Usage:
    oa evals vm setup           # Setup Azure VM with WAA
    oa evals vm status          # Check VM status
    oa evals run --agent gpt-4o # Run live evaluation
    oa evals mock --tasks 10    # Run mock evaluation
    oa evals view               # View results
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the `oa` CLI."""
    parser = argparse.ArgumentParser(
        prog="oa",
        description="OpenAdapt CLI - GUI agent benchmark toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    oa evals vm setup           # Setup Azure VM with WAA
    oa evals run --agent gpt-4o # Run live evaluation
    oa evals mock --tasks 10    # Run mock evaluation
    oa evals view               # View results
""",
    )

    subparsers = parser.add_subparsers(dest="namespace", help="Command namespace")

    # Register 'evals' namespace
    evals_parser = subparsers.add_parser(
        "evals",
        help="Benchmark evaluation commands",
        description="Commands for running GUI agent benchmark evaluations",
    )
    _register_evals_commands(evals_parser)

    args = parser.parse_args(argv)

    if args.namespace is None:
        parser.print_help()
        return 0

    if args.namespace == "evals":
        return _dispatch_evals(args)

    return 0


def _register_evals_commands(parser: argparse.ArgumentParser) -> None:
    """Register all evaluation commands under 'oa evals'."""
    subparsers = parser.add_subparsers(dest="command", help="Evaluation command")

    # VM management commands
    vm_parser = subparsers.add_parser(
        "vm",
        help="VM management commands",
        description="Azure VM lifecycle and management",
    )
    _register_vm_commands(vm_parser)

    # Run evaluation
    run_parser = subparsers.add_parser(
        "run",
        help="Run live evaluation against WAA server",
        description="Run evaluation against a live WAA server",
    )
    run_parser.add_argument("--agent", default="gpt-4o", help="Agent type")
    run_parser.add_argument("--server", default="http://localhost:5001", help="WAA server URL")
    run_parser.add_argument("--tasks", type=int, help="Number of tasks (or use --task-ids)")
    run_parser.add_argument("--task-ids", help="Comma-separated task IDs")
    run_parser.add_argument("--output", "-o", help="Output directory")
    run_parser.add_argument("--run-name", help="Run name for results")
    run_parser.add_argument("--demo", help="Demo text or file path")
    run_parser.add_argument("--max-steps", type=int, default=15, help="Max steps per task")

    # Mock evaluation
    mock_parser = subparsers.add_parser(
        "mock",
        help="Run mock evaluation (no VM required)",
        description="Run evaluation with mock adapter for testing",
    )
    mock_parser.add_argument("--tasks", type=int, default=10, help="Number of tasks")
    mock_parser.add_argument("--agent", default="mock", help="Agent type")
    mock_parser.add_argument("--output", "-o", help="Output directory")
    mock_parser.add_argument("--run-name", help="Run name for results")
    mock_parser.add_argument("--demo", help="Demo text or file path")
    mock_parser.add_argument("--max-steps", type=int, default=15, help="Max steps per task")

    # Probe server
    probe_parser = subparsers.add_parser(
        "probe",
        help="Check if WAA server is ready",
        description="Probe WAA server health endpoint",
    )
    probe_parser.add_argument("--server", default="http://localhost:5001", help="WAA server URL")
    probe_parser.add_argument("--wait", action="store_true", help="Wait for server to be ready")
    probe_parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds")

    # View results
    view_parser = subparsers.add_parser(
        "view",
        help="Generate results viewer",
        description="Generate HTML viewer for evaluation results",
    )
    view_parser.add_argument("--run-name", help="Run name to view")
    view_parser.add_argument("--output", "-o", default="benchmark_results", help="Results directory")
    view_parser.add_argument("--port", type=int, default=9000, help="Server port")
    view_parser.add_argument("--no-open", action="store_true", help="Don't open browser")

    # List tasks
    tasks_parser = subparsers.add_parser(
        "tasks",
        help="List available benchmark tasks",
        description="List available WAA benchmark tasks",
    )
    tasks_parser.add_argument("--domain", help="Filter by domain")


def _register_vm_commands(parser: argparse.ArgumentParser) -> None:
    """Register VM management commands under 'oa evals vm'."""
    subparsers = parser.add_subparsers(dest="vm_action", help="VM action")

    # Setup (create + configure)
    setup_parser = subparsers.add_parser("setup", help="Full VM setup with WAA")
    setup_parser.add_argument("--vm-name", default="waa-eval-vm", help="VM name")
    setup_parser.add_argument("--resource-group", default="openadapt-agents", help="Resource group")
    setup_parser.add_argument("--vm-size", default="Standard_D8ds_v5", help="VM size")
    setup_parser.add_argument("--location", default="eastus", help="Azure region")

    # Status
    subparsers.add_parser("status", help="Show VM status")

    # Start/stop
    subparsers.add_parser("start", help="Start deallocated VM")
    subparsers.add_parser("stop", help="Stop VM")
    subparsers.add_parser("deallocate", help="Deallocate VM (stops billing)")

    # Delete
    delete_parser = subparsers.add_parser("delete", help="Delete VM and resources")
    delete_parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # Probe
    probe_parser = subparsers.add_parser("probe", help="Check WAA server status")
    probe_parser.add_argument("--wait", action="store_true", help="Wait for ready")

    # Logs
    logs_parser = subparsers.add_parser("logs", help="View container logs")
    logs_parser.add_argument("--lines", type=int, default=100, help="Number of lines")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")

    # Diagnostics
    subparsers.add_parser("diag", help="Show VM diagnostic info")

    # SSH
    subparsers.add_parser("ssh", help="Open SSH session to VM")

    # VNC
    subparsers.add_parser("vnc", help="Open VNC viewer")

    # Exec
    exec_parser = subparsers.add_parser("exec", help="Run command on VM")
    exec_parser.add_argument("--cmd", required=True, help="Command to run")

    # Monitor
    monitor_parser = subparsers.add_parser("monitor", help="Start monitoring dashboard")
    monitor_parser.add_argument("--details", action="store_true", help="Show detailed info")
    monitor_parser.add_argument("--auto-shutdown-hours", type=float, help="Auto-shutdown after N hours")


def _dispatch_evals(args: argparse.Namespace) -> int:
    """Dispatch evaluation commands."""
    if args.command is None:
        print("Usage: oa evals <command>")
        print("Commands: vm, run, mock, probe, view, tasks")
        print("Use 'oa evals <command> --help' for more info")
        return 0

    if args.command == "vm":
        return _dispatch_vm(args)
    elif args.command == "mock":
        return _cmd_mock(args)
    elif args.command == "run":
        return _cmd_run(args)
    elif args.command == "probe":
        return _cmd_probe(args)
    elif args.command == "view":
        return _cmd_view(args)
    elif args.command == "tasks":
        return _cmd_tasks(args)

    print(f"Unknown command: {args.command}")
    return 1


def _dispatch_vm(args: argparse.Namespace) -> int:
    """Dispatch VM commands."""
    if args.vm_action is None:
        print("Usage: oa evals vm <action>")
        print("Actions: setup, status, start, stop, deallocate, delete, probe, logs, diag, ssh, vnc, exec, monitor")
        return 0

    # Import VM commands lazily to avoid slow startup
    from openadapt_evals.cli import vm

    action = args.vm_action
    if action == "setup":
        return vm.cmd_setup(args)
    elif action == "status":
        return vm.cmd_status(args)
    elif action == "start":
        return vm.cmd_start(args)
    elif action == "stop":
        return vm.cmd_stop(args)
    elif action == "deallocate":
        return vm.cmd_deallocate(args)
    elif action == "delete":
        return vm.cmd_delete(args)
    elif action == "probe":
        return vm.cmd_probe(args)
    elif action == "logs":
        return vm.cmd_logs(args)
    elif action == "diag":
        return vm.cmd_diag(args)
    elif action == "ssh":
        return vm.cmd_ssh(args)
    elif action == "vnc":
        return vm.cmd_vnc(args)
    elif action == "exec":
        return vm.cmd_exec(args)
    elif action == "monitor":
        return vm.cmd_monitor(args)

    print(f"Unknown VM action: {action}")
    return 1


def _cmd_mock(args: argparse.Namespace) -> int:
    """Run mock evaluation."""
    # Delegate to existing CLI implementation
    from openadapt_evals.benchmarks.cli import cmd_mock
    return cmd_mock(args)


def _cmd_run(args: argparse.Namespace) -> int:
    """Run live evaluation."""
    from openadapt_evals.benchmarks.cli import cmd_live
    return cmd_live(args)


def _cmd_probe(args: argparse.Namespace) -> int:
    """Probe WAA server."""
    from openadapt_evals.benchmarks.cli import cmd_probe
    return cmd_probe(args)


def _cmd_view(args: argparse.Namespace) -> int:
    """Generate results viewer."""
    from openadapt_evals.benchmarks.cli import cmd_view
    return cmd_view(args)


def _cmd_tasks(args: argparse.Namespace) -> int:
    """List available tasks."""
    from openadapt_evals.benchmarks.cli import cmd_tasks
    return cmd_tasks(args)


if __name__ == "__main__":
    sys.exit(main())
