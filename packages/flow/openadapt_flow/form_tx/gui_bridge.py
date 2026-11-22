"""Run the general GUI mechanisms (``openadapt_flow.gui``) inside ``form_tx``.

The form reconciler calls three functions here, and only when the pack opts
in with ``FormPack.gui`` (see :mod:`openadapt_flow.form_tx.gui_pack`):

- :func:`before_leaving_screen`: on a wizard screen, before the unbound scan
  and the screen's own next control:

  - fill and check any repeating group declared for the screen
    (:func:`openadapt_flow.gui.repeating.fill_instances`);
  - walk a hub screen section by section (:func:`openadapt_flow.gui.hub.run_section`,
    with the reconciler's own binding pass as the fill) and leave it only
    when every card reads done;
  - take a declared step (a link, a tab) with :func:`openadapt_flow.gui.steps.take_step`;
  - stop on a blank choice no binding covers (``stop_on_blank_choice``).

- :func:`before_commit`: on the commit screen, read review rows drawn as a
  label beside its value (the form snapshot does not pair them) and stop on
  one that differs from the intake; then plan, tick and check the
  declared attestations from the run's grants
  (:mod:`openadapt_flow.gui.attestations`). With no grant the run stops.
- :func:`is_form_dialog`: a modal that holds an editable control is a form
  dialog and is never confirmed through a dossier flag.

Each returns ``None`` (nothing to do here, carry on), :data:`CONTINUE` (the
screen moved; take a fresh snapshot) or a ``FormTxResult`` that stops the
run. Every stop names its reason. Nothing here names an application.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional, Union

from openadapt_flow.form_tx.codecs import format_for_text_entry
from openadapt_flow.form_tx.gui_pack import GuiHubSpec, GuiMechanismsSpec, GuiRepeatSpec
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    FormHaltItem,
    FormHaltKind,
    FormSnapshot,
    FormTxOutcome,
    FormTxResult,
    HumanDecisionAction,
    ObservedControl,
    ObservedModal,
    WizardScreenSpec,
)
from openadapt_flow.gui.adapters import screen_from_form_snapshot
from openadapt_flow.gui.attestations import (
    AttestationGrant,
    AttestationSpec,
    apply_attestations,
    judge_screen,
    plan_checkboxes,
)
from openadapt_flow.gui.hub import (
    DialogKind,
    SectionSpec,
    card_reading,
    classify_dialog,
    judge_hub_screen,
    run_section,
    save_section,
)
from openadapt_flow.gui.model import Dialog, Screen, norm
from openadapt_flow.gui.repeating import (
    RepeatSpec,
    Slot,
    fill_instances,
    find_instances,
    judge_instances,
    planned_rows,
    seen_rows,
)
from openadapt_flow.gui.steps import StepRole, StepSpec, take_step
from openadapt_flow.verdict import Status

__all__ = [
    "CONTINUE",
    "SnapshotActuator",
    "SurfaceActuator",
    "before_commit",
    "before_leaving_screen",
    "grants_for",
    "is_form_dialog",
]

CONTINUE = "continue"
Outcome = Union[None, str, FormTxResult]


# -- actuators over a form surface ------------------------------------------------


class _Base:
    def __init__(self, surface: Any, pack: Any) -> None:
        self.surface = surface
        self.pack = pack

    def snapshot(self) -> FormSnapshot:
        return self.surface.snapshot(
            entity_tables=self.pack.entity_tables,
            modal_rules=self.pack.modal_rules,
            screens=self.pack.screens,
        )

    def click(self, handle: str) -> None:
        self.surface.click(handle)

    def set_text(self, handle: str, value: str) -> None:
        self.surface.fill_text(handle, value)

    def set_checked(self, handle: str, checked: bool) -> None:
        self.surface.set_checkbox_or_radio(handle, checked)

    def write_count(self) -> Optional[int]:
        """The surface's own mutation counter: the one the commit budget uses."""

        count = getattr(self.surface, "mutation_count", None)
        return count if isinstance(count, int) and not isinstance(count, bool) else None


