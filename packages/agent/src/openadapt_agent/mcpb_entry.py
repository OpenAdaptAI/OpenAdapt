"""Argument-safe MCPB entry point for the local OpenAdapt server."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from openadapt_agent.cli import main as cli_main

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def _boolean(environ: Mapping[str, str], name: str) -> bool:
    value = environ.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false")


def build_argv(environ: Mapping[str, str]) -> list[str]:
    """Translate MCPB user configuration into the existing typed CLI."""

    bundles_dir = environ.get("OPENADAPT_AGENT_BUNDLES_DIR", "").strip()
    if not bundles_dir:
        raise ValueError("Choose a workflow bundle directory before starting OpenAdapt")

    runs_dir = environ.get("OPENADAPT_AGENT_RUNS_DIR", "").strip() or "runs"
    argv = ["serve", "--bundles", bundles_dir, "--runs-dir", runs_dir]

    if _boolean(environ, "OPENADAPT_AGENT_ALLOW_RUN"):
        argv.append("--allow-run")
    if _boolean(environ, "OPENADAPT_AGENT_ALLOW_ATTENDED_ACTIONS"):
        argv.append("--allow-attended-actions")

    deployment_config = environ.get("OPENADAPT_AGENT_DEPLOYMENT_CONFIG", "").strip()
    if deployment_config:
        argv.extend(("--config", deployment_config))
    if _boolean(environ, "OPENADAPT_AGENT_HEADED"):
        argv.append("--headed")

    return argv


def main() -> int:
    try:
        argv = build_argv(os.environ)
    except ValueError as exc:
        print(f"openadapt-agent MCPB: {exc}", file=sys.stderr)
        return 2

    bundle_key = os.environ.get("OPENADAPT_AGENT_BUNDLE_KEY", "").strip()
    if bundle_key:
        os.environ["OPENADAPT_BUNDLE_KEY"] = bundle_key
    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
