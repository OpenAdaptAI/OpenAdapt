"""Checkbox attestations: declared in pack data, ticked only on a person's grant.

Some checkboxes are not data. "I confirm the information above is true", "I
attest that I reviewed this list", "I understand this cannot be undone": a
tick there is a statement someone makes. Packs used to bind such a box to an
intake fact (``confirm_details: true``) or a derived flag, so the run ticked
it on its own. This module separates the two:

- An :class:`AttestationSpec` is pack data: which box, and the SHA-256 of its
  statement as the demonstration showed it (:func:`statement_digest`).
- An :class:`AttestationGrant` is given to the run by the person who
  attests, for one statement digest. It is its own type: an intake value, a
  derived fact, a bool or a mapping is refused (:func:`plan_checkboxes`
  raises ``TypeError``), so nothing can tick an attestation through a fact.
- :func:`check_pack` refuses a pack whose field bindings name a declared
  attestation.

:func:`plan_checkboxes` decides, for every checkbox or switch on the screen:

- ``field``: a field binding covers it; it is not an attestation.
- ``tick``: it matches exactly one declared attestation whose statement still
  hashes to the pinned digest, the run holds a grant for that digest, and the
  box is not ticked yet.
- ``leave``: the pack lists it as knowingly left alone (``ignored``), and it
  shows the state the pack declared.
- ``stop``: anything else. An undeclared checkbox, a changed statement, a
  missing or mismatched grant, an attestation bound to a field, a box ticked
  before the run touched it, two specs matching one box, or one spec
  matching several boxes (one grant covers one box). The run stops
  before acting on any box.

:func:`judge_attestations` is a verdict site. Its obligations are the plan's
boxes (other than ``field`` ones), fixed before any tick, and it settles them
against a fresh read: each must show the planned state with the same
statement, and a box the plan never saw is unexpected.

Words are never matched against a list: an attestation is whatever the pack
declares, and the engine knows no "I agree" vocabulary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

from openadapt_flow.gui.model import (
    Actuator,
    Control,
    ControlSpec,
    DemoStep,
    Screen,
    norm,
)
from openadapt_flow.verdict import (
    Obligations,
    Observed,
    Result,
    Settlement,
    Status,
    Subject,
    settle,
)
from openadapt_flow.verdict_sites import verdict_site

__all__ = [
    "AttestationCandidate",
    "AttestationGrant",
    "AttestationPlan",
    "AttestationSpec",
    "BoxDecision",
    "BoxPlan",
    "IgnoredBox",
    "PackError",
    "apply_attestations",
    "check_pack",
    "judge_attestations",
    "learn_attestations",
    "judge_screen",
    "plan_checkboxes",
    "plan_rows",
    "statement_digest",
    "statement_of",
]

BOX_ROLES = frozenset({"checkbox", "switch"})
#: Longest statement read from a container; a longer one is not read whole.
STATEMENT_CAP = 2000


def statement_digest(text: str) -> str:
    """SHA-256 of a statement after :func:`~openadapt_flow.gui.model.norm`."""

    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AttestationSpec:
    """Pack data: one attestation box and the digest of its statement.

    ``statement_from`` says where the statement is: the box's own label
    (``name``), or the text of the innermost container around it
    (``container``), for a box labelled "I agree" under a paragraph."""

    attestation_id: str
    control: ControlSpec
    statement_sha256: str
    statement_from: Literal["name", "container"] = "name"

    def __post_init__(self) -> None:
        if self.control.role not in BOX_ROLES:
            raise ValueError("an attestation is a checkbox or a switch")
        if len(self.statement_sha256) != 64:
            raise ValueError("statement_sha256 is a SHA-256 hex digest")
        if self.statement_from not in ("name", "container"):
            raise ValueError(f"unknown statement source {self.statement_from!r}")


def statement_of(ctrl: Control, source: str = "name") -> Optional[str]:
    """The statement a box carries: its label, or its innermost container's
    text. None when there is none, or it may have been cut short."""

    if source == "name":
        return ctrl.name or None
    if source != "container" or not ctrl.containers:
        return None
    text = ctrl.containers[-1].text
    if not text or len(text) >= STATEMENT_CAP:
        return None
    return text


@dataclass(frozen=True)
class AttestationGrant:
    """A person's attestation for one run: this statement, by this person.

    Built from the run request by whoever collects the attestation, never
    from intake values."""

    attestation_id: str
    statement_sha256: str
    granted_by: str

    def __post_init__(self) -> None:
        if not self.granted_by.strip():
            raise ValueError("a grant names the person who attests")


@dataclass(frozen=True)
class IgnoredBox:
    """A checkbox the pack knowingly leaves alone, and the state it must show."""

    control: ControlSpec
    checked: bool = False


class PackError(ValueError):
    """The pack binds a declared attestation to a field."""


def check_pack(
    attestations: Sequence[AttestationSpec],
    field_controls: Iterable[ControlSpec],
) -> None:
    """Refuse a pack whose field bindings name a declared attestation's box
    (same role and name). Raises :class:`PackError`."""

    declared = {(a.control.role, norm(a.control.name)): a for a in attestations}
    for spec in field_controls:
        hit = declared.get((spec.role, norm(spec.name)))
        if hit is not None:
            raise PackError(
                f"attestation {hit.attestation_id!r} is bound to a field; "
                "an attestation is ticked only on a grant"
            )
    ids = [a.attestation_id for a in attestations]
    if len(ids) != len(set(ids)):
        raise PackError("two attestations share an id")


class BoxDecision(str, Enum):
    FIELD = "field"
    TICK = "tick"
    LEAVE = "leave"
    STOP = "stop"


@dataclass(frozen=True)
class BoxPlan:
    handle: str
    name: str
    decision: BoxDecision
    digest: str
    #: The state the box must show after the plan is applied (None: a field's).
    want_checked: Optional[bool]
    attestation_id: Optional[str] = None
    granted_by: str = ""
    reason: str = ""
    statement_from: str = "name"


@dataclass(frozen=True)
class AttestationPlan:
    boxes: tuple[BoxPlan, ...]
    run_id: str = ""

    @property
    def stops(self) -> tuple[BoxPlan, ...]:
        return tuple(b for b in self.boxes if b.decision is BoxDecision.STOP)

    @property
    def ticks(self) -> tuple[BoxPlan, ...]:
        return tuple(b for b in self.boxes if b.decision is BoxDecision.TICK)


def _stop(ctrl: Control, reason: str, att: Optional[str] = None) -> BoxPlan:
    return BoxPlan(
        ctrl.handle,
        ctrl.name,
        BoxDecision.STOP,
        statement_digest(ctrl.name),
        None,
        att,
        reason=reason,
    )


def _grants_by_id(grants: Iterable[object]) -> dict[str, AttestationGrant]:
    out: dict[str, AttestationGrant] = {}
    for grant in grants:
        if not isinstance(grant, AttestationGrant):
            raise TypeError(
                "attestations are ticked only on an AttestationGrant, "
                f"not on {type(grant).__name__}"
            )
        if grant.attestation_id in out:
            raise ValueError(f"two grants for {grant.attestation_id!r}")
        out[grant.attestation_id] = grant
    return out


def plan_checkboxes(
    screen: Screen,
    *,
    attestations: Sequence[AttestationSpec],
    grants: Iterable[object] = (),
    bound: Iterable[str] = (),
    ignored: Sequence[IgnoredBox] = (),
    run_id: str = "",
) -> AttestationPlan:
    """Decide every checkbox and switch on ``screen`` (see the module doc).

    ``bound`` holds the handles the pack's field bindings resolved to on this
    screen. ``grants`` must all be :class:`AttestationGrant`."""

    by_id = _grants_by_id(grants)
    bound_set = set(bound)
    boxes: list[BoxPlan] = []
    # One grant covers one box: a spec that names several boxes on the
    # screen (two identical "I agree" boxes) stops on every one of them.
    matches = {
        a.attestation_id: sum(
            1 for c in screen.controls if c.role in BOX_ROLES and a.control.matches(c)
        )
        for a in attestations
    }
    for ctrl in screen.controls:
        if ctrl.role not in BOX_ROLES:
            continue
        specs = [a for a in attestations if a.control.matches(ctrl)]
        if ctrl.handle in bound_set:
            if specs:
                boxes.append(
                    _stop(
                        ctrl,
                        "a declared attestation is bound to a field",
                        specs[0].attestation_id,
                    )
                )
            else:
                boxes.append(
                    BoxPlan(
                        ctrl.handle,
                        ctrl.name,
                        BoxDecision.FIELD,
                        statement_digest(ctrl.name),
                        None,
                    )
                )
            continue
        if len(specs) > 1:
            boxes.append(_stop(ctrl, "two declared attestations match this box"))
            continue
        if specs and matches[specs[0].attestation_id] > 1:
            boxes.append(
                _stop(
                    ctrl,
                    "the attestation's spec matches more than one box",
                    specs[0].attestation_id,
                )
            )
            continue
        if not specs:
            leave = [i for i in ignored if i.control.matches(ctrl)]
            if len(leave) == 1 and ctrl.checked is leave[0].checked:
                boxes.append(
                    BoxPlan(
                        ctrl.handle,
                        ctrl.name,
                        BoxDecision.LEAVE,
                        statement_digest(ctrl.name),
                        leave[0].checked,
                    )
                )
            elif len(leave) == 1:
                boxes.append(
                    _stop(
                        ctrl, "a box the pack leaves alone is not in its declared state"
                    )
                )
            else:
                boxes.append(_stop(ctrl, "a checkbox the pack does not declare"))
            continue
        spec = specs[0]
        statement = statement_of(ctrl, spec.statement_from)
        digest = statement_digest(statement or "")
        grant = by_id.get(spec.attestation_id)
        if statement is None:
            boxes.append(
                _stop(
                    ctrl, "the statement could not be read whole", spec.attestation_id
                )
            )
        elif digest != spec.statement_sha256:
            boxes.append(
                _stop(
                    ctrl,
                    "the statement differs from the declared one",
                    spec.attestation_id,
                )
            )
        elif grant is None:
            boxes.append(
                _stop(
                    ctrl,
                    "no one attested this statement for this run",
                    spec.attestation_id,
                )
            )
        elif grant.statement_sha256 != digest:
            boxes.append(
                _stop(
                    ctrl, "the grant is for a different statement", spec.attestation_id
                )
            )
        elif ctrl.checked is not False:
            boxes.append(
                _stop(
                    ctrl,
                    "the box was ticked before the run touched it",
                    spec.attestation_id,
                )
            )
        elif ctrl.disabled or ctrl.readonly:
            boxes.append(_stop(ctrl, "the box cannot be ticked", spec.attestation_id))
        else:
            boxes.append(
                BoxPlan(
                    ctrl.handle,
                    ctrl.name,
                    BoxDecision.TICK,
                    digest,
                    True,
                    spec.attestation_id,
                    grant.granted_by,
                    statement_from=spec.statement_from,
                )
            )
    return AttestationPlan(tuple(boxes), run_id)


def apply_attestations(actuator: Actuator, plan: AttestationPlan) -> Screen:
    """Tick the plan's ``tick`` boxes and return a fresh read. Refuses a plan
    with any ``stop``: nothing is ticked."""

    if plan.stops:
        raise ValueError(f"the plan stops: {plan.stops[0].reason}")
    for box in plan.ticks:
        actuator.set_checked(box.handle, True)
    return actuator.observe()


# -- the verdict ------------------------------------------------------------------


def _obs_of(screen: Screen, plan: AttestationPlan) -> list[dict[str, Any]]:
    """A fresh read of every box, bound to the run that read it. Each box's
    statement is read from where the plan read it."""

    source = {b.handle: b.statement_from for b in plan.boxes}
    return [
        {
            "handle": c.handle,
            "statement": statement_of(c, source.get(c.handle, "name")),
            "checked": c.checked,
            "run_id": plan.run_id,
        }
        for c in screen.controls
        if c.role in BOX_ROLES
    ]


@dataclass(frozen=True)
class AttestationVerdict:
    settlement: Settlement
    status: Status


@verdict_site("gui.attestations.judge", classes="C1 C2 C5")
def judge_attestations(
    plan: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    complete: bool = True,
) -> AttestationVerdict:
    """Settle the plan's boxes against a fresh read of the screen.

    ``plan`` rows carry ``handle``, ``decision``, ``digest`` and
    ``want_checked``; ``after`` rows carry ``handle``, ``statement``,
    ``checked`` and ``run_id``. ``field`` rows are a binding's business and are left out.
    A ``stop`` row is an obligation that can never hold. A box must show the
    planned state with a statement that still hashes to the planned digest;
    a read whose ``checked`` is not a bool is not checked, and a box the plan
    never saw is unexpected. ``complete=False`` (a capped read) can refute,
    never hold."""

    planned = [p for p in plan if p.get("decision") != BoxDecision.FIELD.value]
    fields = {
        p.get("handle") for p in plan if p.get("decision") == BoxDecision.FIELD.value
    }
    obligations = Obligations.fix(
        (
            Subject(run_id=run_id, surface="gui", key=str(p.get("handle")), field="box")
            for p in planned
        ),
        source="attestation plan",
    )
    by_handle = {str(p.get("handle")): p for p in planned}
    results = []
    for row in after:
        handle = str(row.get("handle"))
        if handle in fields:
            continue
        subject = Subject(
            run_id=str(row.get("run_id") or ""), surface="gui", key=handle, field="box"
        )
        obs = Observed(dict(row), complete=complete is True)
        want = by_handle.get(handle)
        if want is None:
            results.append(Result.refuted(subject, obs, "a box the plan never saw"))
            continue
        checked = row.get("checked")
        decision = want.get("decision")
        if decision not in (BoxDecision.TICK.value, BoxDecision.LEAVE.value):
            results.append(
                Result.not_checked(subject, f"the plan stops here: {decision!r}")
            )
        elif not isinstance(checked, bool):
            results.append(Result.not_checked(subject, "the box's state was not read"))
        elif not isinstance(row.get("statement"), str):
            results.append(Result.not_checked(subject, "the statement was not read"))
        elif statement_digest(row["statement"]) != want.get("digest"):
            results.append(Result.refuted(subject, obs, "the statement changed"))
        elif checked is not want.get("want_checked"):
            results.append(
                Result.refuted(subject, obs, "the box does not show the planned state")
            )
        elif complete is not True:
            results.append(Result.not_checked(subject, "the screen was read in part"))
        else:
            results.append(Result.held(subject, obs, "as planned"))
    settlement = settle(obligations, results)
    status = settlement.label(precedence=(Status.REFUTED, Status.NOT_CHECKED))
    # A box seen in the wrong state stays refuted even when another read of
    # it disagrees (the two are then "conflicting" in the settlement).
    seen_wrong = [r for r in results if r.status is Status.REFUTED]
    if status is not Status.HELD and seen_wrong:
        status = Status.REFUTED
    return AttestationVerdict(settlement, status)


def plan_rows(plan: AttestationPlan) -> list[dict[str, Any]]:
    """The plan as the rows :func:`judge_attestations` reads."""

    return [
        {
            "handle": b.handle,
            "decision": b.decision.value,
            "digest": b.digest,
            "want_checked": b.want_checked,
        }
        for b in plan.boxes
    ]


def judge_screen(plan: AttestationPlan, after: Screen) -> AttestationVerdict:
    """:func:`judge_attestations` on a fresh :class:`Screen`."""

    return judge_attestations(
        plan_rows(plan),
        _obs_of(after, plan),
        run_id=plan.run_id,
        complete=after.complete,
    )


# -- from a demonstration ----------------------------------------------------------


@dataclass(frozen=True)
class AttestationCandidate:
    """A box the demonstration ticked that no field binding covers. A
    suggestion: the pack author decides whether it is an attestation."""

    control: ControlSpec
    statement_sha256: str
    name: str


def learn_attestations(
    demo: Sequence[DemoStep], bound: Iterable[str] = ()
) -> tuple[tuple[AttestationCandidate, ...], tuple[IgnoredBox, ...]]:
    """Suggest attestations (boxes the demo ticked, not bound) and boxes to
    leave alone (boxes the demo saw but never touched, with the state they
    showed). Nothing here goes into a pack without the author."""

    bound_set = set(bound)
    ticked: dict[str, Control] = {}
    seen: dict[str, Control] = {}
    for step in demo:
        for ctrl in step.before.controls:
            if ctrl.role in BOX_ROLES:
                seen.setdefault(ctrl.handle, ctrl)
        if step.action == "check" and step.target.role in BOX_ROLES:
            ticked[step.target.handle] = step.target
    candidates = tuple(
        AttestationCandidate(
            ControlSpec(c.role, c.name, tuple(x.name for x in c.containers if x.name)),
            statement_digest(c.name),
            c.name,
        )
        for h, c in ticked.items()
        if h not in bound_set
    )
    leave = tuple(
        IgnoredBox(ControlSpec(c.role, c.name), bool(c.checked))
        for h, c in seen.items()
        if h not in ticked and h not in bound_set and c.name
    )
    return candidates, leave