class SnapshotActuator(_Base):
    """Reads the screen through the form surface's own snapshot, so handles
    are the selectors the reconciler's write ledger and unbound scan use."""

    def observe(self) -> Screen:
        return screen_from_form_snapshot(self.snapshot())


class SurfaceActuator(_Base):
    """Reads a browser page directly (links, dialogs, card text), falling back
    to the form snapshot on a backend without a page."""

    def observe(self) -> Screen:
        page = getattr(self.surface, "_page", None)
        if page is None:
            return screen_from_form_snapshot(self.snapshot())
        from openadapt_flow.gui.dom import OBSERVE_JS, TEXT_CAP, screen_from_dom

        # A fresh surface snapshot first: the surface acts only on the
        # document it last read.
        self.snapshot()
        return screen_from_dom(page.evaluate(OBSERVE_JS, TEXT_CAP))


def grants_for(pack: Any, attester: str) -> tuple[AttestationGrant, ...]:
    """One grant per attestation the pack declares, made by ``attester`` for
    the statement the pack pins. The caller decides whether a person attests;
    this only builds the grants."""

    gui: Optional[GuiMechanismsSpec] = getattr(pack, "gui", None)
    if gui is None or not attester.strip():
        return ()
    return tuple(
        AttestationGrant(a.attestation_id, a.statement_sha256, attester)
        for a in gui.attestations
    )


# -- halts ------------------------------------------------------------------------


def _halt(
    rec: Any,
    session: Any,
    snapshot: FormSnapshot,
    message: str,
    *,
    sig: str,
    kind: FormHaltKind = FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
    safe_to_retry: bool = True,
    control: Optional[ObservedControl] = None,
) -> FormTxResult:
    from openadapt_flow.form_tx.reconciler import _deterministic_halt_id

    halt_id = _deterministic_halt_id(
        session.run_id,
        kind,
        snapshot.screen_id,
        control.dom_selector if control else "",
        sig,
    )
    actions = (
        (
            HumanDecisionAction.PROVIDE_VALUE,
            HumanDecisionAction.FIXED_ON_SCREEN,
            HumanDecisionAction.ESCALATE_TO_OWNER,
            HumanDecisionAction.ABORT_RUN,
        )
        if control is not None
        else (HumanDecisionAction.ESCALATE_TO_OWNER, HumanDecisionAction.ABORT_RUN)
    )
    extra: dict[str, Any] = {}
    if control is not None:
        extra = {
            "control_id": control.control_id,
            "dom_selector": control.dom_selector,
            "field_label": control.group_label or control.label_text,
            "available_options": control.options,
        }
    halt = FormHaltItem(
        halt_id=halt_id,
        kind=kind,
        screen_id=snapshot.screen_id,
        message=message,
        allowed_actions=actions,
        **extra,
    )
    meta: dict[str, Any] = {"field_signature": sig}
    if control is not None:
        meta.update(control=control, binding=None)
    session._halt_metadata[halt_id] = meta
    return rec._build_result(
        session,
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        safe_to_retry=safe_to_retry,
        halts=(halt,),
        what_happened=message,
    )


def _state(session: Any) -> dict[str, Any]:
    state = getattr(session, "_gui_state", None)
    if state is None:
        state = {"groups": set(), "sections": set()}
        session._gui_state = state
    return state


# -- covered controls and blank choices -------------------------------------------


def _covered(session: Any, snapshot: FormSnapshot) -> set[str]:
    pack = session.pack
    anchors = [b.anchor for b in pack.bindings] + [
        u.anchor for u in pack.unbound_controls
    ]
    for sub in pack.popup_subroutines:
        anchors += [sub.trigger_anchor, sub.parent_return_anchor]
    out: set[str] = set(session._filled_selectors)
    for anchor in anchors:
        try:
            m = match_anchor_in_snapshot(
                anchor, snapshot, include_readonly=True, facts=session.effective_facts
            )
        except AmbiguousAnchorError:
            continue
        if m is not None:
            out.add(m.dom_selector)
            if m.radio_group:
                out.add(f"group|{m.radio_group}")
    return out


