"""Author a UI read-back by demonstration: "Show how to check the save".

For an application with no API or database to read, the record check has to
go through the application's own screens. At the end of a recording, the
person is asked to show how they would check the save. They navigate to the
saved record (that part is recorded like any other step) and mark the fields
that show what they typed. :func:`apply_check` compiles that into an
``onscreen`` read-back effect on the save step:

- the navigation becomes the read-back's re-navigation, replayed after the
  save. A search box the person typed a run value into (for example the
  patient's name) is replayed with this run's value;
- each marked field becomes a region to read, compared against the run
  parameter whose value the person typed into the form. All fields must read
  back for the check to pass;
- the navigation steps are removed from the task itself: they verify the
  save, they are not part of it.

The marks are stored next to the recording as ``check.json``
(:func:`write_check`). Compiling checks them against the demonstration: every
marked region must show the value typed for its parameter on the screen where
it was marked, or compiling refuses.

State reset. Before it reads, the check must throw away what the client
remembers: reload the page, run a new search, close and reopen the window, or
start a fresh session. Otherwise it could read the form buffer or a
client-side page the save left behind, and a save that never reached the
record would pass. :func:`detect_reset` recognizes a reload key, a closed and
reopened window, and a typed and submitted search in the check's steps. The
person can also name the step in ``check.json`` (``reset``); the compiled
check records whether the step was detected or only declared. A check with no
reset is refused.

Admission. The compiled check is marked ``demonstrated``, records who drove
the demonstration (``demonstrated_by``), and has no admission, so it confirms
nothing until a person admits it (:func:`admit_check`, ``openadapt-flow check
admit``). The command needs an interactive terminal and a typed word. An
admission in an agent, model or service name is refused, and the verifier
ignores one that got into the bundle anyway. These are identity checks: code
that can call :func:`admit_check` with a person's name can admit.

How strong this is. The check reads the screen of the same application in
the same session that did the save (``docs/EFFECT_KIT.md``):

- Every compiled check has a state reset, so it is a persisted-state
  re-acquisition (Flow verification tier 3). Re-opening the record catches a
  save that only the form showed. It does not catch a page served from a
  cache the write filled, a duplicate submit, or a lost update by another
  writer.
- Fields marked on the screen the save left, with no reset before them, are
  refused. Before the reset rule they compiled to a re-read of the save's own
  screen (tier 4). That read can still be configured by hand
  (``effects.kind: onscreen``), not taught.
- It is never a system-of-record read (tier 1) and never an independent
  session (tier 2, ``ui_readback``). Its assurance grade is
  ``screen_readback`` (:mod:`openadapt_flow.assurance`). Under
  ``problems_only`` approval a save checked this way still goes to a person
  (``onscreen_readback``).

Zero model calls at runtime: OCR of fixed regions and replayed input.
"""

from __future__ import annotations

import getpass
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ReadbackAdmission,
    ReadbackField,
    ReadbackNav,
    ReadbackReset,
    ReadbackResetKind,
    ReadbackSpec,
    ValueExpr,
    is_agent_identity,
)
from openadapt_flow.runtime.effects.readback_values import ValueKind, infer_value_kind

__all__ = [
    "CHECK_FILE",
    "CHECK_PROMPT",
    "CheckError",
    "CheckMarks",
    "CheckOutcome",
    "DeclaredReset",
    "MarkedField",
    "admit_check",
    "apply_check",
    "demonstrated_checks",
    "describe_check",
    "detect_reset",
    "load_check",
    "write_check",
]

CHECK_FILE = "check.json"
CHECK_PROMPT = "Now show how you'd check it saved."

#: Keys that reload the page or screen.
RELOAD_CHORDS = frozenset(
    {
        "f5",
        "ctrl+f5",
        "ctrl+r",
        "control+r",
        "ctrl+shift+r",
        "control+shift+r",
        "meta+r",
        "cmd+r",
        "command+r",
        "meta+shift+r",
        "cmd+shift+r",
    }
)
#: Keys that close a window or tab.
CLOSE_CHORDS = frozenset(
    {
        "alt+f4",
        "ctrl+f4",
        "ctrl+w",
        "control+w",
        "meta+w",
        "cmd+w",
        "command+w",
        "ctrl+shift+w",
        "meta+q",
        "cmd+q",
    }
)


