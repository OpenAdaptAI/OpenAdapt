"""Task names that route to the existing CLI grammar and handlers.

Only an exact prefix is translated. In particular, operation selectors on a
command with a positional path must be the first argument after its name.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

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

# Values are presentation names. Keys always remain the original parser names.
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


def routes(names: Mapping[str, str]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Return the exact canonical prefixes that differ from legacy names."""
    return {tuple(new.split()): (old,) for old, new in names.items() if new != old}


FLOW_ROUTES = routes(FLOW_NAMES)
# Merge integration only: these capabilities are not registered by this change.
# Once their own registrar is present, the task view can expose it automatically.
INTEGRATION_NAMES = {"pilot": "verify pilot", "audit": "verify audit"}


def translate(
    argv: Sequence[str],
    aliases: Mapping[tuple[str, ...], tuple[str, ...]] = FLOW_ROUTES,
) -> list[str]:
    """Translate only a complete leading route; never scan option values."""
    for prefix in sorted(aliases, key=len, reverse=True):
        if tuple(argv[: len(prefix)]) == prefix:
            return [*aliases[prefix], *argv[len(prefix) :]]
    return list(argv)


_alias_notices = ContextVar("alias_notices", default=True)


@contextmanager
def suppress_alias_notices() -> Iterator[None]:
    """Let a launcher own its single interactive notice for a delegated call."""
    token = _alias_notices.set(False)
    try:
        yield
    finally:
        _alias_notices.reset(token)


def alias_notice(argv: Sequence[str], names: Mapping[str, str], prog: str) -> None:
    """Keep redirected streams and machine protocols byte-compatible."""
    if not _alias_notices.get() or not argv:
        return
    # Select display labels from the trusted registry, never from argv values.
    for alias, canonical in names.items():
        if argv[0] == alias:
            if canonical == alias:
                return
            break
    else:
        return
    if any(not stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return
    # A caller can explicitly request a machine stream even at a terminal.
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
    print(
        f"{prog} {alias} is also available as {prog} {canonical}",
        file=sys.stderr,
    )


def task_help(
    prog: str,
    prefix: Sequence[str],
    aliases: Mapping[tuple[str, ...], tuple[str, ...]],
    summaries: Mapping[str, str] | None = None,
) -> argparse.ArgumentParser:
    """Make a help view without adding arguments to an execution namespace."""
    view = argparse.ArgumentParser(prog=" ".join([prog, *prefix]), allow_abbrev=False)
    if not prefix:
        entries = TASKS
    else:
        entries = {
            route[len(prefix)]: (summaries or {}).get(" ".join(route), "")
            for route in aliases
            if len(route) > len(prefix) and tuple(route[: len(prefix)]) == tuple(prefix)
        }
    if entries:
        group = view.add_subparsers(title="commands" if not prefix else "operations")
        for name, help_text in entries.items():
            group.add_parser(name, help=help_text, allow_abbrev=False)
    if not prefix:
        view.add_argument(
            "--version", action="store_true", help="Show the version and exit"
        )
    if prefix:
        view.epilog = "Pass an operation followed by --help for its original arguments."
        if any(name.startswith("--") for name in entries):
            view.epilog += " An operation option must immediately follow the task name; use -- before a literal path beginning with '-'."
    return view


class TaskParser(argparse.ArgumentParser):
    """A root parser view; all real parsing stays in the original tree."""

    def format_help(self) -> str:
        view = task_help(self.prog, (), self.task_routes)
        view.description = self.description
        legacy: list[str] = []
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                hidden = {
                    action.choices[choice.dest]
                    for choice in action._choices_actions
                    if choice.help == argparse.SUPPRESS
                }
                legacy.extend(
                    name
                    for name, parser in action.choices.items()
                    if parser not in hidden
                )
        view.formatter_class = argparse.RawDescriptionHelpFormatter
        legacy_help = "Legacy commands (original spellings):\n" + "\n".join(
            f"  {name}" for name in sorted(legacy)
        )
        view.epilog = "\n\n".join(filter(None, [self.epilog, legacy_help]))
        return view.format_help()

    def parse_known_args(self, args=None, namespace=None):
        raw = list(sys.argv[1:] if args is None else args)
        translated = translate(raw, self.task_routes)
        if translated == raw and raw:
            prefix = tuple(raw[:1])
            original = next(
                (
                    action.choices
                    for action in self._actions
                    if isinstance(action, argparse._SubParsersAction)
                ),
                {},
            )
            grouped = any(route[:1] == prefix for route in self.task_routes)
            # Existing leaf/group grammars still own their bare invocation.
            if grouped and raw[0] not in original:
                view = task_help(
                    self.prog, prefix, self.task_routes, self._task_summaries()
                )
                if raw[1:] in (["-h"], ["--help"]):
                    view.print_help()
                    self.exit()
                view.error(
                    "an operation is required"
                    if len(raw) == 1
                    else f"unknown operation: {raw[1]}"
                )
        return super().parse_known_args(translated, namespace)

    def _task_summaries(self) -> dict[str, str]:
        choices = next(
            (
                action._choices_actions
                for action in self._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            [],
        )
        return {
            self.task_names[action.dest]: action.help or ""
            for action in choices
            if action.dest in self.task_names
        }

    def add_task_help(self) -> None:
        """Expose sibling operations in help, without changing a leaf grammar."""
        names = {**FLOW_NAMES, **INTEGRATION_NAMES}
        self.task_names = {
            name: names.get(name, name)
            for action in self._actions
            if isinstance(action, argparse._SubParsersAction)
            for name in action.choices
        }
        self.task_routes = routes(self.task_names)
        for action in self._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, parser in action.choices.items():
                canonical = self.task_names.get(name, name)
                lines = []
                if canonical != name:
                    lines.append(f"Also available as: {self.prog} {canonical}")
                operations = [
                    " ".join(route[1:])
                    for route in self.task_routes
                    if route[0] == name
                ]
                if operations:
                    lines.append(
                        "Related operations (immediately after the task name): "
                        + ", ".join(operations)
                    )
                if lines:
                    parser.epilog = "\n".join(
                        [*([parser.epilog] if parser.epilog else []), *lines]
                    )
                if canonical != name:
                    self._nested_alias_help(parser, canonical)

    def _nested_alias_help(
        self, parser: argparse.ArgumentParser, canonical: str
    ) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    path = f"{canonical} {name}"
                    child.epilog = "\n".join(
                        filter(
                            None,
                            [child.epilog, f"Also available as: {self.prog} {path}"],
                        )
                    )
                    self._nested_alias_help(child, path)
