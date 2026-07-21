"""openadapt-agent — the local agent bridge for openadapt-flow workflows.

This package exposes compiled
`openadapt-flow <https://github.com/OpenAdaptAI/openadapt-flow>`_ workflow
bundles and PHI-safe Needs Attention items to other agents:

- an **MCP server** (``openadapt-agent serve`` / ``python -m
  openadapt_agent.mcp``) that presents each bundle as a tool whose
  execution shells out to the governed ``openadapt-flow run`` CLI — the
  server never reimplements or bypasses flow's policy / identity / effect
  gates; and
- an **Agent Skills emitter** (``openadapt-agent emit-skill``) that wraps
  flow's own ``emit-skill`` and appends agent-facing MCP invocation and
  halt and attended-action guidance.

A halted or refused run is always surfaced as a PHI-safe structured
halt/refusal, never as success. Protected report evidence remains local
unless the operator explicitly enables protected export for a trusted
client in the same data boundary.
"""

__version__ = "2.0.0.dev0"

__all__ = ["__version__"]