class CheckError(ValueError):
    """The demonstrated check can't be compiled. Nothing was changed."""


class MarkedField(BaseModel):
    """A field the person marked on the saved record."""

    model_config = ConfigDict(extra="forbid")

    #: The run parameter whose typed value this field must show.
    param: str
    #: ``(x, y, w, h)`` in frame pixels.
    region: tuple[int, int, int, int]
    #: DOM selector or accessibility path of the marked element, if known.
    locator: Optional[str] = None
    #: True when, at marking time, the element's text read from the page
    #: structure equaled the value typed for :attr:`param`. The field is then
    #: read from the structure at runtime instead of by OCR.
    structure_matched: bool = False
    #: How to compare the field: ``text``, ``code``, ``date`` or ``currency``.
    #: Unset, the compiler infers it from the typed value.
    value_kind: Optional[ValueKind] = None
    #: ``date`` only: ``dmy`` or ``mdy`` for a numeric date.
    date_order: Optional[Literal["dmy", "mdy"]] = None


class DeclaredReset(BaseModel):
    """The person's word for which recorded event resets client state."""

    model_config = ConfigDict(extra="forbid")

    kind: ReadbackResetKind
    #: Recorded event index of the boundary (it must be part of the check).
    event: int = Field(ge=0)


class CheckMarks(BaseModel):
    """``check.json``: where the check starts and what was marked."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    #: Index of the first recorded event of the check (events before it are
    #: the task).
    start_event: int
    fields: list[MarkedField] = Field(default_factory=list)
    #: The step the person names as the state reset, when the compiler can't
    #: recognize it from the recording (for example a click on a browser's
    #: reload button, or signing out and back in).
    reset: Optional[DeclaredReset] = None
    #: The prompt the person was shown before the check was recorded.
    prompt: Optional[str] = None


class CheckOutcome(BaseModel):
    """What :func:`apply_check` compiled."""

    save_step: str
    removed_steps: list[str]
    fields: list[str]
    different_path: bool
    tier: str
    reset: Optional[str] = None
    reset_evidence: Optional[str] = None
    #: Always True: a person must admit the check before it can confirm.
    needs_admission: bool = True


def write_check(
    recording_dir: Path | str,
    *,
    start_event: int,
    fields: list[MarkedField],
    reset: Optional[DeclaredReset] = None,
    prompt: Optional[str] = None,
) -> Path:
    """Store the check's marks beside the recording."""
    marks = CheckMarks(
        start_event=start_event, fields=fields, reset=reset, prompt=prompt
    )
    path = Path(recording_dir) / CHECK_FILE
    path.write_text(marks.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_check(recording_dir: Path | str) -> Optional[CheckMarks]:
    path = Path(recording_dir) / CHECK_FILE
    if not path.is_file():
        return None
    return CheckMarks.model_validate_json(path.read_text(encoding="utf-8"))


def _event_index(step: Step) -> Optional[int]:
    """Compiled step ids are positional: ``step_007`` is event 7."""
    prefix, _, number = step.id.rpartition("_")
    if prefix != "step" or not number.isdigit():
        return None
    return int(number)


def _as_nav(step: Step, workflow: Workflow) -> ReadbackNav:
    if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        if step.anchor is None:
            raise CheckError(f"the check's click {step.id} has no recorded target")
        x, y = step.anchor.click_point
        return ReadbackNav(action="click", point=(int(x), int(y)))
    if step.action is ActionKind.TYPE:
        if step.secret or (step.param and step.param in workflow.secret_params):
            raise CheckError(
                f"the check types a secret in {step.id}. A check can't replay a "
                "secret; search by another value"
            )
        if step.param:
            return ReadbackNav(action="type", param=step.param)
        if step.text:
            return ReadbackNav(action="type", text=step.text)
        raise CheckError(f"the check's typing step {step.id} has no text")
    if step.action in (ActionKind.KEY, ActionKind.HOTKEY) and step.key:
        return ReadbackNav(action="key", key=_chord(step))
    raise CheckError(
        f"the check's step {step.id} ({step.action.value}) can't be replayed as "
        "a read-back navigation. Use clicks, typing and keys"
    )


def _chord(step: Step) -> str:
    """The key as replayed, with its modifiers (``ctrl+r``)."""
    if step.modifiers and step.key:
        return "+".join([*step.modifiers, step.key])
    return step.key or ""


def _reset_kind_at(steps: list[Step], index: int) -> Optional[ReadbackResetKind]:
    """The reset ``steps[index]`` performs, recognized from the recording."""
    step = steps[index]
    if step.action in (ActionKind.KEY, ActionKind.HOTKEY) and step.key:
        chord = _chord(step).lower().replace(" ", "")
        if chord in RELOAD_CHORDS:
            return "reload"
        if chord in CLOSE_CHORDS and index + 1 < len(steps):
            return "reopen_window"
    if step.action is ActionKind.TYPE and (step.param or step.text):
        submitted = any(
            later.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            or (
                later.action in (ActionKind.KEY, ActionKind.HOTKEY)
                and (later.key or "").lower() in ("enter", "return")
            )
            for later in steps[index + 1 :]
        )
        if submitted:
            return "new_search"
    return None


def detect_reset(steps: list[Step]) -> Optional[ReadbackReset]:
    """The first state-busting step among the check's ``steps``, if any.

    Recognized: a reload key (F5, Ctrl/Cmd+R), a window closed by key and
    then reopened by a later step, and a search typed and then submitted (by
    Enter or a click). A fresh sign-in can't be recognized: the check can't
    replay a password. A person declares it instead.
    """
    for index in range(len(steps)):
        kind = _reset_kind_at(steps, index)
        if kind is not None:
            return ReadbackReset(kind=kind, at=index, evidence="detected")
    return None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _shows(region_text: str, value: str) -> bool:
    from openadapt_flow.runtime.effects.onscreen import value_matches

    return value_matches(_norm(region_text), value)


def _marked_frame(recording_dir: Path, marks: CheckMarks, n_events: int) -> Path:
    """The screen the person marked: after the check's last event, or the
    task's last screen when the check had no navigation."""
    frames = recording_dir / "frames"
    last = n_events - 1
    candidates = [frames / f"{last:04d}_after.png", frames / f"{last:04d}_before.png"]
    for path in candidates:
        if path.is_file():
            return path
    raise CheckError(
        "the recording has no frame of the screen the fields were marked on"
    )


def _count_events(recording_dir: Path) -> int:
    lines = (recording_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if line.strip())


def apply_check(
    workflow: Workflow,
    recording_dir: Path | str,
    *,
    vision: Any = None,
) -> tuple[Workflow, CheckOutcome]:
    """Compile the demonstrated check into the save step's read-back effect.

    Returns the updated workflow (the caller saves it) and a summary. Raises
    :class:`CheckError`, changing nothing, when the check can't be compiled.
    """
    recording = Path(recording_dir)
    marks = load_check(recording)
    if marks is None:
        raise CheckError(f"no {CHECK_FILE} in {recording}")
    if not marks.fields:
        raise CheckError("no fields were marked, so there is nothing to check")
    if workflow.program is not None:
        raise CheckError("a demonstrated check applies to a linear recording only")

    task: list[Step] = []
    check: list[Step] = []
    for step in workflow.steps:
        index = _event_index(step)
        if index is None:
            raise CheckError(f"step {step.id} has no recorded position")
        (check if index >= marks.start_event else task).append(step)

    saves = [s for s in task if s.risk == "irreversible" or s.effects]
    if not saves:
        raise CheckError(
            "nothing before the check saves. Show the check after the save"
        )
    save = saves[-1]

    renavigation = [_as_nav(step, workflow) for step in check]
    reset = _reset(check, marks)
    # The reset re-fetches the record, so the read takes a different path
    # from the save even when the reset is the check's only step.
    different_path = True

    params = dict(workflow.params)
    seen: set[str] = set()
    for field in marks.fields:
        if field.param in seen:
            raise CheckError(f"{field.param!r} is marked twice")
        seen.add(field.param)
        if field.param in workflow.secret_params:
            raise CheckError(f"{field.param!r} is secret and can't be read back")
        if not params.get(field.param):
            raise CheckError(
                f"{field.param!r} isn't a value typed in the task, so there is "
                "nothing to compare the field with"
            )
        if field.region[2] <= 0 or field.region[3] <= 0:
            raise CheckError(f"the box marked for {field.param!r} is empty")

    if vision is None:
        from openadapt_flow import vision as vision_module

        vision = vision_module
    png = (
        _marked_frame(recording, marks, _count_events(recording)).read_bytes()
        if not all(f.structure_matched for f in marks.fields)
        else b""
    )
    for field in marks.fields:
        if field.structure_matched:
            continue  # checked against the live page when it was marked
        lines = vision.ocr(png, region=field.region)
        text = " ".join(line.text for line in lines)
        if not _shows(text, str(params[field.param])):
            raise CheckError(
                f"the box marked for {field.param!r} doesn't show the value "
                "typed for it. Mark the field that shows it"
            )

    read_fields = [
        ReadbackField(
            name=field.param,
            region=field.region,
            value=ValueExpr(param=field.param),
            locator=field.locator,
            source="structure" if field.structure_matched else "ocr",
            value_kind=field.value_kind or infer_value_kind(str(params[field.param])),
            date_order=field.date_order,
        )
        for field in marks.fields
    ]
    tier = "persisted_state_reacquisition"
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field=marks.fields[0].param,
        value=ValueExpr(param=marks.fields[0].param),
        risk=save.risk,
        readback=ReadbackSpec(
            region=marks.fields[0].region,
            different_path=different_path,
            renavigation=renavigation,
            fields=read_fields,
            demonstrated=True,
            demonstrated_by=_demonstrated_by(recording),
            reset=reset,
        ),
        probe=(
            f"UI read-back shown by demonstration: {len(renavigation)} recorded "
            f"navigation step(s) with a {reset.kind.replace('_', ' ')} "
            f"({reset.evidence}), then read {len(read_fields)} marked field(s) "
            "off the screen. Same application and session as the save; not a "
            "system-of-record read. Needs a person's admission."
        ),
    )
    kept = [
        e
        for e in save.effects
        if not e.needs_operator_confirmation and e.readback is None
    ]
    new_save = save.model_copy(update={"effects": [*kept, effect]})
    steps = [new_save if s.id == save.id else s for s in task]
    updated = workflow.model_copy(update={"steps": steps})
    outcome = CheckOutcome(
        save_step=save.id,
        removed_steps=[s.id for s in check],
        fields=[f.param for f in marks.fields],
        different_path=different_path,
        tier=tier,
        reset=reset.kind,
        reset_evidence=reset.evidence,
    )
    return updated, outcome


