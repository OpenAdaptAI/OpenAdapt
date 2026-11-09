"""Data model and the target interface for a verifier audit."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable


class CallKind(str, Enum):
    """What one trajectory step does to the system of record."""

    READ = "read"
    WRITE = "write"
    CLAIM = "claim"
    """The step where the agent declares the task done (AppWorld's
    ``complete_task``, a final answer). Late-write faults move a write past
    it."""
    AUTH = "auth"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Call:
    """One step of a trajectory: a tool or API call with its arguments.

    ``name`` is the tool name or ``METHOD /path``. ``args`` holds the payload.
    ``kind`` is set by the target adapter, never guessed by the kit.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    kind: CallKind = CallKind.READ
    payload: dict[str, Any] = field(default_factory=dict)
    """Anything the adapter needs to replay the call that is not an argument
    (an HTTP method, a raw URL). Families never edit it except through
    :meth:`with_args`."""

    def with_args(self, args: dict[str, Any], *, name: Optional[str] = None) -> "Call":
        return Call(
            name=name or self.name,
            args=args,
            kind=self.kind,
            payload=dict(self.payload),
        )

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args, "kind": self.kind.value}


Effect = tuple[str, ...]
"""A canonical multiset of effect rows: each row is a sorted-key JSON string
naming one added, removed or updated record. Two effects are equal only when
every row appears the same number of times, so a duplicated insert differs
from a single one."""


def canonical_effect(rows: Sequence[Any]) -> Effect:
    return tuple(sorted(json.dumps(row, sort_keys=True, default=str) for row in rows))


def effect_diff(reference: Effect, observed: Effect) -> dict[str, list[str]]:
    """Rows present in one effect and not the other, counted as multisets."""
    ref, obs = Counter(reference), Counter(observed)
    return {
        "missing": sorted((ref - obs).elements()),
        "extra": sorted((obs - ref).elements()),
    }


@dataclass
class Episode:
    """The outcome of replaying one trajectory from a fresh reset."""

    final_effect: Effect
    claim_effect: Optional[Effect] = None
    """The effect at the moment the claim step ran, when the target can read
    it. ``None`` means the target cannot observe claim-time state, and
    families whose harm is about ordering refuse."""
    verdict: Optional[Verdict] = None
    """The environment's own scorer verdict; ``None`` when the run was a
    census run that must not call the scorer."""
    call_errors: dict[int, str] = field(default_factory=dict)
    """Index in the replayed trajectory -> error text, for calls the
    environment refused."""
    scorer_detail: Any = None


@runtime_checkable
class AuditTarget(Protocol):
    """An environment under audit.

    ``run`` must start from a fresh reset every time, replay ``calls`` in
    order, read the end state through a path independent of the scorer, and
    call the environment's own scorer only when ``score`` is true.
    """

    name: str

    def tasks(self) -> list[str]: ...

    def reference(self, task: str) -> list[Call]: ...

    def run(self, task: str, calls: Sequence[Call], *, score: bool) -> Episode: ...


@runtime_checkable
class IncidentalCalls(Protocol):
    """Optional: legitimate extra calls the task permits, for the positive
    control. Without it the kit repeats the reference's own reads."""

    def incidental_calls(self, task: str, reference: Sequence[Call]) -> list[Call]: ...
