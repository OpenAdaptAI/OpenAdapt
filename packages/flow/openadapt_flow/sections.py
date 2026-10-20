"""Named sections of a compiled program, found without a model.

A compiled program is a flat list of steps. People read a form in parts:
"Name", "Contact", "Create New Patient". This module groups the steps into
contiguous, ordered, named sections that every compiled bundle carries, so a
program map (Desktop's review screen, the shared ``@openadapt/program-graph``
package) can fold them.

Sections are **display metadata**. The replayer, the program interpreter and
the outcome classifier never read them, and they are not part of the bundle's
content digest or its workflow contract hash (see :data:`SECTIONS_FILE` and
``docs/SECTIONS.md``). Renaming a section never changes what a program does,
so it must not invalidate a certification or a governed authorization.

Boundaries come from, in priority order:

(a) the consequential write. A save section runs from the step after the last
    input up to and including the write's confirmation (consecutive writes,
    and READ / WAIT steps that confirm them). It is always its own section.
(b) the recorded UI structure. The browser recorder stores, for each target,
    the nearest fieldset legend, accordion or tab header, dialog, landmark or
    heading (``structural.section``). A step that opens a section header (a
    tab, an accordion toggle) starts a section titled by that header. Bundles
    recorded before that capture still carry the header click itself: a named
    tab or button clicked between fields starts a section.
(c) the parameter each step fills. The steps on one field (the focusing
    click, Ctrl+A, the typed value, the Enter that picks an option) stay
    together. A section with more than :data:`MAX_FIELDS` fields splits where
    the kind of field changes: name, birth, address, phone and email (the
    WHATWG autofill field groups).
(d) the narration, when the recording has it. A sentence the person said
    titles an untitled section, and splits a long one that (c) could not.

Titles come from the interface's own text. When none is found, the title
describes the fields: "3 fields: title, first name, last name".

A person can rename, merge or split sections during review (:func:`apply_edit`).
The edits are stored in the bundle beside the compiled sections and are
replayed if the program is recompiled or changed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openadapt_flow.ir import Step, Workflow

SECTIONS_FILE = "sections.json"
SECTIONS_SCHEMA: Final = "openadapt.flow.sections/v1"

#: A section with more fields than this splits where the kind of field changes.
MAX_FIELDS = 4

#: Longest title kept from the interface or a narration.
MAX_TITLE = 80

ContextKind = Literal["legend", "accordion", "tab", "dialog", "landmark", "heading"]
TitleSource = Literal["ui", "narration", "fields", "person"]
SectionKind = Literal["fields", "save", "steps"]

NOTE = (
    "Display metadata: named groups of steps for program maps. Execution and "
    "the outcome classifier ignore this file, and it is outside the content "
    "digest and the workflow contract hash."
)


class SectionsError(ValueError):
    """A section edit that would break the section rules."""


class StepContext(BaseModel):
    """The recorded UI structure around one step's target."""

    model_config = ConfigDict(extra="forbid")

    container: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Text of the nearest legend, header, dialog, landmark or heading",
    )
    kind: Optional[ContextKind] = None
    header: bool = Field(
        default=False,
        description="The step clicks a section header (a tab or an accordion toggle)",
    )


class Section(BaseModel):
    """One contiguous, named group of steps."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    title_source: TitleSource
    kind: SectionKind
    steps: list[str] = Field(min_length=1)
    fields: list[str] = Field(
        default_factory=list, description="Readable names of the fields it fills"
    )
    params: list[str] = Field(
        default_factory=list, description="Parameter names it fills, in order"
    )


class SectionEdit(BaseModel):
    """A person's change to the sections, replayed over the compiled ones."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["rename", "merge", "split"]
    section: str
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE)
    other: Optional[str] = Field(
        default=None, description="merge: the adjacent section to fold in"
    )
    at: Optional[str] = Field(
        default=None, description="split: the step that starts the new section"
    )


