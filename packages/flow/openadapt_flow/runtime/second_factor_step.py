"""Run a second-factor step: bind the field, ask a person, type once.

One :class:`Handoff` serves the recorder and the replayer, so both follow the
same order and the same rules:

1. **Claim.** The step's key is claimed in the run's :class:`DeliveryLedger`
   before a person is asked. A key is claimed once per run, so a retry, a
   loop back to the same state or a second call never asks again and never
   types again.
2. **Bind.** The focused element must be an empty one-time-code input that
   renders masked (:mod:`openadapt_flow.backends.one_time_field`). A backend
   that cannot establish this refuses.
3. **Ask.** :func:`openadapt_flow.second_factor.obtain` waits for the person,
   with its deadline checked again when the answer arrives.
4. **Re-check.** Immediately before typing, the same element must still be
   focused, empty and masked. A dialog that took focus, a field that was
   prefilled or replaced, or a lost binding refuses, and the code is dropped.
5. **Type once.** The code is typed through the bound element. It is never
   typed again, whatever happens next.
6. **Read back.** The element must hold exactly the code's length (or have
   left the page, which a code page that submits itself does). Anything else
   stops the run as ``second_factor_unverified``; it is not retried.

Each check has three outcomes, as in the shared verdict kernel
(``openadapt_flow.verdict``, #2476): only ``HELD`` proceeds; ``REFUTED`` and
``NOT_CHECKED`` refuse. When that kernel lands, :class:`Status` becomes an
alias of ``verdict.Status``.

The code itself lives only in a :class:`~openadapt_flow.second_factor.OneTimeCode`
for the few calls between steps 3 and 5. No message, result field, log line,
event or receipt carries it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Protocol

from openadapt_flow.second_factor import OneTimeCode, SecondFactorRefused
from openadapt_flow.second_factor import obtain as obtain_second_factor

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow

__all__ = [
    "DeliveryLedger",
    "FieldBinding",
    "Handoff",
    "Status",
    "act",
    "field_checks",
]


class Status(str, Enum):
    """The three values a check can have (``openadapt_flow.verdict.Status``)."""

    HELD = "held"
    REFUTED = "refuted"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status


class FieldBinding(Protocol):
    """What a backend must offer to take a one-time code."""

    def mask(self) -> None: ...

    def bind(self) -> Optional[Mapping[str, Any]]: ...

    def check(self) -> Optional[Mapping[str, Any]]: ...

    def type_once(self, code: str) -> None: ...


class DeliveryLedger:
    """The step keys of one run that already asked for a code."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()

    def claim(self, key: str) -> None:
        if key in self._claimed:
            raise SecondFactorRefused(
                "second_factor_invalid",
                "this step already asked for its second factor in this run; "
                "it is never asked or typed twice",
            )
        self._claimed.add(key)


def field_checks(state: Optional[Mapping[str, Any]], *, bound: bool) -> list[Check]:
    """The checks a field must hold before a code is typed into it.

    ``bound`` adds that the bound element still has focus (step 4).
    """

    def held(name: str, ok: Callable[[Mapping[str, Any]], bool]) -> Check:
        if not isinstance(state, Mapping):
            return Check(name, Status.NOT_CHECKED)
        try:
            return Check(name, Status.HELD if ok(state) else Status.REFUTED)
        except Exception:  # noqa: BLE001 - an unreadable state is not checked
            return Check(name, Status.NOT_CHECKED)

    checks = [
        held("one-time-code field", lambda s: s.get("kind") == "one-time-code"),
        held("empty", lambda s: s.get("empty") is True and s.get("length") == 0),
        held("masked", lambda s: s.get("masked") is True),
    ]
    if bound:
        checks += [
            held("same element", lambda s: s.get("connected") is True),
            held("still focused", lambda s: s.get("focused") is True),
        ]
    return checks


def _require(checks: list[Check], when: str) -> None:
    failed = [check.name for check in checks if check.status is not Status.HELD]
    if failed:
        raise SecondFactorRefused(
            "second_factor_unbound",
            f"{when}, the code field did not hold: {', '.join(failed)}; "
            "nothing was typed",
        )