def _choice_blank(ctrl: ObservedControl) -> bool:
    """A select with no value, or one showing an option whose value is empty
    (a "Choose ..." prompt); a radio that is not checked."""

    if ctrl.input_type.casefold() == "radio" or ctrl.role.casefold() == "radio":
        return ctrl.checked is not True
    if not ctrl.current_value:
        return True
    shown = [o for o in ctrl.options if ctrl.current_value in (o.value, o.label)]
    return bool(shown) and all(not o.value for o in shown)


def _is_choice(ctrl: ObservedControl) -> bool:
    tag, kind, role = (
        ctrl.tag.casefold(),
        ctrl.input_type.casefold(),
        ctrl.role.casefold(),
    )
    return tag == "select" or kind == "radio" or role in ("radio", "listbox")


def _blank_choice_halt(
    rec: Any, session: Any, snapshot: FormSnapshot
) -> Optional[FormTxResult]:
    gui: Optional[GuiMechanismsSpec] = session.pack.gui
    if gui is None or not gui.stop_on_blank_choice:
        return None
    covered = _covered(session, snapshot)
    groups: dict[str, list[ObservedControl]] = {}
    for ctrl in snapshot.controls:
        if not ctrl.visible or ctrl.disabled or ctrl.readonly or ctrl.modal_container:
            continue
        if not _is_choice(ctrl) or ctrl.dom_selector in covered:
            continue
        if ctrl.radio_group and f"group|{ctrl.radio_group}" in covered:
            continue
        key = f"radio|{ctrl.radio_group}" if ctrl.radio_group else ctrl.dom_selector
        groups.setdefault(key, []).append(ctrl)
    for key, ctrls in groups.items():
        blank = all(_choice_blank(c) for c in ctrls)
        if blank:
            ctrl = ctrls[0]
            label = ctrl.group_label or ctrl.label_text or ctrl.dom_selector
            return _halt(
                rec,
                session,
                snapshot,
                f"{label!r} is a question no binding answers, and it is blank",
                sig=f"blank_choice|{key}",
                kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                control=ctrl,
            )
    return None


# -- repeating groups --------------------------------------------------------------


class _ScopedActuator(SnapshotActuator):
    """Only slot controls whose container name matches the group's pattern."""

    def __init__(self, surface: Any, pack: Any, spec: GuiRepeatSpec) -> None:
        super().__init__(surface, pack)
        self.names = {(s.role, norm(s.name)) for s in spec.slots}
        self.pattern = (
            re.compile(spec.instance_pattern) if spec.instance_pattern else None
        )

    def observe(self) -> Screen:
        screen = super().observe()
        if self.pattern is None:
            return screen
        keep = []
        for c in screen.controls:
            if (c.role, norm(c.name)) in self.names and not any(
                self.pattern.search(b.name) for b in c.containers if b.name
            ):
                continue
            keep.append(c)
        return Screen(
            tuple(keep),
            screen.dialogs,
            screen.location,
            screen.title,
            screen.text,
            screen.complete,
            screen.errors,
        )


def _items(session: Any, spec: GuiRepeatSpec) -> Optional[list[dict[str, Any]]]:
    fact = session.effective_facts.get(
        spec.intake_key
    ) or session.dossier.grounded_fact(spec.intake_key)
    raw = getattr(fact, "value", None)
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(i, dict) for i in raw)
    ):
        return None
    out = []
    for item in raw:
        row: dict[str, Any] = {}
        for slot in spec.slots:
            if slot.item_key not in item or item[slot.item_key] in (None, ""):
                continue
            value = item[slot.item_key]
            if slot.role in ("checkbox", "switch"):
                row[slot.item_key] = value if isinstance(value, bool) else None
            else:
                row[slot.item_key] = format_for_text_entry(value, slot.codec)
        out.append(row)
    return out


