"""Hooks for coding agents: check each write an agent makes to a declared system.

Claude Code and Codex both run a command before and after each tool call
(``PreToolUse`` and ``PostToolUse``) and when the agent stops (``Stop``).
``openadapt hooks install --agent claude-code|codex`` writes that command
into the project. For a tool call that writes to a system declared in
``openadapt.systems.yaml``, the hook:

1. **Pre-check.** Matches the call against the system's declared writes and
   their approval mode, then allows it, asks a person, or blocks it. A write
   to a declared system that no entry covers is blocked (or asked about, by
   policy). Before an allowed write it reads the system of record, the
   baseline the read-back compares with.
2. **Read-back.** After the call, it reads the record back with the write's
   ``openadapt.verify/v1`` contract, through :mod:`openadapt_flow.verifier`:
   the same check ``verify-run``, guarded saves and ``openadapt verify``
   use. A failed or missing read-back is ``UNCERTAIN``, never a pass.
3. **Receipt.** It writes a signed ``record/run/v1`` receipt for the call
   with :mod:`openadapt_flow.receipt_record`, with any outcome.

Hooks see tool calls, not the network. An agent with its own credentials and
a shell can reach a system in ways no hook recognizes (a script, an
environment variable, an encoded command), so the hooks are advisory against
a determined agent. See ``docs/HOOKS.md``.
"""

from __future__ import annotations

__all__ = ["HOOKS_DIR", "SYSTEMS_FILE"]

#: The declared-systems file, at the project root.
SYSTEMS_FILE = "openadapt.systems.yaml"
#: The project-local folder for the launcher, state and receipts.
HOOKS_DIR = ".openadapt/hooks"