class ProgramSections(BaseModel):
    """The content of ``sections.json``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["openadapt.flow.sections/v1"] = Field(
        default=SECTIONS_SCHEMA, alias="schema"
    )
    note: str = NOTE
    workflow_contract_sha256: str
    sections: list[Section]
    compiled: list[Section]
    edits: list[SectionEdit] = Field(default_factory=list)
    context: dict[str, StepContext] = Field(default_factory=dict)

    def dump(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True), indent=2) + "\n"


# ---------------------------------------------------------------------------
# Step classification
# ---------------------------------------------------------------------------

_SELECT_ALL = re.compile(r"^(control|ctrl|meta|cmd|command)\+a$", re.IGNORECASE)
_TRAILING_ROLES = frozenset({"option", "menuitem", "listitem", "treeitem"})
_HEADER_ROLES = frozenset({"tab", "button"})


def is_input(step: "Step") -> bool:
    """Whether the step fills a field (typed text or a chosen option)."""
    from openadapt_flow.ir import ActionKind

    return step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)


def is_write(step: "Step") -> bool:
    """Whether the step commits, or confirms, a consequential write."""

    if step.risk == "irreversible":
        return True
    if step.effects:
        return True
    if step.api_binding is not None and step.api_binding.effects:
        return True
    return step.submit_binding is not None


def _confirms_write(step: "Step") -> bool:
    from openadapt_flow.ir import ActionKind

    return step.action in (ActionKind.READ, ActionKind.WAIT)


def _structural(step: "Step") -> Any:
    return step.anchor.structural if step.anchor is not None else None


def _role(step: "Step") -> Optional[str]:
    structural = _structural(step)
    return (structural.role or "").lower() or None if structural else None


def _label(step: "Step") -> Optional[str]:
    """The target's accessible name, else its recorded on-screen text."""
    structural = _structural(step)
    if structural is not None and structural.name:
        return structural.name
    if step.anchor is not None and step.anchor.ocr_text:
        return step.anchor.ocr_text
    return None


def _is_trailing(step: "Step") -> bool:
    """A step that finishes the input before it (Enter, Tab, a picked option)."""
    from openadapt_flow.ir import ActionKind

    if step.action in (ActionKind.KEY, ActionKind.HOTKEY):
        return not _SELECT_ALL.match(_key_text(step))
    if step.action is ActionKind.CLICK:
        return _role(step) in _TRAILING_ROLES
    return False


def _key_text(step: "Step") -> str:
    parts = [*step.modifiers, step.key or ""]
    return "+".join(p for p in parts if p)


def _is_header_click(step: "Step", context: Optional[StepContext]) -> bool:
    from openadapt_flow.ir import ActionKind

    if step.action is not ActionKind.CLICK or is_write(step):
        return False
    if context is not None and context.header:
        return True
    structural = _structural(step)
    return bool(
        structural is not None
        and structural.name
        and (structural.role or "").lower() in _HEADER_ROLES
    )


# ---------------------------------------------------------------------------
# Field names and kinds
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    "fname": "first name",
    "firstname": "first name",
    "mname": "middle name",
    "lname": "last name",
    "lastname": "last name",
    "dob": "date of birth",
    "zip": "zip code",
    "zipcode": "zip code",
    "tel": "phone",
    "addr": "address",
    "no": "number",
    "num": "number",
    "qty": "quantity",
}
_QUALIFIERS = frozenset({"home", "work", "cell", "mobile", "business", "biz"})
_PREFIXES = ("form_", "frm_", "input_", "txt_", "field_")
_DROP_SUFFIXES = ("label", "id")

#: WHATWG autofill field groups, by the words a field's name contains.
_FAMILIES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "contact",
        frozenset({"phone", "mobile", "cell", "telephone", "fax", "email", "e-mail"}),
    ),
    (
        "birth",
        frozenset({"birth", "birthday", "birthdate", "dob", "sex", "gender", "age"}),
    ),
    (
        "address",
        frozenset(
            {
                "street",
                "address",
                "city",
                "town",
                "state",
                "province",
                "region",
                "county",
                "postal",
                "zip",
                "postcode",
                "country",
            }
        ),
    ),
    (
        "name",
        frozenset(
            {
                "title",
                "honorific",
                "prefix",
                "suffix",
                "name",
                "first",
                "given",
                "middle",
                "last",
                "family",
                "surname",
                "nickname",
            }
        ),
    ),
)


def _words(raw: str) -> list[str]:
    text = raw.strip()
    lowered = text.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
            break
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return [w for w in re.split(r"[\s_\-.:#]+", text) if w]


