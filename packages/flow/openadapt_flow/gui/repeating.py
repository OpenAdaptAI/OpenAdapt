"""Repeating structures: "add another traveller", "add a row", "add a line item".

A form that asks for a list shows one instance of a block of fields (a
traveller, a medication row, an invoice line) and a control that adds
another. A run whose intake has three items must add instances until there
are three, fill each one with its own item, and check each one. Packs used to
work around this with a stop or a popup that cannot exist. This module
handles it once:

- :func:`learn_repeats` finds the shape in a demonstration. It looks for a
  click after which a new block of editable controls appeared inside a new
  container of one kind (a fieldset, a row, a list item). When the block
  repeats the fields of an instance already on the screen, that is the group
  (a list that starts empty is learned from its second add).
  It yields a :class:`RepeatSpec`: the add control, the instance's container
  kind and its field slots. :func:`map_slots` proposes which item key feeds
  each slot, from the values the demonstration typed.
- :func:`find_instances` reads the instances on a screen: each container of
  the declared kind that holds exactly one control per slot, in document
  order. A slot control outside any instance, or an instance with a missing
  or doubled slot, makes the read unusable.
- :func:`fill_instances` fixes the obligations from the intake first (every
  item, every slot). It then adds instances until the count matches, fills
  them in document order and reads them back. It stops before writing when:
  - an instance already holds data the run did not enter;
  - the screen already has more instances than the intake (the run never
    deletes one);
  - the add control is missing or ambiguous, or one click did not add exactly
    one blank instance;
  - adding wrote anything;
  - an item lacks a slot's key.
- :func:`judge_instances` is a verdict site. It settles item-by-slot
  obligations against a fresh read, keyed by the instance each item was
  written into (its container handle, bound when it was filled, never a
  position guess). A wrong value is refuted, an instance nobody planned is
  unexpected and refuted, and a missing instance is missing.

:func:`location_rows` exports the per-instance checks as plain rows shaped
like the effect-location record (``openadapt.effect-location/v1``), so the
shared verifier layer can take them over without this module importing it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.gui.model import (
    Actuator,
    AmbiguousControl,
    Container,
    Control,
    ControlSpec,
    DemoStep,
    MissingControl,
    Screen,
    match_one,
    norm,
    spec_for,
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
    "Instance",
    "InstanceRead",
    "RepeatSpec",
    "RepeatVerdict",
    "Slot",
    "FillOutcome",
    "fill_instances",
    "find_instances",
    "judge_instances",
    "learn_repeats",
    "location_rows",
    "map_slots",
    "planned_rows",
    "seen_rows",
]

#: Container kinds an instance can be.
INSTANCE_KINDS = frozenset({"group", "region", "row", "item"})


@dataclass(frozen=True)
class Slot:
    """One field of an instance, and the item key that feeds it ("" until
    the pack author or :func:`map_slots` sets it)."""

    role: str
    name: str
    item_key: str = ""


@dataclass(frozen=True)
class RepeatSpec:
    """Pack data for one repeating group.

    ``within`` names containers the whole group sits in, to tell two groups
    with the same fields apart. ``add_writes`` is how many writes one add made
    in the demonstration (usually 0)."""

    group_id: str
    add: ControlSpec
    instance_kind: str
    slots: tuple[Slot, ...]
    within: tuple[str, ...] = ()
    add_writes: int = 0
    max_instances: int = 50

    def __post_init__(self) -> None:
        if self.instance_kind not in INSTANCE_KINDS:
            raise ValueError(f"unknown instance kind {self.instance_kind!r}")
        if not self.slots:
            raise ValueError("a repeating group has at least one field")
        keys = [(s.role, norm(s.name)) for s in self.slots]
        if len(keys) != len(set(keys)):
            raise ValueError("two slots have the same role and name")


# -- reading instances ----------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    handle: str
    name: str
    #: The control for each slot, in slot order.
    controls: tuple[Control, ...]

    def values(self) -> tuple[str, ...]:
        return tuple(_shown(c) for c in self.controls)

    def blank(self) -> bool:
        return all(not v or v == "false" for v in self.values())


@dataclass(frozen=True)
class InstanceRead:
    instances: tuple[Instance, ...]
    problem: str = ""

    @property
    def usable(self) -> bool:
        return not self.problem


def _shown(ctrl: Control) -> str:
    if ctrl.role in ("checkbox", "switch", "radio"):
        return "true" if ctrl.checked else "false"
    return ctrl.value.strip()


def _slot_of(spec: RepeatSpec, ctrl: Control) -> Optional[int]:
    for n, slot in enumerate(spec.slots):
        if ctrl.role == slot.role and norm(ctrl.name) == norm(slot.name):
            return n
    return None


def _in_scope(spec: RepeatSpec, ctrl: Control) -> bool:
    names = ctrl.container_names()
    at = 0
    for name in names:
        if at < len(spec.within) and name == norm(spec.within[at]):
            at += 1
    return at == len(spec.within)


def _instance_box(spec: RepeatSpec, ctrl: Control) -> Optional[Container]:
    boxes = [c for c in ctrl.containers if c.kind == spec.instance_kind]
    return boxes[-1] if boxes else None


def find_instances(screen: Screen, spec: RepeatSpec) -> InstanceRead:
    """The group's instances on ``screen``, in document order."""

    order: list[str] = []
    by_box: dict[str, dict[int, list[Control]]] = {}
    names: dict[str, str] = {}
    for ctrl in screen.controls:
        slot = _slot_of(spec, ctrl)
        if slot is None or not _in_scope(spec, ctrl) or ctrl.dialog:
            continue
        box = _instance_box(spec, ctrl)
        if box is None:
            return InstanceRead((), f"{ctrl.name!r} sits in no {spec.instance_kind}")
        if box.handle not in by_box:
            order.append(box.handle)
            by_box[box.handle] = {}
            names[box.handle] = box.name
        by_box[box.handle].setdefault(slot, []).append(ctrl)
    out = []
    for handle in order:
        slots = by_box[handle]
        if sorted(slots) != list(range(len(spec.slots))) or any(
            len(v) != 1 for v in slots.values()
        ):
            return InstanceRead(
                (),
                f"instance {names[handle] or handle!r} does not hold each field once",
            )
        out.append(
            Instance(
                handle,
                names[handle],
                tuple(slots[n][0] for n in range(len(spec.slots))),
            )
        )
    if not screen.complete:
        return InstanceRead(tuple(out), "the screen was read in part")
    return InstanceRead(tuple(out))


