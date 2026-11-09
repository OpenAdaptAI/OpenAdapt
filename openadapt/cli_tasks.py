"""Launcher task views over the original Click commands and Flow passthrough."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

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

# These tasks are implemented by launcher-native commands, not the Flow parser.
LAUNCHER_TASKS = {
    "wrap": "Run an agent task once, then reuse its checked compiled tool"
}
TASKS.update(LAUNCHER_TASKS)

# The launcher supports released engines that predate the task view. Translate
# to their original verbs here too; the source-parity test keeps this table in
# step with Flow without raising the existing engine version floor.
FLOW_NAMES = {
    "admit": "run --task-admit",
    "api": "compile --api",
    "approve": "approve",
    "approve-sanitized": "approve --sanitized",
    "approver-grant": "setup approver-grant",
    "attach-seal": "verify attach-seal",
    "bench": "research flow-bench",
    "benchmark": "research benchmark",
    "bind-record-checks": "compile --bind-record-checks",
    "browser-accessibility": "setup browser-accessibility",
    "business-decisions": "services business-decisions",
    "certify": "qualify --certify",
    "check": "verify check",
    "cleanup": "setup cleanup",
    "compile": "compile",
    "compile-agent": "compile --agent",
    "compose": "compile --compose",
    "confirm-surfaces": "record confirm-surfaces",
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


def translate(
    argv: Sequence[str],
    native_commands: set[str],
    normalize: Callable[[str], str] | None = None,
) -> list[str]:
    """Match command prefixes only; preserve every unmatched argument byte-for-byte."""
    raw = list(argv)

    def matches(prefix: Sequence[str]) -> bool:
        if len(raw) < len(prefix):
            return False
        return all(
            (normalize(token) if normalize else token) == expected
            for token, expected in zip(raw, prefix)
        )

    for prefix in sorted(NATIVE_ROUTES, key=len, reverse=True):
        if matches(prefix):
            return [*NATIVE_ROUTES[prefix], *raw[len(prefix) :]]
    for old, new in sorted(
        FLOW_NAMES.items(), key=lambda item: len(item[1].split()), reverse=True
    ):
        flow_prefix = new.split()
        if new != old and matches(flow_prefix):
            return ["flow", old, *raw[len(flow_prefix) :]]
    name = normalize(raw[0]) if raw and normalize else (raw[0] if raw else "")
    if name in TASKS:
        if name not in native_commands or (
            name in {"verify", "setup"}
            and len(raw) > 1
            and not raw[1].startswith("-")
            and not matches(("verify", "serve"))
        ):
            return ["flow", name, *raw[1:]]
    return raw


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
        # Equality chooses a registered label; user text is never rendered.
        for alias, canonical in FLOW_NAMES.items():
            if argv[1] == alias:
                return (f"flow {alias}", canonical) if canonical != alias else None
        return None
    for native_canonical, native_old in NATIVE_ROUTES.items():
        if (
            native_old != native_canonical
            and tuple(argv[: len(native_old)]) == native_old
            and native_old != ("setup",)
        ):
            return " ".join(native_old), " ".join(native_canonical)
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

    aliases: dict[tuple[str, ...], tuple[str, ...]] = {
        tuple(new.split()): (old,) for old, new in FLOW_NAMES.items() if old != new
    }
    for native_route, native_alias in NATIVE_ROUTES.items():
        aliases[native_route] = native_alias
    view = argparse.ArgumentParser(prog=f"{prog} {task}", allow_abbrev=False)
    sub = view.add_subparsers(title="operations")
    for route in aliases:
        if route[0] == task:
            sub.add_parser(route[1], allow_abbrev=False)
    view.epilog = "Pass an operation followed by --help for its original arguments."
    return view.format_help()