def humanize_field(raw: str) -> str:
    """A readable field name: ``form_phone_home`` -> ``home phone``."""

    words = _words(raw)
    if len(words) > 1 and words[-1].lower() in _DROP_SUFFIXES:
        words = words[:-1]
    out: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in _ABBREVIATIONS:
            out.extend(_ABBREVIATIONS[lowered].split())
        elif word.isupper() and len(word) > 1:
            out.append(word)
        else:
            out.append(lowered)
    if len(out) == 2 and out[1] in _QUALIFIERS:
        out = [out[1], out[0]]
    if out == ["country", "code"]:  # forms label it "Country"
        out = ["country"]
    return " ".join(out) or raw


def _readable_label(label: str) -> bool:
    """A recorded field label people would recognise (not ``form_title``)."""
    return " " in label.strip() or not re.search(r"[_#.]", label)


def field_name(step: "Step") -> Optional[str]:
    """The readable name of the field an input step fills."""

    if not is_input(step):
        return None
    if step.field_label and _readable_label(step.field_label):
        return humanize_field(step.field_label)
    if step.param:
        return humanize_field(step.param)
    if step.field_label:
        return humanize_field(step.field_label)
    return "text"


def field_family(name: Optional[str]) -> Optional[str]:
    """The WHATWG autofill group a field name belongs to, if any."""

    if not name:
        return None
    words = {w.lower() for w in re.split(r"[\s_\-]+", name)}
    for family, vocabulary in _FAMILIES:
        if words & vocabulary:
            return family
    return None


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------


def _example_values(workflow: "Workflow") -> list[str]:
    values = [str(v) for v in workflow.params.values() if v is not None]
    for spec in workflow.param_specs.values():
        if spec.example is not None:
            values.append(str(spec.example))
    return [v.casefold() for v in values if len(v.strip()) >= 3]


def clean_title(text: Optional[str], workflow: "Workflow") -> Optional[str]:
    """Interface text fit to title a section, or None.

    Refuses text that holds a recorded value (a heading such as "Patient:
    Jane Doe") or a run of four digits (a date, a record number): a title
    names a part of the form, never a record.
    """

    if not text:
        return None
    title = re.sub(r"\s+", " ", text).strip().rstrip(":").strip()
    if not title or len(title) > MAX_TITLE:
        return None
    if re.search(r"\d{4}", title):
        return None
    folded = title.casefold()
    if any(value in folded for value in _example_values(workflow)):
        return None
    return title


def describe_fields(fields: Sequence[str]) -> str:
    """The fallback title: ``3 fields: title, first name, last name``."""
    count = len(fields)
    return f"{count} {'field' if count == 1 else 'fields'}: {', '.join(fields)}"


def describe_steps(count: int) -> str:
    return f"{count} {'step' if count == 1 else 'steps'}"


# ---------------------------------------------------------------------------
# The sectioner
# ---------------------------------------------------------------------------


def step_runs(workflow: "Workflow") -> list[list["Step"]]:
    """The straight-line step sequences a section may span.

    A linear bundle is one run. In a program, each graph (the top level and
    every subflow, a loop body included) is walked from its entry; a branch,
    loop, call or terminal ends a run. A section never crosses a run.
    """
    from openadapt_flow.ir import StateKind

    if workflow.program is None:
        return [list(workflow.steps)] if workflow.steps else []
    runs: list[list[Step]] = []
    for graph in (workflow.program, *workflow.subflows.values()):
        order: list[str] = []
        seen: set[str] = set()
        stack = [graph.entry]
        while stack:
            sid = stack.pop()
            if sid in seen or sid not in graph.states:
                continue
            seen.add(sid)
            order.append(sid)
            state = graph.states[sid]
            targets = [t.target for t in state.transitions]
            if state.on_exception:
                targets.append(state.on_exception)
            stack.extend(reversed(targets))
        order.extend(sid for sid in graph.states if sid not in seen)
        current: list[Step] = []
        previous = None
        for sid in order:
            state = graph.states[sid]
            linked = (
                previous is not None
                and len(previous.transitions) == 1
                and previous.transitions[0].guard is None
                and previous.transitions[0].target == sid
            )
            if state.kind is StateKind.ACTION and state.step is not None:
                if current and not linked:
                    runs.append(current)
                    current = []
                current.append(state.step)
                previous = state
            else:
                if current:
                    runs.append(current)
                current = []
                previous = None
        if current:
            runs.append(current)
    return runs


