"""Agent Skills emitter — WRAPS ``openadapt_flow.emit.skill.emit_skill``.

openadapt-flow already generates a portable skill folder
(``<slug>/SKILL.md`` + ``<slug>/bundle/``). This module does not
regenerate any of that; it calls flow's emitter and then APPENDS two
agent-facing sections the flow emitter does not cover:

1. how to invoke the workflow through this package's MCP server
   (``openadapt-agent serve``) instead of the raw CLI, and
2. the halt semantics an agent must respect (exit codes, run report
   evidence, and the rule that a halt is never a success).
"""

from __future__ import annotations

from pathlib import Path

from openadapt_agent.bundles import load_workflow_info, slugify

__all__ = ["emit_agent_skill"]

_APPENDIX_TEMPLATE = """\

## Invoking via MCP (openadapt-agent, Experimental)

If the operator runs the EXPERIMENTAL `openadapt-agent` MCP server over
this skill's bundle directory:

```bash
openadapt-agent serve --bundles {bundle_ref} --allow-run
```

this workflow appears as the MCP tool `run_{slug}` (parameters:
{param_list}). Prefer the MCP tool when it is available: it executes
through the governed `openadapt-flow run` path (fail-closed admission
gates) rather than the permissive `replay` demo path, and it returns a
structured outcome instead of raw CLI output. Read-only tools
`list_workflows`, `get_workflow`, and `get_run_report` are always
available; `run_{slug}` exists only when the operator started the server
with `--allow-run`.

## Halt semantics (IMPORTANT)

A run has exactly one of these outcomes — report it faithfully:

- **success** — exit code 0 AND `report.json` marks the run successful.
  Only then may you tell the user the workflow completed.
- **halt** (exit code 1 / MCP status `halt`) — the run executed and
  STOPPED at an unhandled state. This is the system working as designed
  (halt instead of guessing), but it is NOT a success: the workflow's
  effect did not fully happen. Evidence lives in the run directory's
  `report.json` (structured `halt` record: where it stopped, what it
  observed, what completed before) and `REPORT.md`. Surface the halt and
  its evidence to the user; do not retry blindly and never claim success.
- **governed refusal** (exit code 2 / MCP status `refused`, `openadapt-flow
  run` only) — an admission gate refused the bundle before execution;
  NOTHING was executed. The printed coverage report names the failing
  gate.

Never summarize a halted, refused, or timed-out run as if it succeeded.
"""


def _format_params(params: dict[str, str]) -> str:
    if not params:
        return "none"
    return ", ".join(f"`{name}`" for name in sorted(params))


def emit_agent_skill(bundle_dir: Path | str, out_dir: Path | str) -> Path:
    """Emit flow's skill folder for *bundle_dir*, then append MCP guidance.

    Returns the skill folder path (containing ``SKILL.md`` and
    ``bundle/``), exactly as flow's emitter lays it out.
    """
    from openadapt_flow.emit import emit_skill  # WRAP, don't duplicate

    skill_dir = emit_skill(bundle_dir, out_dir)
    info = load_workflow_info(Path(bundle_dir))
    slug = slugify(info.name or Path(bundle_dir).name)

    appendix = _APPENDIX_TEMPLATE.format(
        slug=slug,
        bundle_ref="<path-to-this-skill-folder>/bundle",
        param_list=_format_params(info.params),
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + appendix, encoding="utf-8"
    )
    return skill_dir