def _repeat(
    rec: Any, session: Any, surface: Any, snapshot: FormSnapshot, spec: GuiRepeatSpec
) -> Outcome:
    items = _items(session, spec)
    if items is None:
        return _halt(
            rec,
            session,
            snapshot,
            f"the intake has no list {spec.intake_key!r} for the group {spec.group_id!r}",
            sig=f"repeat_items|{spec.group_id}",
        )
    rspec = RepeatSpec(
        group_id=spec.group_id,
        add=spec.add.spec(dialog=False),
        instance_kind=spec.instance_kind,
        slots=tuple(Slot(s.role, s.name, s.item_key) for s in spec.slots),
        add_writes=spec.add_writes,
        max_instances=spec.max_instances,
    )
    act = _ScopedActuator(surface, session.pack, spec)
    read = find_instances(act.observe(), rspec)
    planned = planned_rows(rspec, items)
    bound: dict[str, str] = {}
    held = False
    wrote = False
    if (
        read.usable
        and len(read.instances) == len(items)
        and not all(i.blank() for i in read.instances)
        and spec.group_id in _state(session)["groups"]
    ):
        # This run already entered them (the pass after filling, or a walk
        # back after a sign-in): check them, write nothing. Rows showing data
        # this run did not enter go to fill_instances, which stops on them.
        bound = {str(i): inst.handle for i, inst in enumerate(read.instances)}
        verdict = judge_instances(
            planned,
            bound,
            seen_rows(read, rspec, session.run_id),
            run_id=session.run_id,
        )
        held = verdict.status is Status.HELD
        if not held:
            return _halt(
                rec,
                session,
                snapshot,
                f"the group {spec.group_id!r} already shows values that do not match the intake",
                sig=f"repeat_check|{spec.group_id}",
            )
    else:
        wrote = True
        outcome = fill_instances(act, rspec, items, run_id=session.run_id)
        if not outcome.held:
            return _halt(
                rec,
                session,
                snapshot,
                f"repeating group {spec.group_id!r}: {outcome.reason}",
                sig=f"repeat_fill|{spec.group_id}",
                safe_to_retry=not outcome.wrote,
            )
        bound = dict(outcome.bound)
        read = find_instances(act.observe(), rspec)
    by_handle = {inst.handle: inst for inst in read.instances}
    for i, handle in bound.items():
        inst = by_handle.get(handle)
        if inst is None:
            continue
        for slot, ctrl in zip(spec.slots, inst.controls):
            session._filled_selectors.add(ctrl.handle)
            session.record_write(
                source="auto_fill",
                dossier_key=f"{spec.intake_key}[{i}].{slot.item_key}",
                label=f"{inst.name or spec.group_id} {slot.name}",
                selector=ctrl.handle,
            )
    _state(session)["groups"].add(spec.group_id)
    # Rows were written: look again. Rows that already showed the intake
    # were only checked, so the screen goes on with this snapshot.
    return CONTINUE if wrote else None


# -- hubs --------------------------------------------------------------------------


class _FillHalt(Exception):
    def __init__(self, result: FormTxResult) -> None:
        super().__init__(result.what_happened)
        self.result = result


def _section_specs(hub: GuiHubSpec) -> list[SectionSpec]:
    return [
        SectionSpec(
            section_id=s.section_id,
            opener=s.opener.spec(dialog=False),
            dialog_name=s.dialog_name,
            save=s.save.spec(dialog=True),
            card=s.card,
            done_tokens=s.done_tokens,
            pending_tokens=s.pending_tokens,
            save_writes=s.save_writes,
        )
        for s in hub.sections
    ]