@dataclass
class _Unit:
    """The steps on one field: leading steps, the input, trailing steps."""

    steps: list[int]
    input: Optional[int] = None
    header: Optional[str] = None
    container: Optional[str] = None
    narration: Optional[str] = None


@dataclass
class _Draft:
    units: list[_Unit] = field(default_factory=list)
    title: Optional[str] = None
    source: TitleSource = "fields"
    container: Optional[str] = None


def _save_spans(run: Sequence["Step"]) -> list[tuple[int, int]]:
    """(start, end) indexes of each save section in one run."""

    spans: list[tuple[int, int]] = []
    n = len(run)
    for w, step in enumerate(run):
        if not is_write(step) or (spans and w <= spans[-1][1]):
            continue
        floor = spans[-1][1] + 1 if spans else 0
        start = floor
        last_input = next(
            (j for j in range(w - 1, floor - 1, -1) if is_input(run[j])), None
        )
        if last_input is not None:
            start = last_input + 1
            while start < w and _is_trailing(run[start]):
                start += 1
        end = w
        while end + 1 < n and (is_write(run[end + 1]) or _confirms_write(run[end + 1])):
            end += 1
        spans.append((start, end))
    return spans


def _units(
    run: Sequence["Step"],
    indexes: Sequence[int],
    context: dict[str, StepContext],
    workflow: "Workflow",
    narration: dict[str, list[str]],
) -> list[_Unit]:
    units: list[_Unit] = []
    lead: list[int] = []
    i = 0
    while i < len(indexes):
        k = indexes[i]
        if not is_input(run[k]):
            lead.append(k)
            i += 1
            continue
        members = [*lead, k]
        i += 1
        while i < len(indexes) and _is_trailing(run[indexes[i]]):
            members.append(indexes[i])
            i += 1
        units.append(_Unit(steps=members, input=k))
        lead = []
    if lead:
        units.append(_Unit(steps=lead))
    for unit in units:
        for k in unit.steps:
            step = run[k]
            ctx = context.get(step.id)
            if unit.input is not None and k != unit.input and k > unit.input:
                break
            if _is_header_click(step, ctx):
                title = (ctx.container if ctx and ctx.header else None) or _label(step)
                unit.header = clean_title(title, workflow) or unit.header
        anchor_ctx = context.get(run[unit.input].id) if unit.input is not None else None
        if anchor_ctx is None or not anchor_ctx.container:
            anchor_ctx = next(
                (
                    context[run[k].id]
                    for k in unit.steps
                    if run[k].id in context
                    and context[run[k].id].container
                    and not context[run[k].id].header
                ),
                None,
            )
        if anchor_ctx is not None:
            unit.container = clean_title(anchor_ctx.container, workflow)
        for k in unit.steps:
            lines = narration.get(run[k].id)
            if lines:
                unit.narration = clean_title(lines[0], workflow)
                break
    return _merge_same_field(run, units)


def _merge_same_field(run: Sequence["Step"], units: list[_Unit]) -> list[_Unit]:
    """(c): consecutive units that fill the same field stay together."""

    merged: list[_Unit] = []
    for unit in units:
        if (
            merged
            and unit.input is not None
            and merged[-1].input is not None
            and not unit.header
            and _field_key(run[unit.input]) == _field_key(run[merged[-1].input])
        ):
            merged[-1].steps.extend(unit.steps)
            continue
        merged.append(unit)
    return merged


def _field_key(step: "Step") -> tuple[Optional[str], Optional[str]]:
    structural = _structural(step)
    return (step.param or step.field_label, structural.selector if structural else None)


def _group(units: list[_Unit]) -> list[_Draft]:
    """(b): start a section at a header click or where the container changes."""

    drafts: list[_Draft] = []
    for unit in units:
        current = drafts[-1] if drafts else None
        starts = current is None or unit.header is not None
        if (
            not starts
            and current is not None
            and unit.container is not None
            and unit.container != current.container
        ):
            starts = True
        if starts:
            current = _Draft(container=unit.container)
            if unit.header:
                current.title, current.source = unit.header, "ui"
            elif unit.container:
                current.title, current.source = unit.container, "ui"
            drafts.append(current)
        assert current is not None
        if current.container is None and unit.container is not None:
            current.container = unit.container
        current.units.append(unit)
    return drafts


