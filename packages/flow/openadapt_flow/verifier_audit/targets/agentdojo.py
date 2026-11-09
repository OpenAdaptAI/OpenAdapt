"""AgentDojo (MIT, ``pip install agentdojo``) as an audit target.

Reference trajectory: each user task's shipped ``ground_truth`` tool calls,
then the task's ``GROUND_TRUTH_OUTPUT`` as the claim. Scorer: AgentDojo's own
utility check (``TaskSuite._check_task_result``, which prefers
``utility_from_traces`` and falls back to ``utility``). Ground truth for the
kit: a field-level diff of the environment's model dump before and after,
read independently of the utility function.

A call is a write when running it on the reference path changed the
environment; the kind is observed, not guessed from the tool name.

AgentDojo ends the episode at the final answer, so no step can follow the
claim and late-write cells refuse.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from openadapt_flow.verifier_audit.model import (
    Call,
    CallKind,
    Episode,
    Verdict,
    canonical_effect,
)

DEFAULT_VERSION = "v1.2.2"
ACCESS_LOGS = ("web.web_requests",)
"""State that records that a read happened, not a record the task writes.
Fetching a page appends here; counting it would make every repeated read a
write."""


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out or {prefix: {}}
    if isinstance(value, list):
        out = {}
        for i, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{i}]"))
        return out or {prefix: []}
    return {prefix: value}


def _clock_free(value: Any, window: tuple[datetime, datetime]) -> Any:
    """Tools stamp new records with the wall clock. A value that parses as a
    timestamp inside this run's window is the clock, not the task's data."""
    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and "T" in value:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return value
        if window[0] <= stamp <= window[1]:
            return "<run-clock>"
    return value


def _diff(
    pre: dict[str, Any], post: dict[str, Any], window: tuple[datetime, datetime]
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(set(pre) | set(post)):
        before = pre.get(path, "<absent>")
        after = _clock_free(post.get(path, "<absent>"), window)
        if before != after:
            rows.append({"path": path, "before": before, "after": after})
    return rows


class AgentDojoTarget:
    post_claim_steps = False

    def __init__(self, suite: str = "banking", version: str = DEFAULT_VERSION) -> None:
        from agentdojo.task_suite.load_suites import get_suite

        self.suite_name = suite
        self.version = version
        self.suite = get_suite(version, suite)
        self.name = f"agentdojo/{version}/{suite}"
        self._refs: dict[str, list[Call]] = {}

    def tasks(self) -> list[str]:
        return sorted(self.suite.user_tasks, key=lambda t: int(t.rsplit("_", 1)[-1]))

    def instruction(self, task: str) -> str:
        return str(self.suite.user_tasks[task].PROMPT)

    def _env(self):
        return self.suite.load_and_inject_default_environment({})

    def _runtime(self):
        from agentdojo.functions_runtime import FunctionsRuntime

        return FunctionsRuntime(self.suite.tools)

    def _snapshot(self, env) -> dict[str, Any]:
        flat = _flatten(env.model_dump(mode="json"))
        return {k: v for k, v in flat.items() if not k.startswith(ACCESS_LOGS)}

    def reference(self, task: str) -> list[Call]:
        if task in self._refs:
            return self._refs[task]
        user_task = self.suite.user_tasks[task]
        env = self._env()
        runtime = self._runtime()
        calls = []
        for fc in user_task.ground_truth(env.model_copy(deep=True)):
            before = self._snapshot(env)
            runtime.run_function(env, fc.function, dict(fc.args))
            kind = CallKind.WRITE if self._snapshot(env) != before else CallKind.READ
            calls.append(Call(fc.function, dict(fc.args), kind))
        calls.append(
            Call(
                "final_answer", {"text": user_task.GROUND_TRUTH_OUTPUT}, CallKind.CLAIM
            )
        )
        self._refs[task] = calls
        return calls

    def run(self, task: str, calls: Sequence[Call], *, score: bool) -> Episode:
        from agentdojo.functions_runtime import FunctionCall
        from agentdojo.types import text_content_block_from_string

        user_task = self.suite.user_tasks[task]
        env = self._env()
        pre_env = env.model_copy(deep=True)
        pre = self._snapshot(env)
        started = datetime.now() - timedelta(seconds=1)
        runtime = self._runtime()
        errors: dict[int, str] = {}
        trace: list[Any] = []
        output = ""
        claim_effect = None
        for i, call in enumerate(calls):
            if call.kind is CallKind.CLAIM:
                output = str(call.args.get("text", ""))
                claim_effect = canonical_effect(
                    _diff(pre, self._snapshot(env), (started, datetime.now()))
                )
                continue
            trace.append(FunctionCall(function=call.name, args=dict(call.args)))
            _result, error = runtime.run_function(env, call.name, dict(call.args))
            if error:
                errors[i] = error[:300]
        episode = Episode(
            final_effect=canonical_effect(
                _diff(pre, self._snapshot(env), (started, datetime.now()))
            ),
            claim_effect=claim_effect,
            call_errors=errors,
        )
        if score:
            try:
                ok = self.suite._check_task_result(
                    user_task,
                    [text_content_block_from_string(output)],
                    pre_env,
                    env,
                    trace,
                )
                episode.verdict = Verdict.PASS if ok else Verdict.FAIL
            except Exception as exc:  # noqa: BLE001 - a crashing scorer is a finding
                episode.verdict = Verdict.ERROR
                episode.scorer_detail = repr(exc)[:300]
        return episode


def make_target(**kwargs: Any) -> AgentDojoTarget:
    return AgentDojoTarget(**kwargs)