def _fill_with_bindings(
    rec: Any, session: Any, surface: Any
) -> Callable[[Screen, Dialog], None]:
    pack = session.pack

    def snap() -> FormSnapshot:
        return surface.snapshot(
            entity_tables=pack.entity_tables,
            modal_rules=pack.modal_rules,
            screens=pack.screens,
        )

    def fill(_screen: Screen, _dialog: Dialog) -> None:
        for _ in range(rec.max_passes_per_screen):
            snapshot = snap()
            session._last_observation = snapshot
            edited, _, halt = rec._reconcile_bindings_pass(
                surface, session, snapshot, pack.bindings
            )
            if halt is not None:
                raise _FillHalt(
                    rec._build_result(
                        session,
                        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                        safe_to_retry=True,
                        halts=(halt,),
                        what_happened=halt.message,
                    )
                )
            if not edited:
                break
        snapshot = snap()
        halts = rec._collect_ungrounded_required_halts(session, snapshot, pack.bindings)
        if halts:
            raise _FillHalt(
                rec._build_result(
                    session,
                    outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
                    safe_to_retry=True,
                    halts=tuple(halts),
                    what_happened=halts[0].message,
                )
            )
        # The section's dialog is the only one open (run_section and the
        # resume path check that), and its fields are the screen being left:
        # the scans below skip controls inside a modal, so read them as not.
        in_dialog = snapshot.model_copy(
            update={
                "controls": tuple(
                    c.model_copy(update={"modal_container": ""})
                    for c in snapshot.controls
                )
            }
        )
        blank = _blank_choice_halt(rec, session, in_dialog)
        if blank is not None:
            raise _FillHalt(blank)
        unbound = _unbound_halt(rec, session, in_dialog)
        if unbound is not None:
            raise _FillHalt(unbound)

    return fill


def _auth_on_screen(rec: Any, session: Any, surface: Any) -> bool:
    from openadapt_flow.form_tx.reconciler import _auth_wall_reason

    pack = session.pack
    snapshot = surface.snapshot(
        entity_tables=pack.entity_tables,
        modal_rules=pack.modal_rules,
        screens=pack.screens,
    )
    return _auth_wall_reason(snapshot, surface.ui_profile) is not None


def _hub(
    rec: Any, session: Any, surface: Any, snapshot: FormSnapshot, hub: GuiHubSpec
) -> Outcome:
    act = SurfaceActuator(surface, session.pack)
    sections = _section_specs(hub)
    saved: set[str] = _state(session)["sections"]
    fill = _fill_with_bindings(rec, session, surface)

    def failed(sid: str, reason: str, *, wrote: bool) -> Outcome:
        if _auth_on_screen(rec, session, surface):
            return CONTINUE  # the main loop stops on the sign-in wall
        return _halt(
            rec,
            session,
            snapshot,
            f"hub section {sid!r}: {reason}",
            sig=f"hub|{hub.screen_id}|{sid}",
            safe_to_retry=not wrote,
        )

    try:
        screen = act.observe()
        if screen.dialogs:
            if len(screen.dialogs) != 1:
                return failed(
                    "?", f"{len(screen.dialogs)} dialogs are open", wrote=False
                )
            kind, owner = classify_dialog(screen, screen.dialogs[0], sections)
            if kind is DialogKind.CONFIRMATION:
                return None  # the reconciler's modal rules decide
            if owner is None:
                return failed("?", "an undeclared form dialog is open", wrote=False)
            fill(screen, screen.dialogs[0])
            out = save_section(act, owner, screen.dialogs[0], run_id=session.run_id)
            if not out.held:
                return failed(owner.section_id, out.reason, wrote=out.saved)
            saved.add(owner.section_id)
            screen = act.observe()
        for section in sections:
            reading = card_reading(screen, section)
            if reading.done and section.section_id in saved:
                continue
            if reading.done:
                return failed(
                    section.section_id,
                    "the card shows done before the run opened it",
                    wrote=False,
                )
            out = run_section(
                act, section, fill, sections=sections, run_id=session.run_id
            )
            if not out.held:
                return failed(section.section_id, out.reason, wrote=out.saved)
            saved.add(section.section_id)
            screen = act.observe()
    except _FillHalt as stop:
        return stop.result
    verdict = judge_hub_screen(
        screen, sections, [s.section_id for s in sections], run_id=session.run_id
    )
    if verdict.status is not Status.HELD:
        return failed("*", "not every section reads done on the hub", wrote=True)
    unbound = _unbound_halt(rec, session, act.snapshot())
    if unbound is not None:
        return unbound
    step = take_step(
        act,
        StepSpec(
            f"{hub.screen_id}:leave", hub.leave.spec(dialog=False), StepRole.NAVIGATE
        ),
        run_id=session.run_id,
    )
    if not step.held:
        return failed("*", f"leaving the hub: {step.result.reason}", wrote=False)
    return CONTINUE


