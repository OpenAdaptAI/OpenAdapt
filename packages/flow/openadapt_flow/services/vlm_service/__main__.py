"""``serve`` entrypoint: run the VLM service on the GPU box with one command.

Example (production Linux GPU box, after ``vllm serve <model>``)::

    VLM_BACKEND=vllm \
    VLM_MODEL=mPLUG/GUI-Owl-1.5-8B-Instruct \
    VLM_VLLM_URL=http://localhost:8000/v1 \
    VLM_SERVICE_TOKEN=$(cat /etc/openadapt/vlm_token) \
        python -m openadapt_flow.services.vlm_service --host 0.0.0.0 --port 877

Example (Apple-Silicon dev box, local MLX model)::

    VLM_BACKEND=mlx VLM_SERVICE_TOKEN=devtoken \
        python -m openadapt_flow.services.vlm_service
"""

from __future__ import annotations

import argparse
import logging

from openadapt_flow.services.vlm_service.app import create_app
from openadapt_flow.services.vlm_service.config import (
    ServiceConfig,
    insecure_exposure_warnings,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="openadapt-flow on-prem VLM service")
    # Loopback by default: the service must not land on the network without an
    # explicit --host 0.0.0.0. Non-loopback binds (and empty tokens) warn loudly
    # below so an unauthenticated PHI endpoint can never happen unknowingly.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument(
        "--backend",
        default=None,
        help="stub|mlx|vllm (overrides VLM_BACKEND)",
    )
    parser.add_argument("--model", default=None, help="model id (overrides VLM_MODEL)")
    args = parser.parse_args(argv)

    import uvicorn

    cfg = ServiceConfig.from_env()
    if args.backend:
        cfg.backend = args.backend
    if args.model:
        cfg.model = args.model

    # Loudly surface an insecure exposure (empty token and/or non-loopback bind)
    # before the server starts serving PHI inference.
    for message in insecure_exposure_warnings(args.host, cfg.token):
        logger.warning(message)

    app = create_app(cfg)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
