"""Agent Skills emitter — WRAPS ``openadapt_flow.emit.skill.emit_skill``.

openadapt-flow already generates a portable skill folder
(``<slug>/SKILL.md`` + ``<slug>/bundle/``). This module does not
regenerate any of that; it calls flow's emitter and then APPENDS two
agent-facing sections the flow emitter does not cover:

1. how to invoke the workflow through this package's MCP server
   (``openadapt-agent serve``) instead of the raw CLI, and
2. the halt and attended-action semantics an agent must respect.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_agent.bundles import load_workflow_info

__all__ = ["emit_agent_skill"]

_APPENDIX_TEMPLATE = """\

## Invoking via MCP (openadapt-agent)

If the operator runs the local `openadapt-agent` MCP server over this
skill's bundle directory:

```bash
openadapt-agent serve --bundles {bundle_ref} --allow-run
```

this workflow appears as the opaque MCP tool `run_{workflow_id}`
(required parameters: {param_list}). Recorded demonstration values are
never placed in the tool schema and every declared parameter is required
by default. Prefer the MCP tool when it is available: it executes
through the governed `openadapt-flow run` path (fail-closed admission
gates) rather than the permissive `replay` demo path, and it returns a
PHI-safe structured outcome instead of raw CLI output. Inspection and
PHI-safe Needs Attention tools are always available; `run_{workflow_id}` exists only
when the operator started the server with `--allow-run`.

## Halt semantics (IMPORTANT)

A run has exactly one of these outcomes — report it faithfully:

- **success** — exit code 0 AND a precise `report.json` records
  `execution_outcome: VERIFIED` with a consistent success flag. A legacy
  report without the precise field must record `success: true`. Only then may
  you tell the user the workflow completed.
- **halt** (MCP status `halt`) — `execution_outcome` says whether the run
  halted, completed without sufficient verification, or completed a rollback.
  None is a verified success. Protected evidence remains in the local
  OpenAdapt operator experience; default MCP results contain only opaque IDs,
  fixed messages, and count/boolean metrics. Surface the exact outcome to the
  user; do not infer the business effect or retry blindly.
- **governed refusal** (exit code 2 / MCP status `refused`, `openadapt-flow
  run` only) — an admission gate refused the bundle before execution;
  NOTHING was executed. The printed coverage report names the failing
  gate.

Never summarize a halted, refused, or timed-out run as if it succeeded.

## Needs Attention

When a run halts, `list_needs_attention` and `get_attention_item` return
an opaque, PHI-safe queue card. Do not ask for or place credentials,
challenge answers, screenshots, observed text, or other protected values
in an attended-action payload.

If the operator enabled attended actions, use only the exact action tool
that matches their explicit decision:

- `continue_attention` only after the local operator says they completed
  the paused task in the live application. Flow revalidates the outcome
  and resumes after it; it never performs that completed action again.
- `skip_attention` only for an allowed, declared skip.
- `reject_attention` to terminate this run without dispatching a new
  action. Earlier run actions may have effects. Review the protected local
  report and transaction outcome. Use escalation if a qualified operator
  can still continue the run.
- `teach_attention` to request a corrective demonstration.
- `escalate_attention` to preserve the pause for qualified assistance.

Reload the item immediately before acting, pass its exact capability
digest, and use one stable idempotency key for retries of that same
request. The MCP server will separately elicit the person's confirmation;
a client without form elicitation must direct the operator to Flow's
attended console/CLI instead. Elicitation is host-mediated explicit
confirmation, not cryptographic human-presence or identity proof. Never
infer or answer that elicitation yourself, auto-retry an uncertain
delivery, or substitute one action for another.
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
    appendix = _APPENDIX_TEMPLATE.format(
        workflow_id=info.public_id,
        bundle_ref="<path-to-this-skill-folder>/bundle",
        param_list=_format_params(info.params),
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + appendix, encoding="utf-8")
    return skill_dir
