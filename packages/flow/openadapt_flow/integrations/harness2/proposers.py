"""Proposers for Harness²'s loop that need neither a coding agent nor Vertex.

Harness²'s ``Proposer`` runs each improvement session as a Claude Code or
OpenCode agent with file access, through Vertex AI. These subclasses keep all
of its session plumbing (the copied context workspace, harvesting the edited
harness, the audit trail, the cache) and replace only the session itself:

* :class:`ScriptedProposer` edits the harness by fixed rules from the
  evidence it is shown. Free and deterministic; the tests use it.
* :class:`ModelProposer` sends the evidence files and the mission to a
  :class:`~.llm.ChatModel` in one call and writes back the files it returns.
  The measured run uses it with a cheap model under a hard cap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harness2.core.proposer import Proposer

from openadapt_flow.integrations.harness2.agent import (
    RULE_IDENTITY,
    RULE_SAVE_ONCE,
    RULE_TYPE_LABEL,
)
from openadapt_flow.integrations.harness2.llm import ChatModel

_CHOICE_HINT = "```choice"


def _evidence_text(context: Path) -> str:
    parts = []
    for sub in ("rollouts", "deliverables", "pool", "candidates"):
        d = context / sub
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix in (".md", ".txt"):
                    parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


#: Evidence phrase -> the harness rule it teaches. The read-back phrases come
#: from ``adapter.readback_text``; the screen phrases are what an agent can see.
_LESSONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"landed on another patient's chart|is not in the referral list", re.I
        ),
        f"- Open a referral only after matching the patient's name, and confirm the "
        f"{RULE_IDENTITY} on the record before writing. If the patient is not "
        f"listed, stop and say so.",
    ),
    (
        re.compile(r"got \d+ records, not 1|save timed out", re.I),
        f"- {RULE_SAVE_ONCE[0].upper() + RULE_SAVE_ONCE[1:]}. If a save reports an "
        f"error, stop and report that it may have saved.",
    ),
    (
        re.compile(r"type Consult", re.I),
        f"- Choose the encounter type {RULE_TYPE_LABEL}, never by position.",
    ),
)


@dataclass
class ScriptedProposer(Proposer):
    """Deterministic edits and selections; no model."""

    def _claude_cli(
        self, cwd: Path, mission: str, system: str, wall: int
    ) -> tuple[str, float, str]:
        context = Path(cwd) / "context"
        evidence = _evidence_text(context)
        if _CHOICE_HINT in mission and (context / "candidates").is_dir():
            ids = sorted(p.stem for p in (context / "candidates").glob("*.md"))
            pick = next(
                (
                    i
                    for i in ids
                    if "status: saved"
                    in (context / "candidates" / f"{i}.md").read_text(encoding="utf-8")
                ),
                ids[0] if ids else "A",
            )
            return (
                f"```choice\n{pick}\n```\n<why>first candidate that reported saved</why>",
                0.0,
                "",
            )
        agents = context / "harness" / "AGENTS.md"
        body = agents.read_text(encoding="utf-8") if agents.is_file() else ""
        added = [
            rule for rx, rule in _LESSONS if rx.search(evidence) and rule not in body
        ]
        if added:
            agents.parent.mkdir(parents=True, exist_ok=True)
            agents.write_text(
                body.rstrip() + "\n" + "\n".join(added) + "\n", encoding="utf-8"
            )
        notes = f"added {len(added)} rule(s)" if added else "no change"
        return f"<notes>{notes}</notes>", 0.0, ""


_EDIT_FORMAT = """\
# HOW TO REPLY

You cannot run tools. The evidence files are inlined below. To change the
harness, output each changed file IN FULL as

<file path="harness/AGENTS.md">
...the whole new file...
</file>

Allowed paths: harness/AGENTS.md and harness/skills/<name>/SKILL.md. Omit a
file to keep it. Then explain your edits in <notes>...</notes>.
"""


@dataclass
class ModelProposer(Proposer):
    """One model call per session, with the evidence inlined."""

    model: Optional[ChatModel] = None
    max_evidence_chars: int = 60_000

    def _claude_cli(
        self, cwd: Path, mission: str, system: str, wall: int
    ) -> tuple[str, float, str]:
        if self.model is None:
            return "", 0.0, "ModelProposer has no model"
        context = Path(cwd) / "context"
        selecting = _CHOICE_HINT in mission and (context / "candidates").is_dir()
        files = _inline(context, self.max_evidence_chars)
        user = (
            mission
            + "\n\n"
            + ("" if selecting else _EDIT_FORMAT)
            + "\n# FILES\n\n"
            + files
        )
        reply = self.model.complete(
            system or "You improve an agent's harness.", user, max_tokens=3000
        )
        if not selecting:
            for path, body in re.findall(
                r'<file path="([^"]+)">\n?(.*?)</file>', reply.text, re.S
            ):
                rel = path.strip().lstrip("/")
                if not re.fullmatch(
                    r"harness/(AGENTS\.md|skills/[a-z0-9][a-z0-9_\-]*/SKILL\.md)", rel
                ):
                    continue
                dest = context / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(body, encoding="utf-8")
        return reply.text, reply.usd, ""


def _inline(context: Path, cap: int) -> str:
    out: list[str] = []
    used = 0
    for p in sorted(context.rglob("*")):
        if (
            not p.is_file()
            or p.suffix not in (".md", ".txt", ".json")
            or p.relative_to(context).parts[0] == "library"
        ):
            continue
        body = p.read_text(encoding="utf-8", errors="replace")
        room = cap - used
        if room <= 0:
            out.append(f"(omitted: {p.relative_to(context)})")
            continue
        if len(body) > room:
            body = body[:room] + "\n... [cut to fit]"
        used += len(body)
        out.append(f'<evidence path="{p.relative_to(context)}">\n{body}\n</evidence>')
    return "\n\n".join(out)


__all__ = ["ModelProposer", "ScriptedProposer"]
