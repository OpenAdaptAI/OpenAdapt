"""Action executors: carry out one step's action through the backend.

One executor per ``ActionKind``. An executor returns an error string when it
performed no action (or only part of one), and None when the action was
delivered. Every input it delivers goes through two callables from the
caller, in this order:

1. ``authorize()``: every governance check that must pass at the last point
   before input (delivery authorization, the qualification environment). It
   returns a refusal string, or None to proceed, and may raise.
2. ``deliver(result, call)``: the caller's delivery bookkeeping around the
   backend call (attempt reporting, settle seeds, acknowledgements).

Kinds not listed in ``ACTION_EXECUTORS`` are still carried out by
``Replayer._act_without_dialog``; they move here one at a time
(docs/design/DECOMPOSITION.md, seam S4).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol, TypeVar, cast

from openadapt_flow.backend import (
    GuardedKeyboardActionBackend,
    RemoteActuationBackend,
)
from openadapt_flow.ir import ActionKind, Step, StepResult

_T = TypeVar("_T")


class Deliver(Protocol):
    def __call__(self, result: StepResult, call: Callable[[], _T]) -> _T: ...


@dataclass(frozen=True)
class ActionContext:
    """What an executor needs: the step, the backend and the caller's gates."""

    step: Step
    result: StepResult
    backend: Any
    # The settled frame the step's checks ran on; guarded input binds to it.
    before_png: bytes
    # Governance at the last point before input: a refusal, or None.
    authorize: Callable[[], Optional[str]]
    deliver: Deliver
    # Whether keyboard input must stay bound to the verified focus. Called
    # only when an executor needs it, in the order the original code did.
    requires_atomic_keyboard: Callable[[], bool]


ActionExecutor = Callable[[ActionContext], Optional[str]]


def execute_key(ctx: ActionContext) -> Optional[str]:
    """KEY and HOTKEY: press one key or one modifier combination."""
    step, result = ctx.step, ctx.result
    if not step.key:
        return (
            f"Step '{step.id}' ({step.intent}) is a "
            f"{step.action.value} step with no key"
        )
    key = (
        "+".join([*step.modifiers, step.key])
        if step.action is ActionKind.HOTKEY
        else step.key
    )
    requires_atomic_keyboard = ctx.requires_atomic_keyboard()
    guarded_key = (
        requires_atomic_keyboard
        and not isinstance(ctx.backend, RemoteActuationBackend)
        and isinstance(ctx.backend, GuardedKeyboardActionBackend)
    )
    if guarded_key:
        refusal = ctx.authorize()
        if refusal is not None:
            return refusal
        result.delivery_receipt = ctx.deliver(
            result,
            lambda: cast(GuardedKeyboardActionBackend, ctx.backend).press_guarded(
                key,
                expected_frame_sha256=hashlib.sha256(ctx.before_png).hexdigest(),
            ),
        )
        result.actuation = "guarded_keyboard"
    elif requires_atomic_keyboard and not isinstance(
        ctx.backend, RemoteActuationBackend
    ):
        result.safety_halt = True
        result.failure_category = "safety_halt"
        return (
            f"Step '{step.id}' ({step.intent}) lost its guarded keyboard "
            "capability before delivery — refusing unguarded key input; "
            "run aborted"
        )
    else:
        refusal = ctx.authorize()
        if refusal is not None:
            return refusal
        ctx.deliver(result, lambda: ctx.backend.press(key))
    return None


def execute_wait(ctx: ActionContext) -> Optional[str]:
    """WAIT: no input. The settle after every action does the waiting."""
    return None


ACTION_EXECUTORS: Mapping[ActionKind, ActionExecutor] = {
    ActionKind.KEY: execute_key,
    ActionKind.HOTKEY: execute_key,
    ActionKind.WAIT: execute_wait,
}
