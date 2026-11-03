"""The interface every comparison arm implements.

The harness owns the worklist and its ground truth, the app and every injected
event, the independent oracle, the simulated operator and the cost model. An
arm owns only how it sets itself up and how it does one item.

Two rules keep arms comparable:

- An arm never charges itself operator time. Every human touch (an approval,
  a stop, a question, a reconciliation, a repair review, an item done by hand)
  goes through ``operator.ask``, which returns the decision and the seconds the
  versioned cost model charges for it.
- An arm never judges itself. ``ItemResult.claimed`` is what the arm says; the
  oracle reads the system of record after every item and decides.

Arms are loaded from ``module:factory`` strings; ``factory(options) -> Arm``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

INTERFACE_VERSION = "lifecycle-arm/1"

RequestKind = Literal[
    "approval", "stop", "reconcile", "repair_review", "question", "manual_entry"
]


@dataclass(frozen=True)
class WorkItem:
    """One unit of work as the source document states it.

    ``params`` may be missing fields or hold malformed values: they are what
    the referral says, not what is true. The harness keeps the truth.
    """

    seq: int
    item_id: str
    day: int
    app: str
    params: dict[str, str]
    source_text: str


@dataclass(frozen=True)
class Env:
    app: str
    ui_url: str
    api_url: Optional[str]
    #: Synthetic (user, password) pairs the arm may sign in with.
    accounts: tuple[tuple[str, str], ...]
    #: The arm's private scratch directory for this run.
    workdir: Path
    seed: int
    #: App-specific extras (for example the fixture's container names).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorRequest:
    kind: RequestKind
    item_id: Optional[str]
    #: The product's own reason code, or the agent's question, verbatim.
    reason: str
    #: Where the request surfaced, e.g. "cli:openadapt-flow durable resume".
    surface: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorDecision:
    #: approve | deny | resume | skip | do_by_hand | accept_repair |
    #: reject_repair | answer | confirm_saved | confirm_not_saved
    action: str
    charged_s: float
    #: Which scripted-policy rule decided it.
    rule: str
    answer: Optional[str] = None


class Operator(Protocol):
    def ask(self, request: OperatorRequest) -> OperatorDecision: ...


@dataclass
class ItemResult:
    item_id: str
    claimed: Literal["done", "stopped", "error"]
    #: The product's final status or the agent's last line, verbatim.
    product_status: str
    #: The arm's own wall time for the item (first action to return).
    wall_s: float
    model_calls: int = 0
    cost_usd: float = 0.0
    #: Evidence path relative to the run directory.
    evidence: str = ""
    #: Maintenance the arm did inside this item (for example re-authoring a
    #: script after it failed on a changed screen). Same shape as a setup
    #: phase; charged as maintenance, not as per-item work.
    phases: list[dict[str, Any]] = field(default_factory=list)
    #: Free-form, kept in the ledger. ``detail["usage"]`` may hold a token
    #: breakdown: [{"model", "input", "output", "cache_read", "cache_write",
    #: "calls", "usd"}].
    detail: dict[str, Any] = field(default_factory=dict)


class Arm(Protocol):
    name: str

    def describe(self) -> dict[str, Any]:
        """The exact configuration, recorded in the ledger as is."""

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        """Teach, compile, qualify, admit. One dict per phase:
        ``{"phase", "wall_s", "attended_s", "model_calls", "cost_usd"}``.
        ``attended_s`` is the part of the wall time a person must watch."""

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult: ...

    def after_drift(
        self, event: dict[str, Any], env: Env, operator: Operator
    ) -> list[dict[str, Any]]:
        """Maintenance an arm does on its own initiative after a UI change it
        was told about (most arms are not told, and return [])."""

    def close(self) -> None: ...


class SpendCapReached(RuntimeError):
    """Raised by an arm before a model call that could cross a spend cap. The
    runner stops that arm, records the rest of its items as paused, and goes
    on with the next arm."""


def load_arm(spec: str, options: Optional[dict[str, Any]] = None) -> Arm:
    """Build an arm from ``module:factory``."""

    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"arm must look like module:factory, got {spec!r}")
    factory = getattr(importlib.import_module(module), name)
    return factory(dict(options or {}))
