"""Launcher task views over the original Click commands and Flow passthrough."""

from __future__ import annotations

import sys
from collections.abc import Sequence

TASKS = {
    "demo": "Try the demonstration and tutorial",
    "verify": "Check effects, receipts, and evidence",
    "record": "Record a task and its narration",
    "teach": "Teach a correction to a stopped workflow",
    "compile": "Compile and prepare workflow bundles",
    "run": "Run, resume, and route workflows",
    "approve": "Review and approve paused work",
    "undo": "Declare and run approved undo steps",
    "qualify": "Qualify a workflow for its intended use",
    "repair": "Review, test, and promote repairs",
    "inspect": "Inspect workflows, runs, and reports",
    "workflow": "Manage reusable workflow libraries",
    "services": "Run local services and connectors",
    "setup": "Configure connections, privacy, and access",
    "research": "Run benchmarks and research tools",
}

# The launcher supports released engines that predate the task view. Translate
# to their original verbs here too; the source-parity test keeps this table in
# step with Flow without raising the existing engine version floor.
FLOW_NAMES = {
    "admit": "run --admit",
    "api": "compile --api",
    "approve": "approve",
    "approve-sanitized": "approve --sanitized",
    "approver-grant": "setup approver-grant",
    "attach-seal": "verify attach-seal",
    "bench": "research flow-bench",
    "benchmark": "research benchmark",
    "bind-record-checks": "compile --bind-record-checks",
    "business-decisions": "services business-decisions",
    "certify": "qualify --certify",
    "check": "verify check",
    "cleanup": "setup cleanup",
    "compile": "compile",
    "compile-agent": "compile --agent",
    "compose": "compile --compose",
    "connect": "setup connect",
    "connector": "services connector",
    "console": "services console",
    "countersign": "verify countersign",
    "deferred": "verify deferred",
    "demo": "demo",
    "demo-record": "demo record",
    "disambiguate": "compile --disambiguate",
    "emit-mcp": "workflow emit-mcp",
    "emit-skill": "workflow emit-skill",
    "explain": "inspect explain",
    "fleet": "services fleet",
    "for-each": "run --for-each",
    "guard": "run --guard",
    "induce": "compile --induce",
    "intake": "teach --intake",
    "item-approval": "approve --item",
    "jit": "compile --jit",
    "known-ids": "verify known-ids",
    "learn": "teach --learn",
    "lint": "inspect lint",
    "local-seal": "verify local-seal",
    "login": "setup login",
    "materialize-approved": "compile --materialize-approved",
    "narration": "record narration",
    "no-outbound": "setup no-outbound",
    "process": "run --process",
    "propose-qualification": "qualify --propose",
    "pull": "workflow pull",
    "push": "workflow push",
    "qualify": "qualify",
    "receipt": "verify receipt",
    "record": "record",
    "redact-report": "inspect redact-report",
    "repair": "repair",
    "replay": "run --replay",
    "report-break": "inspect report-break",
    "report-run": "inspect report-run",
    "resume": "run --resume",
    "review-sanitized": "approve --review-sanitized",
    "rollback": "undo rollback",
    "route": "run --route",
    "run": "run",
    "sanitize": "compile --sanitize",
    "scaffold-verifier": "compile --scaffold-verifier",
    "seal": "setup encrypt",
    "sections": "record sections",
    "serve-execute": "services execute",
    "serve-reward": "services reward",
    "service": "services runner",
    "shadow": "verify shadow",
    "silent-saves": "research silent-saves",
    "skill": "workflow skill",
    "teach": "teach",
    "teach-authorization": "setup teach-authorization",
    "teach-branch": "teach --branch",
    "triage": "run --triage",
    "tutorial": "demo tutorial",
    "undo": "undo",
    "validate-hosted": "qualify --validate-hosted",
    "variants": "compile --variants",
    "verify-run": "verify effect",
    "visualize": "inspect visualize",
}