# -- entry points ------------------------------------------------------------------


def before_leaving_screen(
    rec: Any,
    session: Any,
    surface: Any,
    snapshot: FormSnapshot,
    screen: Optional[WizardScreenSpec],
) -> Outcome:
    gui: Optional[GuiMechanismsSpec] = session.pack.gui
    if gui is None:
        return None
    sid = screen.screen_id if screen is not None else None
    for spec in gui.repeats:
        if spec.screen_id == sid:
            out = _repeat(rec, session, surface, snapshot, spec)
            if out is not None:
                # A stop, or rows were just added and filled: the main loop
                # takes a fresh snapshot (and re-checks them) before going on.
                return out
    for hub in gui.hubs:
        if hub.screen_id == sid:
            return _hub(rec, session, surface, snapshot, hub)
    blank = _blank_choice_halt(rec, session, snapshot)
    if blank is not None:
        return blank
    for step_spec in gui.steps:
        if step_spec.screen_id != sid:
            continue
        role = StepRole.NAVIGATE if step_spec.role == "navigate" else StepRole.IN_PLACE
        unbound = _unbound_halt(rec, session, snapshot)
        if unbound is not None:
            return unbound
        taken = take_step(
            SurfaceActuator(surface, session.pack),
            StepSpec(f"{sid}:step", step_spec.control.spec(dialog=False), role),
            run_id=session.run_id,
        )
        if not taken.held:
            return _halt(
                rec,
                session,
                snapshot,
                f"step on {sid!r}: {taken.result.reason}",
                sig=f"step|{sid}",
                safe_to_retry=not taken.clicked,
            )
        return CONTINUE
    return None


def before_commit(
    rec: Any,
    session: Any,
    surface: Any,
    snapshot: FormSnapshot,
    screen: WizardScreenSpec,
) -> Optional[FormTxResult]:
    gui: Optional[GuiMechanismsSpec] = session.pack.gui
    if gui is None:
        return None
    blank = _blank_choice_halt(rec, session, snapshot)
    if blank is not None:
        return blank
    echo = _review_echo(rec, session, surface, snapshot)
    if echo is not None:
        return echo
    specs = [a for a in gui.attestations if a.screen_id == screen.screen_id]
    if not specs:
        return None
    act = SnapshotActuator(surface, session.pack)
    observed = act.observe()
    bound = {
        m.dom_selector
        for b in session.pack.bindings
        for m in [_match(b.anchor, snapshot, session)]
        if m is not None
    }
    plan = plan_checkboxes(
        observed,
        attestations=[
            AttestationSpec(
                a.attestation_id, a.control.spec(), a.statement_sha256, a.statement_from
            )
            for a in specs
        ],
        grants=getattr(session, "attestation_grants", ()),
        bound=bound,
        run_id=session.run_id,
    )
    if plan.stops:
        return _halt(
            rec,
            session,
            snapshot,
            f"attestation {plan.stops[0].name!r}: {plan.stops[0].reason}",
            sig=f"attest|{screen.screen_id}",
        )
    after = apply_attestations(act, plan)
    verdict = judge_screen(plan, after)
    if verdict.status is not Status.HELD:
        return _halt(
            rec,
            session,
            snapshot,
            "an attestation box does not show the planned state after ticking",
            sig=f"attest_check|{screen.screen_id}",
        )
    for box in plan.ticks:
        session._filled_selectors.add(box.handle)
    return None


