"""OpenEnv entry point: ``uvicorn server.app:app --port 8000``."""

from pathlib import Path

from openadapt_flow.rl_env.server import build_app

app = build_app(Path(__file__).resolve().parents[1])
