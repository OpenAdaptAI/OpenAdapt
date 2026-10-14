"""OpenEnv HTTP+WebSocket server for WAA desktop environment.

Run standalone:
    python -m openadapt_evals.openenv.server --server-url http://localhost:5001

Or import and serve programmatically:
    from openadapt_evals.openenv.server import create_waa_app
    app = create_waa_app(server_url="http://localhost:5001")
    uvicorn.run(app, host="0.0.0.0", port=8000)

Requires: pip install "openenv-core[core]>=0.2.1" uvicorn
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)


def create_waa_app(
    server_url: str = "http://localhost:5001",
    evaluate_url: str | None = None,
    default_task_id: str | None = None,
    max_steps: int = 15,
    task_config_dir: str | None = None,
):
    """Create a FastAPI app serving WAAOpenEnvEnvironment.

    Requires openenv-core to be installed. Returns a FastAPI app
    that can be run with uvicorn.
    """
    from openenv.core.env_server.http_server import create_app

    from openadapt_evals.openenv.environment import WAAOpenEnvEnvironment
    from openadapt_evals.openenv.models import WAAAction, WAAObservation

    # create_app expects the class, not an instance.
    # We create a subclass that bakes in the config.
    class ConfiguredWAAEnv(WAAOpenEnvEnvironment):
        def __init__(self, **kwargs):
            super().__init__(
                server_url=server_url,
                evaluate_url=evaluate_url,
                default_task_id=default_task_id,
                max_steps=max_steps,
                task_config_dir=task_config_dir,
                **kwargs,
            )

    app = create_app(
        ConfiguredWAAEnv,
        WAAAction,
        WAAObservation,
        env_name="waa_desktop",
    )
    logger.info(
        "Created WAA OpenEnv server: server_url=%s, max_steps=%d",
        server_url,
        max_steps,
    )
    return app


def main():
    parser = argparse.ArgumentParser(description="WAA OpenEnv Server")
    parser.add_argument("--server-url", default="http://localhost:5001")
    parser.add_argument("--evaluate-url", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--task-config-dir", default=None)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    app = create_waa_app(
        server_url=args.server_url,
        evaluate_url=args.evaluate_url,
        default_task_id=args.task_id,
        max_steps=args.max_steps,
        task_config_dir=args.task_config_dir,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