#: For each label pattern, the visible elements whose own text matches it
#: whole, and the text of the element right after each one: a review row
#: drawn as a label beside its value.
_LABEL_VALUE_JS = r"""(patterns) => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden';
  const out = {};
  for (const p of patterns) {
    const re = new RegExp(p, 'i');
    const hits = [];
    for (const el of document.body.querySelectorAll('*')) {
      if (el.children.length || !shown(el) || !re.test(clean(el.textContent))) continue;
      const next = el.nextElementSibling;
      hits.push(next && shown(next) ? clean(next.textContent) : null);
    }
    out[p] = hits;
  }
  return out;
}"""


def _review_echo(
    rec: Any, session: Any, surface: Any, snapshot: FormSnapshot
) -> Optional[FormTxResult]:
    """Read the review rows the snapshot could not (a label element beside
    its value element) and stop before the commit on any that differ from
    the intake. A row read here is checked; one still not found stays
    recorded as not shown."""

    check = session.pack.summary_check
    page = getattr(surface, "_page", None)
    if check is None or page is None:
        return None
    todo = [
        rb
        for rb in check.review_readbacks
        if session._review_skipped.get(rb.dossier_key) == "not shown on the review"
        and len(rb.anchor.label_patterns) == 1
    ]
    if not todo:
        return None
    from openadapt_flow.form_tx.judge import _text_pair_matches
    from openadapt_flow.form_tx.types import ObservedTextPair

    found = page.evaluate(_LABEL_VALUE_JS, [rb.anchor.label_patterns[0] for rb in todo])
    for rb in todo:
        hits = found.get(rb.anchor.label_patterns[0]) or []
        fact = session.effective_facts.get(rb.dossier_key)
        if len(hits) != 1 or hits[0] is None or fact is None:
            continue  # not one readable row: it stays "not shown"
        pair = ObservedTextPair(label=rb.anchor.label_patterns[0], value=hits[0])
        if not _text_pair_matches(fact.value, pair, rb):
            return _halt(
                rec,
                session,
                snapshot,
                f"The review shows {hits[0]!r} for {rb.dossier_key!r}, which differs "
                "from the intake",
                sig=f"review_echo|{rb.dossier_key}",
                kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
            )
        session._review_skipped.pop(rb.dossier_key, None)
    return None


def _unbound_halt(
    rec: Any, session: Any, snapshot: FormSnapshot
) -> Optional[FormTxResult]:
    """The reconciler's unbound-control scan, for a screen this module is
    about to leave (the main loop's own scan is skipped when it continues)."""

    halt = rec._scan_unbound_controls(session, snapshot)
    if halt is None:
        return None
    return rec._build_result(
        session,
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        safe_to_retry=True,
        halts=(halt,),
        what_happened=halt.message,
    )


def _match(
    anchor: Any, snapshot: FormSnapshot, session: Any
) -> Optional[ObservedControl]:
    try:
        return match_anchor_in_snapshot(
            anchor, snapshot, include_readonly=True, facts=session.effective_facts
        )
    except AmbiguousAnchorError:
        return None


def is_form_dialog(session: Any, snapshot: FormSnapshot, modal: ObservedModal) -> bool:
    """True when the pack opts in and the modal holds an editable control:
    such a dialog is a form and is never confirmed through a dossier flag."""

    if getattr(session.pack, "gui", None) is None:
        return False
    return any(
        c.visible
        and not c.disabled
        and not c.readonly
        and c.modal_container == modal.container_selector
        and c.tag.casefold() in ("input", "select", "textarea")
        and c.input_type.casefold()
        not in ("submit", "button", "reset", "image", "hidden")
        for c in snapshot.controls
    )