def _reset(check: list[Step], marks: CheckMarks) -> ReadbackReset:
    """The check's state reset, or :class:`CheckError` when it shows none."""
    declared = marks.reset
    if declared is not None:
        positions = [_event_index(step) for step in check]
        if declared.event not in positions:
            raise CheckError(
                f"the reset is declared at event {declared.event}, which isn't "
                "a step of the check"
            )
        at = positions.index(declared.event)
        seen = _reset_kind_at(check, at)
        return ReadbackReset(
            kind=declared.kind,
            at=at,
            evidence="detected" if seen == declared.kind else "declared",
        )
    detected = detect_reset(check)
    if detected is None:
        raise CheckError(
            "the check reads the record without first throwing away what the "
            "screen remembers, so it could read the form the save left behind. "
            "Before reading, reload the page, run a new search, close and "
            "reopen the window, or sign in again, and record that step "
            "(or name it as the reset in check.json)"
        )
    return detected


def demonstrated_checks(workflow: Workflow) -> list[tuple[Step, Effect]]:
    """Every save step's read-back taught by demonstration."""
    found: list[tuple[Step, Effect]] = []
    for step in getattr(workflow, "steps", None) or []:
        for effect in getattr(step, "effects", None) or []:
            if effect.readback is not None and effect.readback.demonstrated:
                found.append((step, effect))
    return found


