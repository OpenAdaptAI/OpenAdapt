"""Hub pages whose sections open in dialogs, with completion tracked per section.

Many apps put a long record behind one page of cards or a checklist. A chart
has Allergies, Medications and Vitals. An onboarding page has Company, Billing
and Team. Each section's Edit button opens a dialog, Save closes it, and the
card shows that the section is done. Two things went wrong on such pages
before:

- the page had no "Next" control, so a wizard walk stopped on the hub; and
- the section dialog was taken for a confirmation modal, which either stops
  the run or, worse, gets "confirmed".

This module treats the hub as its own shape:

- :func:`classify_dialog` sorts an open dialog by what is in it. A dialog
  holding any editable control is a *form* dialog, whatever its words. It is
  a declared section's form only when its name matches that section, and
  every other form dialog stops the run. Only a dialog with no editable
  control can be a confirmation, and that is left to the caller's modal rules.
- :class:`SectionSpec` is pack data for one section: its opener on the hub,
  the dialog it opens, the save control in the dialog, the card that shows
  its status, and the card tokens that mean done. :func:`learn_hub` derives
  them from a demonstration: the tokens the card gained when the section was
  saved, minus the values typed in the dialog.
- :func:`run_section` opens one section, lets the caller fill the dialog, and
  saves it. It stops if any of these fails: no other dialog may be open, the
  card must not already show done, exactly the declared dialog must open, the
  dialog must close on save with the demonstrated number of writes, and the
  card must then show done on a fresh read of the hub.
- :func:`judge_hub` is a verdict site. Its obligations are the planned
  sections, fixed before any is opened, and it settles them against a fresh
  read of the hub. Completion is read from the hub, never inferred from
  having clicked Save.

:func:`expected_writes` gives the writes the planned sections should make, so
a caller with a write budget can size it to the sections instead of to one
commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from openadapt_flow.gui.model import (
    Actuator,
    AmbiguousControl,
    ControlSpec,
    DemoStep,
    Dialog,
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
    "DialogKind",
    "HubVerdict",
    "SectionLearned",
    "SectionOutcome",
    "SectionSpec",
    "card_reading",
    "card_tokens",
    "classify_dialog",
    "expected_writes",
    "judge_hub",
    "judge_hub_screen",
    "learn_hub",
    "run_section",
]


def card_tokens(text: str) -> tuple[str, ...]:
    """A card's text as case-folded word tokens; a symbol (a check mark) is
    a token of its own."""

    return tuple(re.findall(r"\w+|[^\w\s]", norm(text)))


@dataclass(frozen=True)
class SectionSpec:
    """Pack data for one section of a hub.

    ``card`` is the name of the hub container that holds the opener and shows
    the section's status. The section is done when the card's tokens include
    every ``done_tokens`` entry and none of ``pending_tokens``."""

    section_id: str
    opener: ControlSpec
    dialog_name: str
    save: ControlSpec
    card: str
    done_tokens: tuple[str, ...]
    pending_tokens: tuple[str, ...] = ()
    save_writes: int = 1
    required: bool = True

    def __post_init__(self) -> None:
        if not self.done_tokens:
            raise ValueError(
                "a section needs card tokens that show it is done; without "
                "them completion cannot be read from the hub"
            )
        if self.save_writes < 0:
            raise ValueError("save_writes is a count")
        if set(self.done_tokens) & set(self.pending_tokens):
            raise ValueError("a token cannot mean both done and pending")


# -- dialogs ----------------------------------------------------------------------


class DialogKind(str, Enum):
    SECTION_FORM = "section_form"
    UNDECLARED_FORM = "undeclared_form"
    CONFIRMATION = "confirmation"


def classify_dialog(
    screen: Screen, dialog: Dialog, sections: Sequence[SectionSpec] = ()
) -> tuple[DialogKind, Optional[SectionSpec]]:
    """What an open dialog is. Any editable control inside makes it a form
    dialog, never a confirmation. It is a declared section's form only when
    exactly one section names it."""

    editable = [c for c in screen.in_dialog(dialog.handle) if c.editable]
    if not editable:
        return DialogKind.CONFIRMATION, None
    named = [s for s in sections if norm(s.dialog_name) == norm(dialog.name)]
    if len(named) == 1:
        return DialogKind.SECTION_FORM, named[0]
    return DialogKind.UNDECLARED_FORM, None


# -- reading a card ---------------------------------------------------------------


@dataclass(frozen=True)
class CardReading:
    """What a fresh read of the hub shows for one section's card."""

    found: bool
    done: Optional[bool]
    reason: str = ""