# -- the verdict ------------------------------------------------------------------


@dataclass(frozen=True)
class RepeatVerdict:
    settlement: Settlement
    status: Status


@verdict_site("gui.repeating.judge", classes="C1 C2 C5")
def judge_instances(
    planned: Sequence[Mapping[str, Any]],
    bound: Mapping[str, str],
    seen: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    complete: bool = True,
) -> RepeatVerdict:
    """Settle item-by-slot obligations against a fresh read.

    ``planned`` rows carry ``item`` (the item's index as text), ``slot`` and
    ``expected``, one per item and slot, fixed from the intake before any
    instance was added. ``bound`` maps each item to the container handle it
    was written into. ``seen`` rows carry ``handle``, ``slot``, ``value`` and
    ``run_id``, one per slot of every instance on the screen. A value must
    equal the expected text exactly. An instance no item was bound to is
    refuted and unexpected; a value that is not text is not checked."""

    obligations = Obligations.fix(
        (
            Subject(
                run_id=run_id,
                surface="gui",
                key=str(p.get("item")),
                field=str(p.get("slot")),
            )
            for p in planned
        ),
        source="intake items",
    )
    expected = {
        (str(p.get("item")), str(p.get("slot"))): p.get("expected") for p in planned
    }
    item_of = {str(h): str(i) for i, h in bound.items()}
    if len(item_of) != len(bound):
        item_of = {}  # two items bound to one instance: nothing can settle
    results = []
    for row in seen:
        handle, slot = str(row.get("handle")), str(row.get("slot"))
        item = item_of.get(handle)
        subject = Subject(
            run_id=str(row.get("run_id") or ""),
            surface="gui",
            key=item if item is not None else f"unplanned:{handle}",
            field=slot,
        )
        obs = Observed(dict(row), complete=complete is True)
        value = row.get("value")
        want = expected.get((item, slot)) if item is not None else None
        if item is None:
            results.append(
                Result.refuted(subject, obs, "an instance no item was written into")
            )
        elif not isinstance(value, str) or not isinstance(want, str):
            results.append(
                Result.not_checked(subject, "the value was not read as text")
            )
        elif value != want:
            results.append(
                Result.refuted(subject, obs, f"shows {value!r}, the item has {want!r}")
            )
        elif complete is not True:
            results.append(Result.not_checked(subject, "the screen was read in part"))
        else:
            results.append(Result.held(subject, obs, "as entered"))
    settlement = settle(obligations, results)
    status = settlement.label(precedence=(Status.REFUTED, Status.NOT_CHECKED))
    seen_wrong = [r for r in results if r.status is Status.REFUTED]
    if status is not Status.HELD and seen_wrong:
        status = Status.REFUTED
    return RepeatVerdict(settlement, status)


