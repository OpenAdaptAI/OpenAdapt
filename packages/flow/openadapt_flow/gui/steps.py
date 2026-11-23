"""Steps that move a form on, whatever control draws them, and the commit point.

A form moves on when a person activates something: a "Next" button, a
"Continue" link, a step tab, a menu item. Packs used to list only buttons as
next controls, so a form whose next control was a link stopped with "next
button missing". Here any activatable control (:data:`~openadapt_flow.gui.model.ACTIVATABLE`)
can be a step, and what a step *does* comes from the demonstration, never
from its words:

- :func:`learn_steps` reads each click of a demonstration and records what the
  backend saw it do (:class:`StepRole`): ``commit`` when it counted writes,
  ``navigate`` when the screen changed and nothing was written, ``in_place``
  when neither, ``unknown`` when the backend could not count writes.
- The commit points are the steps the demonstration showed writing
  (:func:`commit_points`), with the number of writes each made. A link that
  commits is a commit point because it wrote, not because it says "Submit".
- :func:`take_step` resolves a learned step on the live screen, clicks it
  and asks :func:`judge_step` whether it did what the demonstration showed.

:func:`judge_step` is a verdict site. A commit step holds only when the
backend counted exactly the writes the demonstration made, and each returned
what the demonstrated one did: the same status class and, for a redirect,
the same ``Location`` key (path and query, with minted ids replaced; see
:func:`~openadapt_flow.gui.dom.location_key`). A save bounced to a sign-in
page is a 302 like a save that went to its confirmation page; only the
``Location`` tells them apart. A commit spec with no recorded outcomes, or
with an outcome in the old path-only ``"3xx <path>"`` form (an older pack),
is never clicked. A
backend that cannot count writes never shows a commit, so a commit through a
plain link that sends nothing the backend counts stays unproven, as it
should: the run has no evidence the record was saved. A navigation step that wrote, or that
left the screen as it was, is refuted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from openadapt_flow.gui.model import (
    Actuator,
    AmbiguousControl,
    Control,
    ControlSpec,
    DemoStep,
    MissingControl,
    Screen,
    match_one,
    screen_signature,
    spec_for,
    write_outcomes_of,
)
from openadapt_flow.verdict import Observed, Result, Status, Subject
from openadapt_flow.verdict_sites import verdict_site

__all__ = [
    "LearnedStep",
    "StepEvidence",
    "StepOutcome",
    "StepRole",
    "StepSpec",
    "commit_points",
    "judge_step",
    "learn_steps",
    "take_step",
]


class StepRole(str, Enum):
    COMMIT = "commit"
    NAVIGATE = "navigate"
    IN_PLACE = "in_place"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StepSpec:
    """Pack data for one step: which control, and what it did in the demo.

    ``writes`` is the number of writes the demonstration counted for it: at
    least 1 for a commit, 0 otherwise. ``outcomes`` is what each of a
    commit's writes returned in the demo, sorted (``"2xx"``,
    ``"redirect /done"``); None on a spec learned before outcomes were
    recorded. Such a commit is not taken, and nor is one with an outcome in
    the old ``"3xx <path>"`` form."""

    step_id: str
    control: ControlSpec
    role: StepRole
    writes: int = 0
    outcomes: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, StepRole):
            raise ValueError(f"unknown step role {self.role!r}")
        if self.role is StepRole.COMMIT and self.writes < 1:
            raise ValueError("a commit step wrote at least once in the demo")
        if self.role is not StepRole.COMMIT and self.writes != 0:
            raise ValueError("only a commit step writes")
        if self.outcomes is not None and (
            self.role is not StepRole.COMMIT or len(self.outcomes) != self.writes
        ):
            raise ValueError("a commit records one outcome per demonstrated write")


@dataclass(frozen=True)
class LearnedStep:
    """What :func:`learn_steps` found for one demonstrated click. ``spec`` is
    None when no spec names the control alone (``reason`` says why); the
    pack author has to name it."""

    index: int
    control: Control
    role: StepRole
    writes: Optional[int]
    spec: Optional[StepSpec]
    reason: str = ""


def _role_of(step: DemoStep) -> StepRole:
    if step.writes is None or step.writes < 0:
        return StepRole.UNKNOWN
    if step.writes > 0:
        return StepRole.COMMIT
    if screen_signature(step.after) != screen_signature(step.before):
        return StepRole.NAVIGATE
    return StepRole.IN_PLACE


def learn_steps(demo: Sequence[DemoStep]) -> tuple[LearnedStep, ...]:
    """One :class:`LearnedStep` per click on an activatable control.

    A step's role comes from what the backend counted and saw, so the same
    word ("Continue") can be a navigation in one app and a commit in
    another, and a link and a button are treated alike."""

    out = []
    for index, step in enumerate(demo):
        if step.action != "click" or not step.target.activatable:
            continue
        role = _role_of(step)
        spec = spec_for(step.target, step.before)
        if role is StepRole.UNKNOWN:
            out.append(
                LearnedStep(
                    index,
                    step.target,
                    role,
                    step.writes,
                    None,
                    "the backend could not count writes for this click",
                )
            )
            continue
        if spec is None:
            out.append(
                LearnedStep(
                    index,
                    step.target,
                    role,
                    step.writes,
                    None,
                    "another control on the screen has the same role, name and containers",
                )
            )
            continue
        writes = int(step.writes or 0) if role is StepRole.COMMIT else 0
        outcomes = None
        if role is StepRole.COMMIT:
            outcomes = _outcomes(step.write_outcomes)
            if outcomes is None or len(outcomes) != writes or _legacy(outcomes):
                out.append(
                    LearnedStep(
                        index,
                        step.target,
                        role,
                        step.writes,
                        None,
                        "the backend did not report what this commit's writes returned",
                    )
                )
                continue
        out.append(
            LearnedStep(
                index,
                step.target,
                role,
                step.writes,
                StepSpec(f"step-{index}", spec, role, writes, outcomes),
            )
        )
    return tuple(out)


def commit_points(steps: Sequence[LearnedStep]) -> tuple[LearnedStep, ...]:
    """The steps the demonstration showed writing, in order."""

    return tuple(s for s in steps if s.role is StepRole.COMMIT)


# -- the verdict ------------------------------------------------------------------


@dataclass(frozen=True)
class StepEvidence:
    """What one live step did. ``moved`` is whether the screen signature
    changed (None when the after read failed)."""

    step_id: str
    role: object
    expected_writes: object
    writes_before: object
    writes_after: object
    moved: object
    after_complete: object
    after_errors: tuple[str, ...] = ()
    run_id: str = ""
    #: What the demo's writes returned, and what this step's writes did.
    expected_outcomes: object = None
    outcomes: object = None


def _count(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


#: The prefix of a redirect outcome learned before the query and minted ids
#: were part of the key. Such an outcome is never compared: its path-only
#: key could match a replay that went somewhere else.
LEGACY_REDIRECT = "3xx "


def _legacy(outcomes: object) -> bool:
    """Whether any outcome is in the old path-only redirect form."""
    return isinstance(outcomes, (tuple, list)) and any(
        isinstance(o, str) and o.startswith(LEGACY_REDIRECT) for o in outcomes
    )


def _outcomes(value: object) -> Optional[tuple[str, ...]]:
    """``value`` as a sorted tuple of non-empty strings, else None."""
    if not isinstance(value, (tuple, list)):
        return None
    if not all(isinstance(v, str) and v for v in value):
        return None
    return tuple(sorted(value))


@verdict_site("gui.steps.judge_step", classes="C3 C5")
def judge_step(ev: StepEvidence) -> Result:
    """Did the step do what the demonstration showed?

    - ``commit``: exactly ``expected_writes`` new writes, on a complete read
      with no error shown, each returning what the demo's did (status class,
      and the ``Location`` key of a redirect). No write, a different
      number, or a different outcome is refuted; outcomes missing on either
      side, or in the old path-only ``"3xx <path>"`` form, are not checked.
    - ``navigate``: the screen changed and nothing was written.
    - ``in_place``: nothing was written and the screen did not move.
    - ``unknown`` or any role nobody listed: not checked.

    A write count that is missing, negative or not a whole number is not
    checked; so is a partial read with nothing to refute."""

    subject = Subject(run_id=ev.run_id, surface="gui", key=ev.step_id, field="step")
    obs = Observed(ev, complete=ev.after_complete is True)
    try:
        role = StepRole(ev.role) if not isinstance(ev.role, StepRole) else ev.role
    except ValueError:
        return Result.not_checked(subject, f"unlisted step role {ev.role!r}")
    if role is StepRole.UNKNOWN:
        return Result.not_checked(
            subject, "the demo could not count this step's writes"
        )
    before, after = _count(ev.writes_before), _count(ev.writes_after)
    if before is None or after is None or after < before:
        return Result.not_checked(subject, "the backend could not count writes")
    delta = after - before
    if ev.after_errors:
        return Result.refuted(
            subject, obs, f"the screen shows an error: {ev.after_errors[0]}"
        )
    if role is StepRole.COMMIT:
        want = _count(ev.expected_writes)
        if not want:
            return Result.not_checked(subject, "no expected write count for a commit")
        if delta == 0:
            return Result.refuted(subject, obs, "the commit step wrote nothing")
        if delta != want:
            return Result.refuted(
                subject,
                obs,
                f"the commit step wrote {delta} times; the demo wrote {want}",
            )
        want_out, got_out = _outcomes(ev.expected_outcomes), _outcomes(ev.outcomes)
        if want_out is None or len(want_out) != want:
            return Result.not_checked(
                subject, "the demo did not record what its writes returned"
            )
        if _legacy(want_out) or _legacy(got_out):
            return Result.not_checked(
                subject,
                "an outcome is in the old path-only redirect form; relearn the step",
            )
        if got_out is None or len(got_out) != delta:
            return Result.not_checked(
                subject, "the backend did not report what the writes returned"
            )
        if got_out != want_out:
            return Result.refuted(
                subject,
                obs,
                f"the commit's writes returned {', '.join(got_out)}; "
                f"the demo's returned {', '.join(want_out)}",
            )
    elif delta:
        return Result.refuted(subject, obs, f"a {role.value} step wrote {delta} times")
    elif role is StepRole.NAVIGATE:
        if ev.moved is False:
            return Result.refuted(subject, obs, "the screen did not change")
        if ev.moved is not True:
            return Result.not_checked(subject, "the screen after the step was not read")
    elif role is StepRole.IN_PLACE:
        if ev.moved is True:
            return Result.refuted(subject, obs, "an in-place step moved the screen")
        if ev.moved is not False:
            return Result.not_checked(subject, "the screen after the step was not read")
    if ev.after_complete is not True:
        return Result.not_checked(subject, "the screen after the step was read in part")
    return Result.held(subject, obs, f"{role.value} as demonstrated")


# -- running a step ---------------------------------------------------------------


@dataclass(frozen=True)
class StepOutcome:
    result: Result
    clicked: bool
    control: Optional[Control] = None
    after: Optional[Screen] = None

    @property
    def held(self) -> bool:
        return self.result.status is Status.HELD


def take_step(
    actuator: Actuator,
    spec: StepSpec,
    *,
    run_id: str = "",
    screen: Optional[Screen] = None,
) -> StepOutcome:
    """Resolve ``spec`` on the live screen, click it, and judge it.

    Nothing is clicked when the control is missing or ambiguous, when the
    backend cannot count writes (no step could be judged there, so none is
    attempted), or for a commit when the spec or the backend has no record of
    what the writes returned, or the spec has it only in the old path-only
    redirect form."""

    subject = Subject(run_id=run_id, surface="gui", key=spec.step_id, field="step")
    before_screen = screen if screen is not None else actuator.observe()
    try:
        ctrl = match_one(spec.control, before_screen)
    except (MissingControl, AmbiguousControl) as exc:
        return StepOutcome(Result.not_checked(subject, str(exc)), clicked=False)
    if not ctrl.activatable:
        return StepOutcome(
            Result.not_checked(subject, "the control is disabled"), False, ctrl
        )
    w0 = actuator.write_count()
    if w0 is None:
        # Without a write count no step can be judged (a navigation must be
        # shown not to write), so none is taken.
        return StepOutcome(
            Result.not_checked(
                subject, "this backend cannot count writes; no step is taken"
            ),
            False,
            ctrl,
        )
    o0 = write_outcomes_of(actuator)
    if spec.role is StepRole.COMMIT and (
        spec.outcomes is None or o0 is None or _legacy(spec.outcomes)
    ):
        # Without the demo's outcomes in the current form, or a backend that
        # reports them, a commit bounced to a sign-in page looks like one
        # that saved.
        return StepOutcome(
            Result.not_checked(
                subject,
                "the spec or the backend has no record of what the commit's "
                "writes returned; the commit is not taken",
            ),
            False,
            ctrl,
        )
    err_fn = getattr(actuator, "write_errors", None)
    err0 = len(err_fn()) if callable(err_fn) else 0
    actuator.click(ctrl.handle)
    after = actuator.observe()
    w1 = actuator.write_count()
    write_errors = tuple(err_fn()[err0:]) if callable(err_fn) else ()
    o1 = write_outcomes_of(actuator)
    outcomes = None if o0 is None or o1 is None else o1[len(o0) :]
    ev = StepEvidence(
        step_id=spec.step_id,
        role=spec.role,
        expected_writes=spec.writes if spec.role is StepRole.COMMIT else 0,
        writes_before=w0,
        writes_after=w1,
        moved=screen_signature(after) != screen_signature(before_screen),
        after_complete=after.complete,
        after_errors=tuple(after.errors)
        + tuple(c.error for c in after.controls if c.error)
        + write_errors,
        run_id=run_id,
        expected_outcomes=spec.outcomes if spec.role is StepRole.COMMIT else None,
        outcomes=outcomes,
    )
    return StepOutcome(judge_step(ev), True, ctrl, after)