def card_reading(screen: Screen, section: SectionSpec) -> CardReading:
    """Find the section's card through its opener (outside any dialog) and
    read whether it shows done."""

    opener = ControlSpec(
        section.opener.role,
        section.opener.name,
        section.opener.containers,
        section.opener.target,
        dialog=False,
    )
    try:
        ctrl = match_one(opener, screen)
    except (MissingControl, AmbiguousControl) as exc:
        return CardReading(False, None, str(exc))
    cards = [c for c in ctrl.containers if norm(c.name) == norm(section.card)]
    if len(cards) != 1:
        return CardReading(False, None, f"{len(cards)} cards named {section.card!r}")
    tokens = set(card_tokens(cards[0].text))
    if set(section.pending_tokens) & tokens:
        return CardReading(True, False, "the card still shows it pending")
    if not set(section.done_tokens) <= tokens:
        return CardReading(True, False, "the card does not show it done")
    return CardReading(True, True)


# -- the verdict ------------------------------------------------------------------


@dataclass(frozen=True)
class HubVerdict:
    settlement: Settlement
    status: Status


@verdict_site("gui.hub.judge", classes="C1 C2")
def judge_hub(
    planned: Sequence[str],
    cards: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    complete: bool = True,
    open_dialogs: int = 0,
) -> HubVerdict:
    """Settle the planned sections against a fresh read of the hub.

    ``planned`` is the section ids the run set out to complete, fixed before
    any was opened. ``cards`` rows carry ``section_id``, ``found``, ``done``
    and ``run_id``. A section holds only when its card was found and shows
    done, on a complete read with no dialog left open. A card that shows it
    not done is refuted. One that was not found, or whose state is not a
    bool, is not checked. A section nobody planned is unexpected."""

    obligations = Obligations.fix(
        (
            Subject(run_id=run_id, surface="gui", key=str(s), field="section")
            for s in planned
        ),
        source="hub plan",
    )
    results = []
    for row in cards:
        subject = Subject(
            run_id=str(row.get("run_id") or ""),
            surface="gui",
            key=str(row.get("section_id")),
            field="section",
        )
        obs = Observed(dict(row), complete=complete is True)
        done = row.get("done")
        if row.get("found") is not True or not isinstance(done, bool):
            results.append(Result.not_checked(subject, "the card was not read"))
        elif done is False:
            results.append(Result.refuted(subject, obs, "the card does not show done"))
        elif complete is not True:
            results.append(Result.not_checked(subject, "the hub was read in part"))
        elif type(open_dialogs) is not int or open_dialogs != 0:
            results.append(Result.not_checked(subject, "a dialog is still open"))
        else:
            results.append(Result.held(subject, obs, "the card shows done"))
    settlement = settle(obligations, results)
    status = settlement.label(precedence=(Status.REFUTED, Status.NOT_CHECKED))
    seen_wrong = [r for r in results if r.status is Status.REFUTED]
    if status is not Status.HELD and seen_wrong:
        status = Status.REFUTED
    return HubVerdict(settlement, status)


def judge_hub_screen(
    screen: Screen,
    sections: Sequence[SectionSpec],
    planned: Iterable[str],
    *,
    run_id: str,
) -> HubVerdict:
    """:func:`judge_hub` on a fresh read of the hub."""

    planned = list(planned)
    rows = []
    for section in sections:
        if section.section_id not in planned:
            continue
        reading = card_reading(screen, section)
        rows.append(
            {
                "section_id": section.section_id,
                "found": reading.found,
                "done": reading.done,
                "run_id": run_id,
            }
        )
    return judge_hub(
        planned,
        rows,
        run_id=run_id,
        complete=screen.complete,
        open_dialogs=len(screen.dialogs),
    )