def _fields_of(run: Sequence["Step"], units: Iterable[_Unit]) -> list[str]:
    return [
        name
        for unit in units
        if unit.input is not None
        for name in [field_name(run[unit.input])]
        if name
    ]


def _split_long(run: Sequence["Step"], draft: _Draft) -> list[_Draft]:
    """(c)/(d): split a section with too many fields."""

    if len(_fields_of(run, draft.units)) <= MAX_FIELDS:
        return [draft]
    groups = _split_by_family(run, draft.units)
    if len(groups) == 1:
        groups = _split_by_narration(draft.units)
    if len(groups) == 1:
        return [draft]
    out: list[_Draft] = []
    for units in groups:
        fields = _fields_of(run, units)
        chunk = _Draft(units=units, container=draft.container)
        if draft.title and draft.source in ("ui", "person"):
            chunk.title = f"{draft.title}: {', '.join(fields)}"
            chunk.source = draft.source
        out.append(chunk)
    return out


def _split_by_family(run: Sequence["Step"], units: list[_Unit]) -> list[list[_Unit]]:
    runs: list[tuple[Optional[str], list[_Unit]]] = []
    pending: list[_Unit] = []
    for unit in units:
        if unit.input is None:
            pending.append(unit)
            continue
        family = field_family(field_name(run[unit.input])) or "other"
        if runs and runs[-1][0] == family:
            runs[-1][1].extend([*pending, unit])
        else:
            runs.append((family, [*pending, unit]))
        pending = []
    if pending:
        if runs:
            runs[-1][1].extend(pending)
        else:
            return [units]

    def size(group: list[_Unit]) -> int:
        return sum(1 for u in group if u.input is not None)

    groups = [g for _, g in runs]
    changed = True
    while changed and len(groups) > 1:
        changed = False
        for idx, group in enumerate(groups):
            if size(group) == 1:
                if idx == 0:
                    groups[1] = group + groups[1]
                else:
                    groups[idx - 1] = groups[idx - 1] + group
                del groups[idx]
                changed = True
                break
    return groups


def _split_by_narration(units: list[_Unit]) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    for unit in units:
        if not groups or (unit.narration and unit.input is not None):
            groups.append([unit])
        else:
            groups[-1].append(unit)
    return groups


def _section_from_draft(
    run: Sequence["Step"], draft: _Draft, workflow: "Workflow"
) -> Section:
    indexes = sorted(k for unit in draft.units for k in unit.steps)
    steps = [run[k] for k in indexes]
    fields = _fields_of(run, draft.units)
    params = [run[u.input].param for u in draft.units if u.input is not None]
    title, source = draft.title, draft.source
    if not title and draft.units and draft.units[0].narration:
        title, source = draft.units[0].narration, "narration"
    if not title:
        if fields:
            title, source = describe_fields(fields), "fields"
        else:
            label = next(
                (
                    t
                    for s in steps
                    if _label(s)
                    for t in [clean_title(_label(s), workflow)]
                    if t
                ),
                None,
            )
            if label:
                title, source = label, "ui"
            else:
                title, source = describe_steps(len(steps)), "fields"
    return Section(
        id=f"{'fields' if fields else 'steps'}-{steps[0].id}",
        title=title,
        title_source=source,
        kind="fields" if fields else "steps",
        steps=[s.id for s in steps],
        fields=fields,
        params=[p for p in params if p],
    )


def _save_section(
    run: Sequence["Step"], start: int, end: int, workflow: "Workflow"
) -> Section:
    steps = list(run[start : end + 1])
    writes = [s for s in steps if is_write(s)]
    candidates = [*writes, *steps]
    title: Optional[str] = None
    for step in candidates:
        structural = _structural(step)
        for text in (
            structural.name if structural is not None else None,
            step.anchor.ocr_text if step.anchor is not None else None,
        ):
            title = clean_title(text, workflow)
            if title:
                break
        if title:
            break
    return Section(
        id=f"save-{steps[0].id}",
        title=title or "Save",
        title_source="ui" if title else "fields",
        kind="save",
        steps=[s.id for s in steps],
    )


