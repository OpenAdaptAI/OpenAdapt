"""Shared SDK import source for emitted MCP servers.

Keep generation independent of the optional SDK. Only an absent MCP 2 server
module selects MCP 1; a broken dependency or unsupported API must still fail.
"""

from __future__ import annotations


def server_import(
    server_type: str, import_error: str, context_type: str | None = None
) -> str:
    """Render imports using symbols reserved by the emitter's name allocator."""
    modern_context = (
        f"    from mcp.server.mcpserver import Context as {context_type}\n"
        if context_type
        else ""
    )
    legacy_context = (
        f"    from mcp.server.fastmcp import Context as {context_type}\n"
        if context_type
        else ""
    )
    return f"""try:
    from mcp.server.mcpserver import MCPServer as {server_type}
{modern_context}except ModuleNotFoundError as {import_error}:
    if {import_error}.name != "mcp.server.mcpserver":
        raise
    from mcp.server.fastmcp import FastMCP as {server_type}
{legacy_context}"""