def _item_text(item: Mapping[str, Any], slot: Slot) -> Optional[str]:
    if slot.item_key not in item:
        return None
    value = item[slot.item_key]
    if slot.role in ("checkbox", "switch"):
        if not isinstance(value, bool):
            return None
        return "true" if value else "false"
    if value is None or isinstance(value, bool):
        return None
    return str(value).strip()


def planned_rows(
    spec: RepeatSpec, items: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {"item": str(i), "slot": slot.name, "expected": _item_text(item, slot)}
        for i, item in enumerate(items)
        for slot in spec.slots
    ]


def seen_rows(
    read: InstanceRead, spec: RepeatSpec, run_id: str
) -> list[dict[str, Any]]:
    return [
        {"handle": inst.handle, "slot": slot.name, "value": value, "run_id": run_id}
        for inst in read.instances
        for slot, value in zip(spec.slots, inst.values())
    ]


# -- filling ----------------------------------------------------------------------


@dataclass(frozen=True)
class FillOutcome:
    group_id: str
    status: Status
    reason: str
    #: Item index (as text) to the container handle it was written into.
    bound: Mapping[str, str]
    added: int = 0
    wrote: bool = False
    verdict: Optional[RepeatVerdict] = None

    @property
    def held(self) -> bool:
        return self.status is Status.HELD


def fill_instances(
    actuator: Actuator,
    spec: RepeatSpec,
    items: Sequence[Mapping[str, Any]],
    *,
    run_id: str = "",
) -> FillOutcome:
    """Add, fill and check one instance per item (see the module doc)."""

    gid = spec.group_id

    def stop(reason: str, **kw: Any) -> FillOutcome:
        return FillOutcome(gid, Status.NOT_CHECKED, reason, kw.pop("bound", {}), **kw)

    planned = planned_rows(spec, items)
    missing = [p for p in planned if p["expected"] is None]
    if missing:
        return stop(
            f"item {missing[0]['item']} has no usable value for {missing[0]['slot']!r}"
        )
    if len(items) > spec.max_instances:
        return stop(f"{len(items)} items is more than the group's {spec.max_instances}")
    read = find_instances(actuator.observe(), spec)
    if not read.usable:
        return stop(read.problem)
    if any(not inst.blank() for inst in read.instances):
        return stop("an instance already holds data the run did not enter")
    if len(read.instances) > len(items):
        return stop(
            f"the screen shows {len(read.instances)} instances for {len(items)} items; "
            "the run never deletes one"
        )
    added = 0
    while len(read.instances) < len(items):
        try:
            add = match_one(spec.add, actuator.observe())
        except (MissingControl, AmbiguousControl) as exc:
            return stop(str(exc), added=added)
        w0 = actuator.write_count()
        actuator.click(add.handle)
        w1 = actuator.write_count()
        added += 1
        if w0 is None or w1 is None:
            return stop(
                "this backend cannot count writes, so an add cannot be shown safe",
                added=added,
            )
        if w1 - w0 != spec.add_writes:
            return stop(
                f"adding an instance wrote {w1 - w0} times; the demo wrote {spec.add_writes}",
                added=added,
                wrote=True,
            )
        after = find_instances(actuator.observe(), spec)
        if not after.usable:
            return stop(after.problem, added=added)
        old = {i.handle for i in read.instances}
        new = [i for i in after.instances if i.handle not in old]
        if len(after.instances) != len(read.instances) + 1 or len(new) != 1:
            return stop(
                f"one add went from {len(read.instances)} to {len(after.instances)} instances",
                added=added,
            )
        if not new[0].blank():
            return stop("the added instance is not blank", added=added)
        read = after
    bound = {str(i): inst.handle for i, inst in enumerate(read.instances)}
    for i, inst in enumerate(read.instances):
        for slot, ctrl in zip(spec.slots, inst.controls):
            value = _item_text(items[i], slot)
            assert value is not None  # checked above
            if slot.role in ("checkbox", "switch"):
                actuator.set_checked(ctrl.handle, value == "true")
            else:
                actuator.set_text(ctrl.handle, value)
    final = find_instances(actuator.observe(), spec)
    if not final.usable:
        return FillOutcome(gid, Status.NOT_CHECKED, final.problem, bound, added, True)
    verdict = judge_instances(
        planned, bound, seen_rows(final, spec, run_id), run_id=run_id, complete=True
    )
    reason = (
        "every instance shows its item"
        if verdict.status is Status.HELD
        else ("an instance does not show its item")
    )
    return FillOutcome(gid, verdict.status, reason, bound, added, True, verdict)