def expected_writes(sections: Sequence[SectionSpec], planned: Iterable[str]) -> int:
    """Writes the planned sections' saves should make, in total."""

    wanted = set(planned)
    return sum(s.save_writes for s in sections if s.section_id in wanted)


# -- running one section ----------------------------------------------------------


@dataclass(frozen=True)
class SectionOutcome:
    """How one section went. ``status`` is HELD only when :func:`judge_hub`
    held for the section on a fresh read of the hub (``verdict``)."""

    section_id: str
    status: Status
    reason: str
    opened: bool = False
    saved: bool = False
    hub: Optional[Screen] = None
    verdict: Optional[HubVerdict] = None

    @property
    def held(self) -> bool:
        return self.status is Status.HELD


def run_section(
    actuator: Actuator,
    section: SectionSpec,
    fill: Callable[[Screen, Dialog], None],
    *,
    sections: Sequence[SectionSpec] = (),
    run_id: str = "",
) -> SectionOutcome:
    """Open ``section``, let ``fill`` enter its values, save it, and confirm
    on the hub that it is done.

    ``fill`` gets the screen with the dialog open and the dialog. It must
    act only inside that dialog."""

    sid = section.section_id

    def stop(reason: str, **kw: Any) -> SectionOutcome:
        return SectionOutcome(sid, Status.NOT_CHECKED, reason, **kw)

    hub = actuator.observe()
    if hub.dialogs:
        return stop("a dialog is already open on the hub")
    before = card_reading(hub, section)
    if not before.found:
        return stop(f"the section's card is not on the hub: {before.reason}")
    if before.done:
        return stop("the card shows done before the run opened the section")
    try:
        opener = match_one(
            ControlSpec(
                section.opener.role,
                section.opener.name,
                section.opener.containers,
                section.opener.target,
                dialog=False,
            ),
            hub,
        )
    except (MissingControl, AmbiguousControl) as exc:
        return stop(str(exc))
    w0 = actuator.write_count()
    if w0 is None:
        return stop("this backend cannot count writes; a section save cannot be shown")
    actuator.click(opener.handle)
    opened = actuator.observe()
    if len(opened.dialogs) != 1:
        return stop(f"{len(opened.dialogs)} dialogs opened", opened=True)
    dialog = opened.dialogs[0]
    kind, owner = classify_dialog(opened, dialog, sections or (section,))
    if kind is not DialogKind.SECTION_FORM or owner is None or owner.section_id != sid:
        return stop(
            f"the dialog that opened is a {kind.value} dialog, not {sid!r}", opened=True
        )
    if actuator.write_count() != w0:
        return stop("opening the section wrote", opened=True)
    fill(opened, dialog)
    filled = actuator.observe()
    if [d.handle for d in filled.dialogs] != [dialog.handle]:
        return stop("the dialog changed while it was filled", opened=True)
    try:
        save = match_one(
            ControlSpec(
                section.save.role,
                section.save.name,
                section.save.containers,
                section.save.target,
                dialog=True,
            ),
            filled,
        )
    except (MissingControl, AmbiguousControl) as exc:
        return stop(str(exc), opened=True)
    if save.dialog != dialog.handle:
        return stop("the save control is outside the section's dialog", opened=True)
    w1 = actuator.write_count()
    actuator.click(save.handle)
    w2 = actuator.write_count()
    after = actuator.observe()
    if after.dialogs:
        errors = [c.error for c in after.controls if c.error] or list(after.errors)
        reason = (
            f"the dialog did not close: {errors[0]}"
            if errors
            else "the dialog did not close"
        )
        return SectionOutcome(sid, Status.REFUTED, reason, True, False, after)
    if w1 is None or w2 is None:
        return SectionOutcome(
            sid,
            Status.NOT_CHECKED,
            "the save's writes were not counted",
            True,
            True,
            after,
        )
    if w2 - w1 != section.save_writes:
        reason = f"saving wrote {w2 - w1}; the demo wrote {section.save_writes}"
        return SectionOutcome(sid, Status.REFUTED, reason, True, True, after)
    verdict = judge_hub_screen(after, (section,), [sid], run_id=run_id)
    reading = card_reading(after, section)
    reason = (
        "the card shows done"
        if verdict.status is Status.HELD
        else (reading.reason or "the card was not read")
    )
    return SectionOutcome(sid, verdict.status, reason, True, True, after, verdict)