def _demonstrated_by(recording: Path) -> Optional[str]:
    """``demonstrated_by`` from the recording's ``meta.json``, if it says."""

    try:
        meta = json.loads((recording / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("demonstrated_by") if isinstance(meta, dict) else None
    return str(value) if value else None


def describe_check(step: Step, effect: Effect) -> str:
    """What a person is asked to admit, in plain words."""
    spec = effect.readback
    assert spec is not None
    lines = [
        f"Save step {step.id} ({step.intent}) is checked by "
        f"(demonstrated by {spec.demonstrated_by or 'unknown'}):"
    ]
    for i, nav in enumerate(spec.renavigation):
        if nav.action == "click":
            what = f"click at {nav.point}"
        elif nav.action == "type":
            what = (
                f"type this run's {nav.param!r}" if nav.param else f"type {nav.text!r}"
            )
        else:
            what = f"press {nav.key}"
        marker = ""
        if spec.reset is not None and spec.reset.at == i:
            marker = f"   <- state reset: {spec.reset.kind} ({spec.reset.evidence})"
        lines.append(f"  {i + 1}. {what}{marker}")
    for field in spec.fields:
        lines.append(
            f"  then read {field.name!r} ({field.value_kind}, by {field.source}) "
            f"at {tuple(field.region)}"
        )
    lines.append(
        "This is a screen read-back in the session that saved: assurance "
        "screen_readback, never a system-of-record read."
    )
    return "\n".join(lines)


def admit_check(
    workflow: Workflow,
    *,
    admitted_by: Optional[str] = None,
    step_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[Workflow, list[str]]:
    """Record a person's admission of the demonstrated checks.

    Binds each admission to the exact check (``spec_digest``). The caller
    saves the workflow (which re-seals the bundle manifest) and is
    responsible for asking a person: the CLI requires a terminal and a typed
    confirmation. Returns the updated workflow and the admitted step ids.
    """
    who = (admitted_by or getpass.getuser() or "").strip()
    if not who:
        raise CheckError("an admission needs the name of the person admitting it")
    if is_agent_identity(who):
        raise CheckError(
            f"{who!r} is not a person; an agent or model can't admit a check"
        )
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    when = stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    admitted: list[str] = []
    steps = []
    for step in workflow.steps:
        effects = []
        for effect in step.effects:
            spec = effect.readback
            if (
                spec is not None
                and spec.demonstrated
                and (step_id is None or step.id == step_id)
            ):
                if spec.reset is None:
                    raise CheckError(
                        f"the check on {step.id} has no state reset and can't "
                        "be admitted"
                    )
                clean = spec.model_copy(update={"admission": None})
                admission = ReadbackAdmission(
                    admitted_by=who,
                    admitted_at=when,
                    spec_digest=clean.admission_digest(),
                )
                effect = effect.model_copy(
                    update={
                        "readback": clean.model_copy(update={"admission": admission})
                    }
                )
                admitted.append(step.id)
            effects.append(effect)
        steps.append(step.model_copy(update={"effects": effects}))
    if not admitted:
        raise CheckError(
            "no demonstrated check to admit" + (f" on {step_id}" if step_id else "")
        )
    return workflow.model_copy(update={"steps": steps}), admitted


def outcome_json(outcome: CheckOutcome) -> str:
    return json.dumps(outcome.model_dump(), indent=2, sort_keys=True)
