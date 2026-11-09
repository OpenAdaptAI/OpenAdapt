"""Construct Flow's public persistent attended-action service."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = ["open_attended_service"]


@contextmanager
def open_attended_service(
    *,
    enabled: bool,
    deployment_config: Optional[str],
    url: Optional[str],
    headed: bool,
    allow_model_grounding: bool,
) -> Iterator[Optional[Any]]:
    """Open Flow's thread-owned service from one public DeploymentConfig.

    Agent never constructs a backend, Replayer, verifier, owner thread, or
    continuation. Flow owns the visible session and the complete attended
    decision lifecycle behind ``AttendedActionService.execute``.
    """
    if not enabled or not deployment_config:
        yield None
        return

    from openadapt_flow.deployment import load_deployment
    from openadapt_flow.runtime.durable import AttendedActionService

    deployment = load_deployment(Path(deployment_config))
    backend = deployment.backend
    runtime = deployment.runtime
    if url is not None:
        backend = backend.model_copy(update={"url": url})
    if headed:
        backend = backend.model_copy(update={"headed": True})
    if allow_model_grounding:
        runtime = runtime.model_copy(update={"allow_model_grounding": True})
    deployment = deployment.model_copy(
        update={
            "backend": backend,
            "runtime": runtime,
        }
    )

    with AttendedActionService(deployment) as service:
        yield service