# -- from a demonstration ----------------------------------------------------------


@dataclass(frozen=True)
class SectionLearned:
    """One section found in a demonstration. ``spec`` is None when the card
    showed no new token on save (``reason`` says why); the pack author must
    then say how the hub shows the section done."""

    opener_index: int
    save_index: int
    spec: Optional[SectionSpec]
    reason: str = ""


def learn_hub(demo: Sequence[DemoStep]) -> tuple[SectionLearned, ...]:
    """Find each open, fill, save cycle: a click that opened one form dialog,
    then a click inside it that closed it. The card is the opener's innermost
    named container. Its done tokens are those it gained on save, less any
    token of a value typed in the dialog; its pending tokens are those it lost."""

    out = []
    i = 0
    while i < len(demo):
        step = demo[i]
        new = [
            d
            for d in step.after.dialogs
            if d.handle not in {x.handle for x in step.before.dialogs}
        ]
        if step.action != "click" or step.target.dialog or len(new) != 1:
            i += 1
            continue
        dialog = new[0]
        typed: set[str] = set()
        j = i + 1
        closed_by: Optional[int] = None
        while j < len(demo) and demo[j].target.dialog == dialog.handle:
            if demo[j].value:
                typed |= set(card_tokens(demo[j].value))
            if demo[j].action == "click" and dialog.handle not in {
                d.handle for d in demo[j].after.dialogs
            }:
                closed_by = j
                break
            j += 1
        if closed_by is None:
            # The dialog was left some other way (or never closed): not a
            # section cycle the demo showed whole.
            i = j
            continue
        out.append(_learned(step, demo[closed_by], dialog, typed, i, closed_by))
        i = closed_by + 1
    return tuple(out)


def _learned(
    open_step: DemoStep,
    save_step: DemoStep,
    dialog: Dialog,
    typed: set[str],
    i: int,
    j: int,
) -> SectionLearned:
    opener = open_step.target
    named = [c for c in opener.containers if c.name and c.kind != "dialog"]
    if not named:
        return SectionLearned(i, j, None, "the opener sits in no named card")
    card = named[-1]
    after_ctrl = save_step.after.control(opener.handle)
    after_card = None
    if after_ctrl is not None:
        after_card = next(
            (c for c in after_ctrl.containers if c.handle == card.handle), None
        )
    if after_card is None:
        return SectionLearned(i, j, None, "the card was not on the hub after saving")
    before_t, after_t = set(card_tokens(card.text)), set(card_tokens(after_card.text))
    done = tuple(sorted(after_t - before_t - typed))
    pending = tuple(sorted(before_t - after_t - typed))
    open_spec = spec_for(opener, open_step.before)
    save_spec = spec_for(save_step.target, save_step.before)
    if not done:
        return SectionLearned(
            i, j, None, "the card gained no token when the section was saved"
        )
    if open_spec is None or save_spec is None:
        return SectionLearned(
            i, j, None, "the opener or the save control is not unique"
        )
    if save_step.writes is None:
        return SectionLearned(
            i, j, None, "the backend could not count the save's writes"
        )
    return SectionLearned(
        i,
        j,
        SectionSpec(
            section_id=norm(card.name).replace(" ", "_"),
            opener=open_spec,
            dialog_name=dialog.name,
            save=save_spec,
            card=card.name,
            done_tokens=done,
            pending_tokens=pending,
            save_writes=save_step.writes,
        ),
    )