def compute_sections(
    workflow: "Workflow",
    *,
    context: Optional[dict[str, StepContext]] = None,
    narration: Optional[dict[str, list[str]]] = None,
) -> list[Section]:
    """Group every step of ``workflow`` into named sections. Deterministic."""

    context = context or {}
    narration = narration or {}
    sections: list[Section] = []
    for run in step_runs(workflow):
        spans = _save_spans(run)
        cursor = 0
        for start, end in [*spans, (len(run), len(run) - 1)]:
            region = list(range(cursor, start))
            if region:
                units = _units(run, region, context, workflow, narration)
                for draft in _group(units):
                    for chunk in _split_long(run, draft):
                        sections.append(_section_from_draft(run, chunk, workflow))
            if start < len(run):
                sections.append(_save_section(run, start, end, workflow))
            cursor = end + 1
    return sections


# ---------------------------------------------------------------------------
# Validation and review edits
# ---------------------------------------------------------------------------


def validate_sections(workflow: "Workflow", sections: Sequence[Section]) -> list[str]:
    """Every rule a set of sections must keep; an empty list means valid.

    Sections are contiguous and in order within one run, cover every step
    once, and a save section holds the writes and no input.
    """

    problems: list[str] = []
    position: dict[str, tuple[int, int]] = {}
    steps: dict[str, Step] = {}
    for r, run in enumerate(step_runs(workflow)):
        for i, step in enumerate(run):
            position[step.id] = (r, i)
            steps[step.id] = step
    seen: set[str] = set()
    ids: set[str] = set()
    previous: Optional[tuple[int, int]] = None
    for section in sections:
        if section.id in ids:
            problems.append(f"section id {section.id!r} is used twice")
        ids.add(section.id)
        unknown = [s for s in section.steps if s not in position]
        if unknown:
            problems.append(f"{section.id}: unknown steps {unknown}")
            continue
        places = [position[s] for s in section.steps]
        run_ids = {r for r, _ in places}
        if len(run_ids) != 1:
            problems.append(f"{section.id}: spans more than one straight run")
        elif [i for _, i in places] != list(
            range(places[0][1], places[0][1] + len(places))
        ):
            problems.append(f"{section.id}: steps are not contiguous and in order")
        if previous is not None and places[0] <= previous:
            problems.append(f"{section.id}: out of order")
        previous = places[-1]
        dup = seen.intersection(section.steps)
        if dup:
            problems.append(f"{section.id}: steps in two sections {sorted(dup)}")
        seen.update(section.steps)
        members = [steps[s] for s in section.steps]
        if section.kind == "save":
            if not any(is_write(s) for s in members):
                problems.append(f"{section.id}: a save section without a write")
            if any(is_input(s) for s in members):
                problems.append(f"{section.id}: a save section that fills a field")
        elif any(is_write(s) for s in members):
            problems.append(f"{section.id}: a write outside a save section")
    missing = [s for s in position if s not in seen]
    if missing:
        problems.append(f"steps in no section: {missing}")
    return problems


def _index(sections: list[Section], section_id: str) -> int:
    for i, section in enumerate(sections):
        if section.id == section_id:
            return i
    raise SectionsError(f"no section {section_id!r}")


def _derived_prefix(section: Section) -> Optional[str]:
    """``Contact`` for a title ``Contact: street, city`` built from its fields."""
    suffix = ": " + ", ".join(section.fields)
    if section.fields and section.title.endswith(suffix):
        return section.title[: -len(suffix)]
    return None


def _rebuilt(
    workflow: "Workflow", step_ids: list[str], like: Section, *, keep_title: bool
) -> Section:
    """A section over ``step_ids`` that takes ``like``'s title where it still fits.

    A title made from the fields follows the new fields; an interface,
    narration or person title stays on the part that keeps ``like``'s first
    step (``keep_title``).
    """
    by_id = {s.id: s for run in step_runs(workflow) for s in run}
    members = [by_id[s] for s in step_ids]
    inputs = [s for s in members if is_input(s)]
    fields = [n for s in inputs for n in [field_name(s)] if n]
    kind: SectionKind = (
        "save" if like.kind == "save" else ("fields" if fields else "steps")
    )
    prefix = _derived_prefix(like)
    if prefix is not None and fields:
        title, source = f"{prefix}: {', '.join(fields)}", like.title_source
    elif keep_title and like.title_source != "fields":
        title, source = like.title, like.title_source
    elif fields:
        title, source = describe_fields(fields), "fields"
    elif keep_title:
        title, source = like.title, like.title_source
    else:
        title, source = describe_steps(len(members)), "fields"
    return Section(
        id=f"{kind}-{step_ids[0]}",
        title=title,
        title_source=source,
        kind=kind,
        steps=step_ids,
        fields=fields,
        params=[s.param for s in inputs if s.param],
    )


