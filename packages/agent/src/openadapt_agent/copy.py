"""Canonical public sentences this package repeats.

README, server.json, llms.txt, and skill frontmatter share
``IDENTITY_SENTENCE``. The skill body adds ``SKILL_WHEN_TO_USE``.
"""

from __future__ import annotations

IDENTITY_SENTENCE = (
    "OpenAdapt is a compiled program for GUI writes with no API. "
    "This package invokes it over MCP."
)

SKILL_WHEN_TO_USE = (
    "When the user needs a repeating GUI write with no API and must prove "
    "persistence, call run_<slug>. If the tool returns HALTED, tell the user "
    "the record did not change."
)

SKILL_HONESTY = (
    "Never summarize halt, refused, timeout, or error as success. "
    "A local unsigned replay may complete. If the tool returns unsigned "
    "success, treat it as failure. Production success without a Seal is "
    "failure."
)

REQUIRES_SEAL_META = {"requires_seal": True}

SKILL_NAME = "openadapt-gui-write"

THREE_LINE_INSTALL = (
    "claude mcp add openadapt -- \\\n"
    "  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \\\n"
    "  serve --allow-run"
)
