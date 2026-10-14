"""WAA (Windows Agent Arena) deployment module.

This module contains files that are deployed into the WAA Docker container:
- api_agent.py: API-based agent (Claude/GPT-5.1) for WAA
- Dockerfile: Custom waa-auto Docker image
"""

from openadapt_evals.waa_deploy.api_agent import ApiAgent

__all__ = ["ApiAgent"]
