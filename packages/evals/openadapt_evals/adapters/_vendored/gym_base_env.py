# Vendored from https://github.com/RAGEN-AI/VAGEN
# These are pure abstract base classes with no heavy dependencies.
# Vendored to avoid requiring the full VAGEN installation.
# Last synced: 2026-03-02

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class GymBaseEnv(ABC):
    """
    Abstract async environment API.
    The handler does not assume any obs/data schema beyond what you return.

    Contract:
      - reset(seed) -> (obs, info)
      - step(action_str) -> (obs, reward, done, info)
    """

    def __init__(self, env_config: Dict[str, Any]):
        self.config = env_config

    @abstractmethod
    async def close(self) -> None:
        """Async teardown."""
        raise NotImplementedError

    @abstractmethod
    async def reset(self, seed: int):
        raise NotImplementedError

    @abstractmethod
    async def step(self, action_str: str):
        raise NotImplementedError

    @abstractmethod
    async def system_prompt(self):
        raise NotImplementedError
