# Vendored from https://github.com/RAGEN-AI/VAGEN
# These are pure abstract base classes with no heavy dependencies.
# Vendored to avoid requiring the full VAGEN installation.
# Last synced: 2026-03-02

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, Tuple

from .gym_base_env import GymBaseEnv


class GymImageEnv(GymBaseEnv):
    """
    GymImageEnv is a base environment class that supports optional
    **image-based multi-modal observations**, while keeping the same API
    as GymBaseEnv.

    --------------------------------------------------------------------
    Observation Protocol
    --------------------------------------------------------------------

    WITH images
    -------------------------------
    If the environment returns images, the observation should follow:

        obs = {
            "obs_str": "... <image> ...",
            "multi_modal_input": {
                "<image>": [PIL.Image.Image, ...]
            }
        }

    - Images are stored under obs["multi_modal_input"]["<image>"].
    - "<image>" in obs_str is a placeholder indicating where each image
      should appear in the prompt.
    - The number of "<image>" in obs_str should match the number of
      images in the list.

    WITHOUT images:
    ----------------------------------
    Can simply use:

        obs = {
            "obs_str": "..."
        }

    - "multi_modal_input" is optional and may be omitted.
    - obs_str should NOT contain "<image>" placeholders.


    --------------------------------------------------------------------
    Agent-Loop Rollout
    --------------------------------------------------------------------
    - sys      : system prompt (from system_prompt()).
    - init_obs : observation from reset().
    - step_obs : observation from step().
    - res_i    : agent response at step i.

    Concat mode (single growing context):
        sys + init_obs + res_0 + step_obs_1 + res_1 + ...

    Non-concat mode (step-wise independent contexts):
        Step 0: sys + init_obs   + res_0
        Step 1: sys + step_obs_1 + res_1
        Step 2: sys + step_obs_2 + res_2

    --------------------------------------------------------------------
    Info
    --------------------------------------------------------------------
    The `info` dict returned by reset() and step() may include:
        - success (bool): whether the task/episode is considered
          successful, this will be used for wandb logging.
    """

    def __init__(self, env_config: Dict[str, Any]):
        """
        Initialize the environment.

        Args:
            env_config (Dict[str, Any]):
                Environment configuration. The exact schema is defined by
                the concrete environment implementation and/or GymBaseEnv.

        Side effects:
            - Calls GymBaseEnv.__init__(env_config).
        """
        super().__init__(env_config)

    @abstractmethod
    async def close(self) -> None:
        """
        Close the environment and release all resources.

        This should clean up anything created by the environment, e.g.:
        - windows / renderers
        - subprocesses
        - file handles
        - GPU memory / models

        Returns:
            None
        """
        raise NotImplementedError

    @abstractmethod
    async def system_prompt(self) -> Dict[str, Any]:
        """
        Return the system-level prompt/observation for the environment.

        Returns:
            obs (Dict[str, Any]):
                A dict representing the system prompt observation.

                If returning images, it must follow:

                    obs = {
                        "obs_str": "... <image> ...",
                        "multi_modal_input": {
                            "<image>": [PIL.Image.Image, ...]
                        }
                    }

                If returning no images, it must follow:

                    obs = {
                        "obs_str": "..."
                    }
        """
        raise NotImplementedError

    @abstractmethod
    async def reset(self, seed: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Reset the environment to the initial state.

        Args:
            seed (int):
                Random seed used to initialize the environment

        Returns:
            obs (Dict[str, Any]):
                The initial observation after reset.

                If returning images, it must follow:

                    obs = {
                        "obs_str": "... <image> ...",
                        "multi_modal_input": {
                            "<image>": [PIL.Image.Image, ...]
                        }
                    }

                If returning no images, it must follow:

                    obs = {
                        "obs_str": "..."
                    }

            info (Dict[str, Any]):
                A dict containing any additional metadata about the reset,
                e.g. debug information, episode identifiers, etc.
        """
        raise NotImplementedError

    @abstractmethod
    async def step(
        self, action_str: str
    ) -> Tuple[Dict[str, Any], float, bool, Dict[str, Any]]:
        """
        Execute one environment step using an agent-provided action.

        Args:
            action_str (str):
                The action produced by the agent, in text form.

        Returns:
            obs (Dict[str, Any]):
                The next observation after applying the action.

                If returning images, it must follow:

                    obs = {
                        "obs_str": "... <image> ...",
                        "multi_modal_input": {
                            "<image>": [PIL.Image.Image, ...]
                        }
                    }

                If returning no images, it must follow:

                    obs = {
                        "obs_str": "..."
                    }

            reward (float):
                Scalar reward for the current step.

            done (bool):
                Whether the current episode has terminated after this
                step.

            info (Dict[str, Any]):
                Additional step-level metadata.

                Common optional keys:
                    - success (bool): whether the task/episode is
                      considered successful, typically used for logging
                      (e.g. wandb).
        """
        raise NotImplementedError
