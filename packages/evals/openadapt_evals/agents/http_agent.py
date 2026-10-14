"""HTTP-backed agent for remote agent-as-a-service integration.

Forwards observations to any HTTP endpoint and parses the response
into a BenchmarkAction. This lets external teams deploy their own
agent stack (model + prompt + parsing) as a black-box HTTP server
without coupling to openadapt-evals internals.

Protocol:
    POST {endpoint_url}/act
    Request:
        {
            "screenshot_b64": "<base64 PNG>",
            "instruction": "Click the Submit button",
            "task_id": "notepad_1",
            "viewport": [1920, 1200],
            "accessibility_tree": {...},
            "step_count": 3
        }
    Response:
        {
            "type": "click",
            "x": 0.5,
            "y": 0.3
        }

    GET {endpoint_url}/health  -> 200 OK

Example:
    from openadapt_evals.agents import HttpAgent

    agent = HttpAgent(endpoint_url="http://gpu-box:8080")
    action = agent.act(observation, task)
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent

logger = logging.getLogger(__name__)


class HttpAgent(BenchmarkAgent):
    """Agent that delegates to a remote HTTP endpoint.

    The remote server receives observation data (screenshot, task,
    accessibility tree) and returns an action dict that maps directly
    to BenchmarkAction fields.

    Args:
        endpoint_url: Base URL of the remote agent server (no trailing slash).
        timeout: Request timeout in seconds.
        headers: Optional extra HTTP headers (e.g. auth tokens).
    """

    def __init__(
        self,
        endpoint_url: str,
        timeout: int = 120,
        headers: dict[str, str] | None = None,
    ):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}
        self._step_count = 0

        logger.info("HttpAgent initialized: endpoint=%s", self.endpoint_url)

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Send observation to remote endpoint, parse response as BenchmarkAction."""
        self._step_count += 1

        # Encode screenshot
        screenshot_b64 = None
        if observation.screenshot:
            screenshot_b64 = base64.b64encode(observation.screenshot).decode("ascii")

        payload: dict[str, Any] = {
            "screenshot_b64": screenshot_b64,
            "instruction": task.instruction,
            "task_id": task.task_id,
            "viewport": list(observation.viewport) if observation.viewport else None,
            "accessibility_tree": observation.accessibility_tree,
            "step_count": self._step_count - 1,
        }

        try:
            resp = requests.post(
                f"{self.endpoint_url}/act",
                json=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        # `done` means "the agent finished the task" and the eval loop breaks
        # cleanly with no error marker; a sidecar that OOMed at step 0 would be
        # scored as an agent that ran and failed. `error` with an explicit
        # error_type is the shape the harness already knows how to exclude
        # (see claude_computer_use_agent).
        except requests.ConnectionError as e:
            logger.error("Connection failed: %s", e)
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error": f"connection_failed: {e}",
                    "error_type": "infrastructure",
                },
            )
        except requests.Timeout as e:
            logger.error("Request timed out: %s", e)
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error": f"timeout: {e}",
                    "error_type": "infrastructure",
                },
            )
        except requests.HTTPError as e:
            logger.error("HTTP error: %s", e)
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error": f"http_error: {e}",
                    "error_type": "infrastructure",
                },
            )
        except (ValueError, KeyError) as e:
            logger.error("Invalid response: %s", e)
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error": f"invalid_response: {e}",
                    "error_type": "infrastructure",
                },
            )

        return _parse_action_response(data)

    def reset(self) -> None:
        """Reset agent state and optionally notify remote endpoint."""
        self._step_count = 0
        try:
            requests.post(
                f"{self.endpoint_url}/reset",
                headers=self.headers,
                timeout=10,
            )
        except requests.RequestException:
            # Remote endpoint may not support /reset — that's fine
            pass

    def health_check(self) -> bool:
        """Check if the remote endpoint is reachable.

        Returns:
            True if GET /health returns 200, False otherwise.
        """
        try:
            resp = requests.get(
                f"{self.endpoint_url}/health",
                headers=self.headers,
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False


def _parse_action_response(data: dict[str, Any]) -> BenchmarkAction:
    """Convert a response dict into a BenchmarkAction.

    The response dict should have at minimum a ``type`` field. All other
    fields map directly to BenchmarkAction attributes.

    Args:
        data: Response dict from the remote agent.

    Returns:
        Parsed BenchmarkAction.
    """
    # No default: a body with no `type` is a malformed response, and
    # defaulting it to "done" reads as a clean agent finish.
    action_type = data.get("type")
    if not action_type:
        return BenchmarkAction(
            type="error",
            raw_action={
                "error": f"response has no 'type' field: {data!r}"[:500],
                "error_type": "agent",
            },
        )

    return BenchmarkAction(
        type=action_type,
        x=data.get("x"),
        y=data.get("y"),
        target_node_id=data.get("target_node_id"),
        target_bbox=tuple(data["target_bbox"]) if data.get("target_bbox") else None,
        target_role=data.get("target_role"),
        target_name=data.get("target_name"),
        text=data.get("text"),
        key=data.get("key"),
        modifiers=data.get("modifiers"),
        scroll_direction=data.get("scroll_direction"),
        scroll_amount=data.get("scroll_amount"),
        end_x=data.get("end_x"),
        end_y=data.get("end_y"),
        answer=data.get("answer"),
        raw_action=data,
    )
