"""Run a fixed task through the existing agent-to-skill cache."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from openadapt_flow.integrations.agents.skills import SkillRun

_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class TaskArgumentsError(ValueError):
    """The call does not supply the task's declared inputs."""


class TaskCache(Protocol):
    """The existing SkillCache entry point used by the wrapper."""

    def run(self, prompt: str, params: Mapping[str, Any], agent: Any) -> SkillRun: ...

    def key(self, prompt: str, params: Mapping[str, Any]) -> dict[str, Any]: ...


class WrappedTask:
    """Expose one operator-configured task to a CLI or an MCP client.

    Callers supply input values. The instruction, record checks, cache and
    provider belong to the local operator. Execution stays in SkillCache,
    including its compiler, verifier, admission and emitted replay tool.
    """

    def __init__(
        self,
        *,
        cache: TaskCache,
        agent: Any,
        instruction: str,
        parameters: Sequence[str],
        runs_dir: str | Path = "~/.openadapt/wrap/runs",
    ) -> None:
        names = tuple(parameters)
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("the task needs an instruction")
        if isinstance(parameters, (str, bytes)) or any(
            not isinstance(name, str) or not _PARAMETER.fullmatch(name)
            for name in names
        ):
            raise ValueError("task parameter names must be identifiers")
        if len(names) != len(set(names)):
            raise ValueError("task parameter names must be unique")
        self.cache = cache
        self.agent = agent
        self.instruction = instruction
        self.parameters = names
        self._lock = threading.Lock()
        self.runs_dir = Path(runs_dir).expanduser()

    def input_schema(self) -> dict[str, Any]:
        """The complete input surface exposed to a calling agent."""
        return {
            "type": "object",
            "properties": {name: {"type": "string"} for name in self.parameters},
            "required": list(self.parameters),
            "additionalProperties": False,
        }

    def run(self, arguments: Mapping[str, Any]) -> SkillRun:
        """Execute once, preserving the cache's actual outcome and receipt.

        This method does not retry a failed or interrupted run. Transport
        request ownership is handled by the caller's existing run ledger.
        """
        values = self._values(arguments)
        # A before/after read pair must not straddle another run of this task.
        with self._lock:
            return self.cache.run(self.instruction, values, self.agent)

    def _values(self, arguments: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(arguments, Mapping) or set(arguments) != set(self.parameters):
            raise TaskArgumentsError("supply exactly the task's declared parameters")
        values = dict(arguments)
        if any(not isinstance(value, str) for value in values.values()):
            raise TaskArgumentsError("task parameter values must be strings")
        return values

    def call(self, request_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Reserve a transport request through the existing durable RunStore.

        Retrying the same request reads its original outcome. An interrupted
        owner leaves a reservation for reconciliation, never another write.
        """
        from openadapt_flow.agent.runs import RunStore

        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise TaskArgumentsError("request_id must contain 1 to 128 characters")
        values = self._values(arguments)
        identity = self.cache.key(self.instruction, values)
        workflow = "wrap:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        store = RunStore(self.runs_dir)
        begin = store.begin(
            request_id=request_id, workflow=workflow, inputs=values, mode="skill-cache"
        )
        envelope: dict[str, Any] = {
            "request_id": request_id,
            "run_id": begin.run_id,
            "replayed": begin.kind == "replay",
        }
        if begin.kind != "new":
            if begin.kind == "conflict":
                return dict(envelope, status="REQUEST_ID_CONFLICT", verified=False)
            record = begin.record or {}
            result = record.get("result")
            if record.get("state") == "finished" and isinstance(result, dict):
                return dict(result, replayed=True)
            return dict(envelope, status="RECONCILIATION_REQUIRED", verified=False)
        assert begin.record is not None
        try:
            result = dict(envelope, **_result(self.run(values), self.agent))
        except Exception:  # noqa: BLE001 - preserve uncertain writes without reflecting values
            # The call may have written before failing. A terminal, nonretryable
            # result keeps this request from being sent again.
            result = dict(envelope, status="RECONCILIATION_REQUIRED", verified=False)
        result["reason"] = "cached_tool_result"
        store.finish(begin.record, result)
        return result


def _result(run: SkillRun, agent: Any) -> dict[str, Any]:
    """Keep raw inputs, model summaries and exception text out of run records."""
    cost: float | None = None
    if run.path == "skill" and run.model_calls == 0:
        cost = 0.0
    elif run.path == "agent":
        from openadapt_flow.compile_agent.agents import PRICES_PER_MTOK

        ledger = getattr(agent, "last_ledger", None)
        if ledger is not None and (
            getattr(ledger, "prices", None)
            or getattr(ledger, "model", None) in PRICES_PER_MTOK
        ):
            cost = run.cost_usd
    result = {
        "path": run.path,
        "status": run.status,
        "verified": run.verified,
        "model_calls": run.model_calls,
        "cost_usd": cost,
        "seconds": run.seconds,
        "wire_checked": run.wire_checked,
        "receipt": run.replay.get("receipt"),
        "receipt_outcome": run.replay.get("receipt_outcome"),
    }
    return result