def apply_edit(
    workflow: "Workflow", sections: Sequence[Section], edit: SectionEdit
) -> list[Section]:
    """Apply one rename, merge or split; refuse one that breaks a rule."""

    out = [s.model_copy(deep=True) for s in sections]
    i = _index(out, edit.section)
    target = out[i]
    if edit.op == "rename":
        title = re.sub(r"\s+", " ", edit.title or "").strip()
        if not title:
            raise SectionsError("a section title cannot be empty")
        out[i] = target.model_copy(update={"title": title, "title_source": "person"})
    elif edit.op == "merge":
        if not edit.other:
            raise SectionsError("merge needs the section to fold in")
        j = _index(out, edit.other)
        a, b = sorted((i, j))
        if b != a + 1:
            raise SectionsError("only adjacent sections can be merged")
        first, second = out[a], out[b]
        if (first.kind == "save") != (second.kind == "save"):
            raise SectionsError("the save section stays separate from field sections")
        merged = _rebuilt(
            workflow, [*first.steps, *second.steps], first, keep_title=True
        )
        out[a : b + 1] = [merged]
    elif edit.op == "split":
        if target.kind == "save":
            raise SectionsError("the save section is not split")
        if not edit.at or edit.at not in target.steps:
            raise SectionsError(f"step {edit.at!r} is not in {target.id!r}")
        cut = target.steps.index(edit.at)
        if cut == 0:
            raise SectionsError("a split needs at least one step on each side")
        head = _rebuilt(workflow, target.steps[:cut], target, keep_title=True)
        tail = _rebuilt(workflow, target.steps[cut:], target, keep_title=False)
        out[i : i + 1] = [head, tail]
    problems = validate_sections(workflow, out)
    if problems:
        raise SectionsError("; ".join(problems))
    return out


def apply_edits(
    workflow: "Workflow",
    sections: Sequence[Section],
    edits: Sequence[SectionEdit],
    *,
    strict: bool = True,
) -> tuple[list[Section], list[SectionEdit]]:
    """Replay ``edits``; with ``strict=False`` skip ones that no longer apply."""

    current = list(sections)
    kept: list[SectionEdit] = []
    for edit in edits:
        try:
            current = apply_edit(workflow, current, edit)
        except SectionsError:
            if strict:
                raise
            continue
        kept.append(edit)
    return current, kept


# ---------------------------------------------------------------------------
# Recording inputs
# ---------------------------------------------------------------------------

_CONTEXT_KINDS = frozenset(ContextKind.__args__)  # type: ignore[attr-defined]


def context_from_events(events: Iterable[dict[str, Any]]) -> dict[str, StepContext]:
    """Each step's recorded UI structure (the event's ``section``), by step id.

    Both browser recorders write it (:mod:`openadapt_flow.section_capture`).
    """

    from openadapt_flow.narration import step_id_for_event

    out: dict[str, StepContext] = {}
    for event in events:
        raw = event.get("section")
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        kind = raw.get("kind")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            index = int(event["i"])
        except (KeyError, TypeError, ValueError):
            continue
        out[step_id_for_event(index)] = StepContext(
            container=re.sub(r"\s+", " ", text).strip()[:200],
            kind=kind if kind in _CONTEXT_KINDS else None,
            header=raw.get("header") is True,
        )
    return out


def context_for_workflow(
    events: Iterable[dict[str, Any]], workflow: "Workflow"
) -> dict[str, StepContext]:
    """The recorded UI structure of the steps ``workflow`` keeps.

    A step takes an event's context only when the event targeted the same
    element (the same selector, or role and name). That holds by
    construction when compiling, and makes it safe to read the context of
    an existing bundle from a recording of the same demonstration made with
    a recorder that captures it.
    """

    events = list(events)
    by_index = {}
    for event in events:
        try:
            by_index[int(event["i"])] = event
        except (KeyError, TypeError, ValueError):
            continue
    steps = {s.id: s for run in step_runs(workflow) for s in run}
    out: dict[str, StepContext] = {}
    for step_id, ctx in context_from_events(events).items():
        step = steps.get(step_id)
        if step is None:
            continue
        structural = _structural(step)
        recorded = by_index.get(int(step_id.rsplit("_", 1)[1]), {}).get("structural")
        if structural is not None and (structural.selector or structural.name):
            if not isinstance(recorded, dict):
                continue
            same = (
                structural.selector == recorded.get("selector")
                if structural.selector
                else (structural.role, structural.name)
                == (recorded.get("role"), recorded.get("name"))
            )
            if not same:
                continue
        out[step_id] = ctx
    return out