# Keep the native wrapper: it may have its own version checks or interception.
NATIVE_ROUTES = {
    ("demo", "quickstart"): ("quickstart",),
    ("record", "capture"): ("capture",),
    ("services", "agent"): ("agent",),
    ("services", "verifier"): ("verify", "serve"),
    ("setup", "clients"): ("setup",),
    ("setup", "connect"): ("connect",),
    ("setup", "deploy"): ("deploy",),
    ("setup", "doctor"): ("doctor",),
    ("setup", "version"): ("version",),
    ("verify", "seal"): ("seal",),
    ("verify", "shadow"): ("shadow",),
    ("verify", "ledger"): ("verify", "ledger"),
    ("research", "bench"): ("bench",),
    ("research", "eval"): ("eval",),
    ("research", "train"): ("train",),
    ("research", "dashboard"): ("serve",),
    ("workflow", "pull"): ("pull",),
}


def translate(argv: Sequence[str], native_commands: set[str]) -> list[str]:
    """Resolve exact native prefixes first, then use Flow's task grammar."""
    for prefix, target in NATIVE_ROUTES.items():
        if tuple(argv[: len(prefix)]) == prefix:
            return [*target, *argv[len(prefix) :]]
    flow_argv = list(argv)
    for old, new in FLOW_NAMES.items():
        prefix = new.split()
        if new != old and list(argv[: len(prefix)]) == prefix:
            flow_argv = [old, *argv[len(prefix) :]]
            break
    if argv and argv[0] in TASKS:
        # Bare setup and its options remain the legacy clients setup command.
        # The existing verify ledger/serve command grammar also stays intact.
        if argv[0] not in native_commands or (
            argv[0] in {"verify", "setup"}
            and len(argv) > 1
            and not argv[1].startswith("-")
            and argv[:2] != ["verify", "serve"]
        ):
            return ["flow", *flow_argv]
    return list(argv)


def alias_notice(argv: Sequence[str], prog: str) -> None:
    """Show old native spellings only when all three streams are terminals."""
    if not argv or any(
        not stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)
    ):
        return
    if any(
        token
        in {
            "-h",
            "--help",
            "--version",
            "--json",
            "--jsonl",
            "--stdio",
            "stdio",
            "json",
            "jsonl",
        }
        or token.startswith(("--format=", "--output-format=", "--transport="))
        for token in argv
    ):
        return
    names = legacy_names(argv)
    if names:
        old, new = names
        print(f"{prog} {old} is also available as {prog} {new}", file=sys.stderr)


def legacy_names(argv: Sequence[str]) -> tuple[str, str] | None:
    """Return the old and new display prefixes without including user values."""
    if argv[:1] == ["flow"] and len(argv) > 1:
        old = argv[1]
        canonical = FLOW_NAMES.get(old, old)
        return (f"flow {old}", canonical) if canonical != old else None
    for canonical, old in NATIVE_ROUTES.items():
        if old != canonical and tuple(argv[: len(old)]) == old and old != ("setup",):
            return " ".join(old), " ".join(canonical)
    return None


def alias_help(argv: Sequence[str], prog: str) -> str | None:
    """A requested help footer is separate from operational alias notices."""
    before_literal = list(argv[: argv.index("--")]) if "--" in argv else argv
    if not any(token in {"-h", "--help"} for token in before_literal):
        return None
    names = legacy_names(argv)
    return f"Also available as: {prog} {names[1]}" if names else None


def group_help(prog: str, task: str) -> str:
    """Combine the engine's task view with launcher-only operations."""
    import argparse

    aliases = {
        tuple(new.split()): (old,) for old, new in FLOW_NAMES.items() if old != new
    }
    aliases.update(NATIVE_ROUTES)
    view = argparse.ArgumentParser(prog=f"{prog} {task}")
    sub = view.add_subparsers(title="operations")
    for route in aliases:
        if route[0] == task:
            sub.add_parser(route[1])
    view.epilog = "Pass an operation followed by --help for its original arguments."
    return view.format_help()
