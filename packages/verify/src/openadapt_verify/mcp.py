"""Existing MCP tools; optional SDK imports occur only when requested."""

from pathlib import Path
from typing import Any


def build_server(contracts_dir: Path, state_dir: Path) -> Any:
    from openadapt_flow.agent.mcp import build_server as canonical_server
    from openadapt_flow.agent.verify_tools import VerifyBridge

    return canonical_server(teach=VerifyBridge(contracts_dir, state_dir))


def serve(contracts_dir: Path, state_dir: Path) -> None:
    from openadapt_flow.agent.mcp import serve as canonical_serve
    from openadapt_flow.agent.verify_tools import VerifyBridge

    canonical_serve(teach=VerifyBridge(contracts_dir, state_dir))
