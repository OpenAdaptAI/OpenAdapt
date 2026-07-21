"""Emit compiled workflow bundles as agent-consumable surfaces.

- :func:`openadapt_flow.emit.skill.emit_skill` — an Agent Skills folder
  (``SKILL.md`` with YAML frontmatter) describing when and how to invoke
  the workflow via the ``openadapt-flow`` CLI.
- :func:`openadapt_flow.emit.mcp_tool.emit_mcp_server` — a standalone
  ``server.py`` exposing the workflow as a single MCP tool via FastMCP.
"""

from openadapt_flow.emit.mcp_tool import emit_mcp_server
from openadapt_flow.emit.skill import emit_skill

__all__ = ["emit_skill", "emit_mcp_server"]
