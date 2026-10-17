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

How strong this is. The check reads the screen of the same application in
the same session that did the save (``docs/EFFECT_KIT.md``):

- With a re-navigation of at least
  :data:`~openadapt_flow.runtime.effects.effect.MIN_RENAV_ACTIONS` actions, it
  is a persisted-state re-acquisition (Flow verification tier 3). Re-opening
  the record catches a save that only the form showed. It does not catch a
  page served from a cache the write filled, a duplicate submit, or a lost
  update by another writer.
- With a shorter navigation (the person marked fields on the screen the save
  left), it re-reads the save's own screen (tier 4): a consistency signal only.
- It is never a system-of-record read (tier 1) and never an independent
  session (tier 2, ``ui_readback``). Under ``problems_only`` approval a save
  checked this way still goes to a person (``onscreen_readback``).

Zero model calls at runtime: OCR of fixed regions and replayed input.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.effects.effect import (
    MIN_RENAV_ACTIONS,
    Effect,
    EffectKind,
    ReadbackField,
    ReadbackNav,
    ReadbackSpec,
    ValueExpr,
)

__all__ = [
    "CHECK_FILE",
    "CHECK_PROMPT",
    "CheckError",
    "CheckMarks",
    "CheckOutcome",
    "MarkedField",
    "apply_check",
    "load_check",
    "write_check",
]

CHECK_FILE = "check.json"
CHECK_PROMPT = "Show how to check the save"


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


class CheckMarks(BaseModel):
    """``check.json``: where the check starts and what was marked."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    #: Index of the first recorded event of the check (events before it are
    #: the task).
    start_event: int
    fields: list[MarkedField] = Field(default_factory=list)


class CheckOutcome(BaseModel):
    """What :func:`apply_check` compiled."""

    save_step: str
    removed_steps: list[str]
    fields: list[str]
    different_path: bool
    tier: str


def write_check(
    recording_dir: Path | str,
    *,
    start_event: int,
    fields: list[MarkedField],
) -> Path:
    """Store the check's marks beside the recording."""
    marks = CheckMarks(start_event=start_event, fields=fields)
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
    if step.action is ActionKind.KEY and step.key:
        return ReadbackNav(action="key", key=step.key)
    raise CheckError(
        f"the check's step {step.id} ({step.action.value}) can't be replayed as "
        "a read-back navigation. Use clicks, typing and keys"
    )


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
    different_path = len(renavigation) >= MIN_RENAV_ACTIONS

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
        )
        for field in marks.fields
    ]
    tier = "persisted_state_reacquisition" if different_path else "immediate_screen"
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field=marks.fields[0].param,
        value=ValueExpr(param=marks.fields[0].param),
        risk=save.risk,
        readback=ReadbackSpec(
            region=marks.fields[0].region,
            different_path=different_path,
            renavigation=renavigation if different_path else [],
            fields=read_fields,
        ),
        probe=(
            f"UI read-back shown by the person: {len(renavigation)} recorded "
            f"navigation step(s), then read {len(read_fields)} marked field(s) "
            "off the screen. Same application and session as the save; not a "
            "system-of-record read."
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
    )
    return updated, outcome


def outcome_json(outcome: CheckOutcome) -> str:
    return json.dumps(outcome.model_dump(), indent=2, sort_keys=True)
