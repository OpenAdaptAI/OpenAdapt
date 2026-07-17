"""openadapt-agent — the agent-facing bridge for openadapt-flow workflows.

**Status: Experimental (v2).** This package exposes compiled
`openadapt-flow <https://github.com/OpenAdaptAI/openadapt-flow>`_ workflow
bundles to other agents:

- an **MCP server** (``openadapt-agent serve`` / ``python -m
  openadapt_agent.mcp``) that presents each bundle as a tool whose
  execution shells out to the governed ``openadapt-flow run`` CLI — the
  server never reimplements or bypasses flow's policy / identity / effect
  gates; and
- an **Agent Skills emitter** (``openadapt-agent emit-skill``) that wraps
  flow's own ``emit-skill`` and appends agent-facing MCP invocation and
  halt-semantics guidance.

A halted or refused run is always surfaced as a structured halt/refusal
with an evidence pointer (the run's ``report.json``), never as success.

Version 1.x of this package (a legacy execution wrapper for model-driven
GUI agents) is deprecated; see the README for the pointer to the last
v0.1.x release.
"""

__version__ = "2.0.0.dev0"

__all__ = ["__version__"]
