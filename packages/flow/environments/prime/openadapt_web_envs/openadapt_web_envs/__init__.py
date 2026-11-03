"""OpenAdapt's record-checked web tasks as a ``verifiers`` v1 taskset.

Each task is one task of a running OpenAdapt OpenEnv server (MockMed,
MockLoan, or a self-hosted OpenEMR, openIMIS or Frappe Lending environment).
The rollout is verifiers' own OpenEnv driver: the model receives the
observation and the action schema, replies with one JSON action per turn,
and the episode reward is the server's, which comes from verify-run reading
the app's record, never from the model's report.

    python -m openadapt_web_envs.run_eval --env.taskset.base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import verifiers.v1 as vf
from verifiers.v1.tasksets.openenv.taskset import (
    OpenEnvData,
    OpenEnvEnv,
    OpenEnvTask,
)

#: The running OpenAdapt OpenEnv server, when --env.taskset.base-url is unset.
BASE_URL_ENV = "OPENADAPT_ENV_BASE_URL"


class OpenAdaptWebConfig(vf.TasksetConfig):
    base_url: str | None = None
    """The OpenAdapt OpenEnv server (default: $OPENADAPT_ENV_BASE_URL)."""
    split: str = "public"
    """Task split to load from the server's task API."""


class OpenAdaptWebTaskset(vf.Taskset[OpenEnvTask, OpenAdaptWebConfig]):
    """One verifiers task per task the OpenEnv server lists."""

    def load(self) -> Iterator[OpenEnvTask]:
        config = self.config
        base_url = config.base_url or os.environ.get(BASE_URL_ENV)
        if not base_url:
            raise ValueError(
                f"set --env.taskset.base-url or {BASE_URL_ENV} to a running "
                "OpenAdapt OpenEnv server"
            )
        base = base_url.rstrip("/")
        with httpx.Client(timeout=30) as http:
            name = http.get(f"{base}/list_environments").json()[0]
            listed = http.post(f"{base}/{name}/tasks", json={"split": config.split})
            listed.raise_for_status()
        for idx, task in enumerate(listed.json()["tasks"]):
            yield OpenEnvTask(
                OpenEnvData(
                    idx=idx,
                    name=task["id"],
                    prompt=None,
                    env=None,
                    base_url=base,
                    use_docker=False,
                    provider_kwargs={},
                    reset={"task_id": task["id"]},
                ),
                config.task,
            )


class OpenAdaptWebEnv(OpenEnvEnv):
    """verifiers' OpenEnv driver, unchanged."""


__all__ = ["OpenAdaptWebConfig", "OpenAdaptWebTaskset", "OpenAdaptWebEnv"]
