"""openadapt-agent: your AI agent decides what to enter; OpenAdapt enters it.

This package connects a calling agent to
`openadapt-flow <https://github.com/OpenAdaptAI/openadapt-flow>`_, which
enters values in an app's own screens and reads the saved record back:

- an **MCP server** (``openadapt-agent serve`` / ``python -m
  openadapt_agent.mcp``) with three contract tools, ``list_workflows``,
  ``run_workflow``, and ``get_run``. Every result is ``done``,
  ``needs_review``, ``not_sure_if_saved``, or ``did_not_run``, derived from
  Flow's transaction outcome (:mod:`openadapt_agent.contract`). Operator
  bundles run through the governed ``openadapt-flow run`` CLI; the server
  never reimplements or bypasses Flow's policy, identity, or record checks.
  With no flags it serves a synthetic sandbox (:mod:`openadapt_agent.sandbox`).
- an **Agent Skills emitter** (``openadapt-agent emit-skill``) that writes a
  PHI-safe skill per workflow from its card.

Only a verified write is reported as done. Uncertain delivery is never safe
to retry, and the same ``request_id`` never writes twice. Protected report
evidence stays local unless the operator explicitly enables protected export
for a trusted client in the same data boundary.
"""

__version__ = "2.0.2"

__all__ = ["__version__"]