class Handoff:
    """One step's handoff. See the module docstring for the order."""

    def __init__(
        self,
        field: Optional[FieldBinding],
        ledger: DeliveryLedger,
        *,
        obtain: Callable[..., Any] = obtain_second_factor,
    ) -> None:
        self._field = field
        self._ledger = ledger
        self._obtain = obtain

    def approve(self, step_id: str, *, key: str) -> None:
        """Wait for a person to approve the sign-in on their device."""

        self._ledger.claim(key)
        self._obtain("approval", step_id)

    def deliver_code(
        self,
        step_id: str,
        *,
        key: str,
        deliver: Callable[[Callable[[], None]], Any] = lambda call: call(),
    ) -> bool:
        """Ask for one code and type it once into the bound field.

        ``deliver`` wraps the single typing call (the replayer's delivery
        boundary). Returns True when the field reads back the code's length,
        False when the page took the code and removed the field. Raises
        :class:`SecondFactorRefused`; a refusal before typing drops the code
        untyped.
        """

        self._ledger.claim(key)
        field = self._field
        if field is None:
            raise SecondFactorRefused(
                "second_factor_unbound",
                "this backend cannot bind a code to its field, check that "
                "the field is empty, or hide the code on screen; nothing was typed",
            )
        try:
            field.mask()
            before = field.bind()
        except Exception:  # noqa: BLE001 - a failed bind is a refusal
            before = None
        _require(field_checks(before, bound=False), "before asking")
        code = self._obtain("code", step_id)
        if not isinstance(code, OneTimeCode):
            raise SecondFactorRefused("second_factor_invalid", "no code came back")
        try:
            try:
                now = field.check()
            except Exception:  # noqa: BLE001 - a failed read is not checked
                now = None
            _require(field_checks(now, bound=True), "after the person answered")
            length = code.length
            try:
                deliver(lambda: field.type_once(code.consume()))
            except Exception as exc:
                raise SecondFactorRefused(
                    "second_factor_unverified",
                    f"typing the code raised {type(exc).__name__}; it is not "
                    "typed again",
                ) from None
        finally:
            code.discard()
        try:
            after = field.check()
        except Exception:  # noqa: BLE001 - a failed read is inconclusive
            after = None
        if isinstance(after, Mapping) and after.get("connected") is False:
            return False  # the page took the code and moved on
        if not (
            isinstance(after, Mapping)
            and after.get("kind") == "one-time-code"
            and after.get("length") == length
        ):
            raise SecondFactorRefused(
                "second_factor_unverified",
                "the code field does not hold the code's length after it was "
                "typed once; it is not typed again",
            )
        return True


# -- the replayer's step ------------------------------------------------------


def _keeps_exact_frames(replayer: Any, workflow: "Workflow") -> bool:
    """Whether the program retains raw frames for a visual transition guard."""

    graphs = [workflow.program, *(workflow.subflows or {}).values()]
    uses_frame = getattr(replayer, "_program_guard_uses_frame", None)
    for graph in graphs:
        for state in (getattr(graph, "states", None) or {}).values():
            for transition in getattr(state, "transitions", None) or ():
                if not callable(uses_frame) or uses_frame(transition.guard):
                    return True
    return False


def act(
    replayer: Any,
    step: "Step",
    workflow: "Workflow",
    params: Mapping[str, Any],
    result: Any,
) -> Optional[str]:
    """``Replayer._act_without_dialog`` for a ``second_factor`` step."""

    from openadapt_flow.backends.one_time_field import one_time_field_for

    ledger = replayer.__dict__.setdefault("_second_factor_ledger", DeliveryLedger())
    try:
        if step.one_time == "approval":
            Handoff(None, ledger).approve(step.id, key=step.id)
            return None
        if _keeps_exact_frames(replayer, workflow):
            raise SecondFactorRefused(
                "second_factor_unavailable",
                "this program keeps exact screen frames for a visual "
                "transition, which a code could appear in; nothing was typed",
            )
        refusal = replayer._delivery_authorization_refusal(
            workflow, params, step, result
        )
        if refusal is not None:
            return str(refusal)
        locale = replayer._require_locale_invariants_before_keyboard(workflow)
        if locale is not None:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return str(locale)
        replayer._require_qualification_environment_current()
        verified = Handoff(one_time_field_for(replayer.backend), ledger).deliver_code(
            step.id,
            key=step.id,
            deliver=lambda call: replayer._deliver_backend_call(result, call),
        )
    except SecondFactorRefused as refused:
        if refused.code == "second_factor_unverified":
            result.safety_halt = True
            result.failure_category = "safety_halt"
        else:
            result.failure_category = "governed_refusal"
        return f"Step '{step.id}' ({step.intent}) {refused}"
    result.actuation = "dom"
    if verified:
        result.input_verified = True
    return None