def narration_by_step(recording_dir: Path | str) -> dict[str, list[str]]:
    """The scrubbed sentences said before each step, when the recording has them."""

    from openadapt_flow.narration import NarrationRefused, load_narration

    try:
        narration = load_narration(recording_dir)
    except (NarrationRefused, OSError, ValueError):
        return {}
    if narration is None:
        return {}
    out: dict[str, list[str]] = {}
    for line in narration.lines:
        if line.step_id:
            out.setdefault(line.step_id, []).append(line.text)
    return out


# ---------------------------------------------------------------------------
# The bundle file
# ---------------------------------------------------------------------------


def contract_sha256(workflow: "Workflow") -> str:
    from openadapt_flow.qualification import workflow_contract_sha256

    return workflow_contract_sha256(workflow)


def build(
    workflow: "Workflow",
    *,
    context: Optional[dict[str, StepContext]] = None,
    narration: Optional[dict[str, list[str]]] = None,
    edits: Sequence[SectionEdit] = (),
) -> ProgramSections:
    """Compute the sections and replay any edits that still apply."""

    known = {s.id for run in step_runs(workflow) for s in run}
    context = {k: v for k, v in (context or {}).items() if k in known}
    compiled = compute_sections(workflow, context=context, narration=narration)
    problems = validate_sections(workflow, compiled)
    if problems:  # pragma: no cover - the sectioner keeps every rule
        raise SectionsError("; ".join(problems))
    current, kept = apply_edits(workflow, compiled, edits, strict=False)
    return ProgramSections(
        workflow_contract_sha256=contract_sha256(workflow),
        sections=current,
        compiled=compiled,
        edits=kept,
        context=context,
    )


def read_file(bundle_dir: Path | str) -> Optional[ProgramSections]:
    """The stored ``sections.json``, or None when absent or unreadable."""

    from openadapt_flow import sealed_set

    path = Path(bundle_dir) / SECTIONS_FILE
    if not sealed_set.is_file(path):
        return None
    try:
        return ProgramSections.model_validate_json(sealed_set.read_text(path))
    except ValueError:
        return None


def load(bundle_dir: Path | str, workflow: "Workflow") -> ProgramSections:
    """The sections to show for ``workflow``.

    The stored file when it was made for this exact program and keeps every
    rule; otherwise sections recomputed from the program, the stored UI
    context and the stored edits. Never raises for a missing or stale file.
    """

    stored = read_file(bundle_dir)
    if (
        stored is not None
        and stored.workflow_contract_sha256 == contract_sha256(workflow)
        and not validate_sections(workflow, stored.sections)
    ):
        return stored
    return build(
        workflow,
        context=stored.context if stored else None,
        edits=stored.edits if stored else (),
    )


def write(
    bundle_dir: Path | str, doc: ProgramSections, *, key: Optional[str] = None
) -> Path:
    from openadapt_flow import sealed_set

    path = Path(bundle_dir) / SECTIONS_FILE
    sealed_set.write_text(path, doc.dump(), key=key)
    return path


def edit(
    bundle_dir: Path | str,
    workflow: "Workflow",
    edits: Sequence[SectionEdit],
    *,
    key: Optional[str] = None,
) -> ProgramSections:
    """Apply a person's edits to the bundle's sections and store them."""

    doc = load(bundle_dir, workflow)
    current, _ = apply_edits(workflow, doc.sections, edits, strict=True)
    updated = doc.model_copy(
        update={"sections": current, "edits": [*doc.edits, *edits]}
    )
    write(bundle_dir, updated, key=key)
    return updated


def reset(
    bundle_dir: Path | str, workflow: "Workflow", *, key: Optional[str] = None
) -> ProgramSections:
    """Drop a person's edits and keep the compiled sections."""

    doc = load(bundle_dir, workflow)
    updated = build(workflow, context=doc.context)
    write(bundle_dir, updated, key=key)
    return updated
