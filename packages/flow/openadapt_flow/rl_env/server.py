"""Serve one environment directory over the OpenEnv protocol.

    python -m openadapt_flow.rl_env.server path/to/env --port 8000

The app is ``openenv.core.env_server.http_server.create_app`` over
:class:`~openadapt_flow.rl_env.environment.OpenAdaptWebEnvironment`, so the
routes (``/reset``, ``/step``, ``/state``, ``/schema``, ``/health``, ``/ws``)
and the task routes are OpenEnv's own.
"""

from __future__ import annotations

import argparse
import functools
import os
from pathlib import Path
from typing import Any

#: The environment directory, for ``uvicorn ...server:app`` style launches.
ENV_DIR_VAR = "OPENADAPT_ENV_DIR"
SCREENSHOTS_VAR = "OPENADAPT_ENV_SCREENSHOTS"


def build_app(env_dir: str | Path, *, max_concurrent_envs: int = 8) -> Any:
    from openenv.core.env_server.http_server import create_app

    from openadapt_flow.rl_env.environment import (
        OpenAdaptWebEnvironment,
        WebAction,
        WebObservation,
        WebState,
    )
    from openadapt_flow.rl_env.tasks import load_environment

    loaded = load_environment(env_dir)
    if loaded.manifest.app.kind == "desktop":
        from openadapt_flow.rl_env.desktop import build_app as build_desktop_app

        return build_desktop_app(loaded.directory)
    if loaded.manifest.app.kind == "external":
        # One app database serves every session, and a reset restores it for
        # everyone: concurrent episodes would see and wipe each other's writes.
        max_concurrent_envs = 1
    factory = functools.partial(
        OpenAdaptWebEnvironment,
        str(loaded.directory),
        screenshots=os.environ.get(SCREENSHOTS_VAR) == "1",
    )
    return create_app(
        factory,
        WebAction,
        WebObservation,
        env_name=loaded.manifest.name,
        max_concurrent_envs=max_concurrent_envs,
        state_cls=WebState,
    )


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("env_dir", nargs="?", default=os.environ.get(ENV_DIR_VAR))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-concurrent-envs", type=int, default=8)
    args = parser.parse_args(argv)
    if not args.env_dir:
        parser.error(f"name an environment directory or set {ENV_DIR_VAR}")
    app = build_app(args.env_dir, max_concurrent_envs=args.max_concurrent_envs)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
