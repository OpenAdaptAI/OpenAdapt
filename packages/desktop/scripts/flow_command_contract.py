"""The exact Flow command lines Desktop builds, checked against the Flow it bundles.

Desktop pins one Flow release and talks to it only through command lines.
Unit tests fake the bridge, so a flag Desktop sends that the bundled Flow
doesn't know passes every test and fails every real run (Desktop 0.24.0 sent
``--config-sha256`` to a Flow that had none). This module drives the real
:class:`~openadapt_desktop.flow_bridge.FlowBridge` with a recording runner and
returns each command line it would run; the contract test parses them with
the installed Flow's own parser, and the frozen smoke runs them through the
bundled Flow.
"""

from __future__ import annotations

import ast
import io
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "openadapt_desktop" / "flow_bridge.py"

#: Flag literals in flow_bridge.py that aren't checked against Flow's parser,
#: and why. Anything else the bridge names must be a Flow flag.
NOT_FLOW_FLAGS = {
    # Desktop's own recorder entry point (flow_record_entry), not Flow.
    "--watch-parent-stdin": "Desktop's record entry point",
    # Sent only when ``run --help`` lists it (run_supports_authorization).
    "--authorization-file": "probed before use",
}

_REQUEST_ID = "0" * 32
_REQUEST_SHA = "sha256:" + "1" * 64
_ROOT = "a" * 64
_HEAD = (3, "sha256:" + "c" * 64)
_PUBLIC = "b" * 64


class _Recorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, cmd, **_kwargs):
        self.commands.append(list(cmd[1:]))
        stdout = '{"ok": true}\n'
        if cmd[1:3] == ["item-approval", "show"]:
            stdout = '{"schema": "openadapt.item-approval-list/v1", "requests": []}'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def popen(self, cmd, **_kwargs):
        self.commands.append(list(cmd[1:]))
        answer = '{"ok": false, "code": "contract", "message": "recorded"}\n'
        return SimpleNamespace(
            stdout=io.StringIO(answer),
            stdin=io.StringIO(),
            wait=lambda timeout=None: 1,
            kill=lambda: None,
        )

    def last(self) -> list[str]:
        return self.commands[-1]


class _Keychain:
    def __init__(self, in_app: bool) -> None:
        self.in_app = in_app

    def supported(self) -> bool:
        return self.in_app

    def stored(self):
        return SimpleNamespace(public_key=_PUBLIC) if self.in_app else None

    def sign(self, *_a, **_k):  # pragma: no cover - the recorder never asks
        raise AssertionError("the contract never signs")


@contextmanager
def _pinned(key_path: Path) -> Iterator[None]:
    """A pinned root and head, and a terminal-made key, for the answer path."""

    from openadapt_desktop import approver_key, approver_keychain

    saved = (
        approver_keychain.pinned_root,
        approver_keychain.pinned_head,
        approver_key.approver_key_environment,
    )
    approver_keychain.pinned_root = lambda: _ROOT  # type: ignore[assignment]
    approver_keychain.pinned_head = lambda: _HEAD  # type: ignore[assignment]
    approver_key.approver_key_environment = lambda *a, **k: (key_path, {})  # type: ignore[assignment]
    try:
        yield
    finally:
        (
            approver_keychain.pinned_root,
            approver_keychain.pinned_head,
            approver_key.approver_key_environment,
        ) = saved


def bridge_commands(workdir: Path) -> dict[str, list[str]]:
    """Each Flow command line Desktop builds for runs and answers, by name.

    Paths point into ``workdir`` and need not exist: these lines are for
    parsing, and a run of them must fail on the missing files, never on a
    flag.
    """

    from openadapt_desktop.flow_bridge import FlowBridge

    workdir = Path(workdir)
    bundle, config, run_dir = workdir / "bundle", workdir / "deployment.yaml", workdir / "run"
    params, inputs = workdir / "params.json", workdir / "inputs.json"
    digest = "0" * 64
    commands: dict[str, list[str]] = {}

    def bridge(in_app: bool) -> tuple[FlowBridge, _Recorder]:
        recorder = _Recorder()
        made = FlowBridge(
            flow_bin=sys.executable,
            runner=recorder.run,
            popen=recorder.popen,
            approver_keychain=_Keychain(in_app),
        )
        return made, recorder

    flow, rec = bridge(False)
    flow.run(
        bundle,
        config,
        out_dir=run_dir,
        params_file=params,
        approval_mode="each_item",
        approval_wait_s=600,
        config_sha256=digest,
    )
    commands["run"] = rec.last()
    flow.qualify_run_case(
        bundle,
        config,
        case_id="case-1",
        inputs_file=inputs,
        campaign_id="campaign-1",
        run_id="run-1",
        out_dir=run_dir,
        url="http://127.0.0.1:9/",
        approval_wait_s=600,
        config_sha256=digest,
    )
    commands["qualify run-case"] = rec.last()
    flow.item_approval_show(run_dir)
    commands["item-approval show"] = rec.last()

    with _pinned(workdir / "approver.pem"):
        signed, signed_rec = bridge(True)
        signed.item_approval_answer(
            run_dir, decision="approve", request_id=_REQUEST_ID, request_sha256=_REQUEST_SHA
        )
        commands["item-approval approve, signed in the app"] = signed_rec.last()
        flow.item_approval_answer(
            run_dir,
            decision="decline",
            request_id=_REQUEST_ID,
            request_sha256=_REQUEST_SHA,
            reason=_decline_reason(),
        )
        commands["item-approval decline, with a key file"] = rec.last()
    return commands


def _decline_reason() -> str:
    from openadapt_desktop.flow_bridge import DECLINE_REASONS

    return sorted(DECLINE_REASONS)[0]


def bridge_flag_literals(path: Path = BRIDGE) -> set[str]:
    """Every ``--flag`` string literal in flow_bridge.py that goes to Flow.

    The log redaction list (``redacted_after`` in ``_safe_command_for_log``)
    names arguments to hide wherever they appear, so its own entries don't
    count; a flag the bridge sends is a literal where the command is built.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    redaction: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "redacted_after" for t in node.targets)
        ):
            redaction.update(id(child) for child in ast.walk(node.value))
    found = {
        node.value
        for node in ast.walk(tree)
        if id(node) not in redaction
        and isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("--")
        and len(node.value) > 2
        and " " not in node.value
    }
    return found - set(NOT_FLOW_FLAGS)


def parser_option_strings(parser) -> set[str]:
    """Every option string anywhere in an argparse parser and its subcommands."""

    import argparse

    options: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
    return options


def argparse_refused(stderr: str) -> bool:
    """Whether a Flow process stopped in argument parsing (``usage:`` and
    ``error:``), as opposed to failing on the missing files it was given."""

    return "usage:" in stderr and "error:" in stderr