def location_rows(
    spec: RepeatSpec, items: Sequence[Mapping[str, Any]], bound: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The per-instance checks as rows shaped like effect locations: one per
    item and slot, found by the instance's container and the slot's name,
    expected once, read on the same screen."""

    return [
        {
            "id": f"{spec.group_id}:{i}:{slot.name}",
            "keys": [f"{spec.group_id}[{i}].{slot.item_key}"],
            "start": "same_screen",
            "locator": {"kind": "region", "value": bound[str(i)], "column": slot.name},
            "expected": {
                f"{spec.group_id}[{i}].{slot.item_key}": _item_text(item, slot)
            },
            "expected_count": 1,
        }
        for i, item in enumerate(items)
        if str(i) in bound
        for slot in spec.slots
    ]


# -- from a demonstration ----------------------------------------------------------


def _common_box(ctrls: Sequence[Control]) -> Optional[Container]:
    """The innermost container every control shares."""

    if not ctrls:
        return None
    shared = list(ctrls[0].containers)
    for ctrl in ctrls[1:]:
        handles = [c.handle for c in ctrl.containers]
        shared = [c for c in shared if c.handle in handles]
    return shared[-1] if shared else None


def learn_repeats(demo: Sequence[DemoStep]) -> tuple[RepeatSpec, ...]:
    """Each click that added a block of editable controls in a new container,
    where the block's fields (role and name) match the fields of a container of
    the same kind already on the screen. A list that starts empty is learned
    from its second add, when the first added block is the sibling."""

    out: list[RepeatSpec] = []
    for index, step in enumerate(demo):
        if step.action != "click" or not step.target.activatable:
            continue
        before = {c.handle for c in step.before.controls}
        new = [c for c in step.after.controls if c.editable and c.handle not in before]
        if not new:
            continue
        box = _common_box(new)
        if box is None or box.kind not in INSTANCE_KINDS:
            continue
        if box.handle in {b.handle for c in step.before.controls for b in c.containers}:
            continue  # the controls appeared inside an existing container
        fields = Counter((c.role, norm(c.name)) for c in new)
        if any(n != 1 for n in fields.values()):
            continue
        siblings: dict[str, Counter[tuple[str, str]]] = {}
        for ctrl in step.before.controls:
            if not ctrl.editable:
                continue
            kin = [b for b in ctrl.containers if b.kind == box.kind]
            if kin:
                siblings.setdefault(kin[-1].handle, Counter())[
                    (ctrl.role, norm(ctrl.name))
                ] += 1
        if not any(sig == fields for sig in siblings.values()):
            continue
        add = spec_for(step.target, step.before)
        if add is None or step.writes is None:
            continue
        slots = tuple(Slot(c.role, c.name) for c in new)
        out.append(
            RepeatSpec(
                group_id=f"group-{index}",
                add=add,
                instance_kind=box.kind,
                slots=slots,
                add_writes=step.writes,
            )
        )
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for spec in out:
        key = (spec.add, spec.instance_kind, spec.slots)
        if key not in seen:
            seen.add(key)
            unique.append(spec)
    return tuple(unique)


def map_slots(
    spec: RepeatSpec,
    demo: Sequence[DemoStep],
    items: Sequence[Mapping[str, Any]],
) -> tuple[RepeatSpec, dict[str, str]]:
    """Propose the item key for each slot from the demonstration's typed
    values: a slot maps to key ``k`` when, for every instance the demo filled,
    the value typed there equals ``items[i][k]`` and no other key fits. Slots
    that do not map are returned with a reason. A proposal, for the author."""

    final = demo[-1].after if demo else Screen()
    read = find_instances(final, spec)
    problems: dict[str, str] = {}
    if not read.usable or len(read.instances) != len(items):
        return spec, {
            s.name: "the demo's instances do not line up with its items"
            for s in spec.slots
        }
    typed: dict[str, str] = {}
    for step in demo:
        if step.action == "type":
            typed[step.target.handle] = step.value.strip()
    slots = []
    for n, slot in enumerate(spec.slots):
        keys: Optional[set[str]] = None
        for i, inst in enumerate(read.instances):
            value = typed.get(inst.controls[n].handle)
            fit = {
                k
                for k, v in items[i].items()
                if value is not None and str(v).strip() == value
            }
            keys = fit if keys is None else keys & fit
        if keys and len(keys) == 1:
            slots.append(Slot(slot.role, slot.name, next(iter(keys))))
        else:
            slots.append(slot)
            problems[slot.name] = "no single item key matches what the demo typed"
    return (
        RepeatSpec(
            spec.group_id,
            spec.add,
            spec.instance_kind,
            tuple(slots),
            spec.within,
            spec.add_writes,
            spec.max_instances,
        ),
        problems,
    )
