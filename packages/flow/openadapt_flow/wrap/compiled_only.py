"""Dispatch to a task's admitted program only: a miss never starts an agent.

``SkillCache.run`` makes a first run when the task has no admitted program,
which hands the task to the agent with the normal compile and admission
settings. ``wrap-dispatch`` never does that, even when the program is
evicted between a status check and the call: it uses
:func:`compiled_only`, which reports ``NO_ADMITTED_PROGRAM`` instead.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from openadapt_flow.integrations.agents.skills import SkillRun

#: ``status`` of a dispatch that found no admitted program and ran nothing.
NO_ADMITTED_PROGRAM = "NO_ADMITTED_PROGRAM"


def missed(started: float) -> SkillRun:
    """The result of a dispatch with no admitted program: nothing ran."""

    return SkillRun(
        path="skill",
        status=NO_ADMITTED_PROGRAM,
        verified=False,
        model_calls=0,
        cost_usd=0.0,
        usage=[],
        usage_known=True,
        seconds=round(time.monotonic() - started, 3),
        detail="The task has no admitted program; nothing ran. "
        "openadapt wrap makes the first run.",
    )


def compiled_only(cache: Any, prompt: str, params: Mapping[str, Any]) -> SkillRun:
    """The cache's compiled replay or hold, or a miss. Never a first run.

    A cache without ``replay_only`` (not a SkillCache) can't promise that,
    so it is treated as a miss.
    """

    started = time.monotonic()
    replay_only = getattr(cache, "replay_only", None)
    if not callable(replay_only):
        return missed(started)
    served = replay_only(prompt, params)
    return served if served is not None else missed(started)


class CompiledOnly:
    """A TaskCache for :class:`~openadapt_flow.wrap.WrappedTask` that only
    replays. The agent ``WrappedTask`` passes is never used. A halt it sees
    keeps its record check as the fallback policy does, so ``wrap-dispatch
    release`` can tell a proved clean halt from one that may have written."""

    def __init__(self, cache: Any) -> None:
        self.cache = cache

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["cache"], name)

    def key(self, prompt: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.cache.key(prompt, params)

    def run(self, prompt: str, params: Mapping[str, Any], agent: Any) -> SkillRun:
        served = compiled_only(self.cache, prompt, params)
        if (
            served.path == "skill"
            and not served.verified
            and served.status
            not in (
                "CACHE_HALTED",
                NO_ADMITTED_PROGRAM,
            )
        ):
            # Keep the halted run's record check beside its halt when it
            # proves nothing was written, so release can rely on it.
            from openadapt_flow.wrap.fallback import keep_halt_evidence

            try:
                keep_halt_evidence(self.cache, prompt, params, served)
            except OSError:
                pass  # No evidence kept: release then needs reconciling.
        return served
