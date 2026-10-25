"""Turn shadow-mined episodes into a CANDIDATE program, never a runnable one.

OpenAdapt Capture's shadow mode (``openadapt_capture.shadow``, off by default)
can notice that a person repeated the same cross-application sequence several
times and, if the person clicks **Review draft**, export those episodes as
``openadapt.shadow.episodes/v2`` JSON. Each step in that export is a
value-free symbol: an application, a control role, references local to the
export in place of labels and typed values (``label-a``, ``value-b``), a
scrubbed URL pattern. There are no screenshots, no coordinates, no plain-text
values, and no keyed digests: two steps that share a reference shared a value,
and that is all the export says about values. Version 1 exports, which carried
keyed digests, are refused, so a digest can never reach a draft.

This module converts the episodes into linear :class:`~openadapt_flow.ir.Workflow`
traces and hands them to the EXISTING multi-trace induction
(:func:`openadapt_flow.compiler.induction.induce_program`), which infers
parameters, loops, and branches or refuses. The result is a
:class:`MotifCandidate`:

* ``status`` is always ``"candidate"``, ``admitted`` and ``runnable`` are
  always False. Nothing here can run a step, and nothing here writes a
  ``workflow.json`` bundle.
* The draft's anchors are placeholders (shadow mining keeps no pixels), so
  the record-next worklist always starts with "record this task once" through
  the normal record, compile, induce, and qualify path. That path is where a
  person admits the program.
* Write-shaped steps (a Submit-like control, a click right before a form
  submit, a save chord) are marked irreversible and ``risk_review_required``,
  so certification refuses them until a person reviews them.
* A verification check is PROPOSED where the episodes show one (a file
  saved, a form submitted, a row pasted into a spreadsheet). A proposal is
  never an effect contract: the episodes that suggested it can't approve it.

Deterministic and model-free; no network access.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.compiler.induction import RecordNext, TraceInput, induce_program
from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator, Workflow

EPISODES_SCHEMA = "openadapt.shadow.episodes/v2"
REFUSED_SCHEMAS = frozenset({"openadapt.shadow.episodes/v1"})
CANDIDATE_SCHEMA: Literal["openadapt.shadow.candidate/v1"] = (
    "openadapt.shadow.candidate/v1"
)
PLACEHOLDER_TEMPLATE = "shadow-unrecorded.png"
DRAFT_FILENAME = "draft-workflow.json"
CANDIDATE_FILENAME = "candidate.json"

_DIGEST = r"^[0-9a-f]{16}$"
_TOKEN_DIGEST = re.compile(r"^[0-9a-f]{8,}$")
_LABEL_REF = r"^label-[a-z]{1,4}$"
_VALUE_REF = r"^value-[a-z]{1,4}$"
#: Capture's placeholder for a chrome label that looked like data.
DYNAMIC_LABEL = "<dynamic_label>"
_WRITE_STEMS = ("save", "submit", "send", "post", "approve", "confirm", "create", "add")
_SHEET_APPS = ("excel", "numbers", "sheets", "calc", "spreadsheet")
_SAVE_MODIFIERS = frozenset({"meta", "ctrl", "cmd", "command"})

ADMISSION_PATH: tuple[str, ...] = (
    "Record the task with OpenAdapt Capture (flow record or capture record).",
    "Run flow admit --candidate <motif_id>: it compiles, induces and proposes "
    "checks, then stops once for a person (or a delegated agent approver) to "
    "accept or reject them.",
    "Qualify the accepted bundle (flow qualify).",
)


class ShadowStep(BaseModel):
    """One value-free symbol from the Capture export."""

    model_config = ConfigDict(extra="forbid")

    t: float
    token: str = Field(max_length=256)
    kind: Literal[
        "focus",
        "click",
        "double_click",
        "right_click",
        "type",
        "key",
        "shortcut",
        "copy",
        "paste",
        "navigate",
        "submit",
    ]
    app: Optional[str] = Field(default=None, max_length=128)
    window_class: Optional[str] = Field(default=None, max_length=128)
    role: Optional[str] = Field(default=None, max_length=64)
    label_ref: Optional[str] = Field(default=None, pattern=_LABEL_REF)
    display_label: Optional[str] = Field(
        default=None, max_length=32, pattern=r"^[^0-9@]*$"
    )
    value_ref: Optional[str] = Field(default=None, pattern=_VALUE_REF)
    value_shape: Optional[
        Literal["empty", "digits", "decimal", "date", "email", "alpha", "alnum", "text"]
    ] = None
    url_pattern: Optional[str] = Field(default=None, max_length=256)
    key: Optional[str] = Field(default=None, max_length=32)
    modifiers: list[str] = Field(default_factory=list, max_length=4)
    secure: bool = False

    @field_validator("token")
    @classmethod
    def _no_digest_in_token(cls, value: str) -> str:
        if any(_TOKEN_DIGEST.match(part) for part in value.split("|")):
            raise ValueError("a shadow token must not carry a keyed digest")
        return value


def _check_tokens(tokens: list[str]) -> list[str]:
    for token in tokens:
        ShadowStep._no_digest_in_token(token)
    return tokens


class ShadowEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_start: float
    t_end: float
    exact: bool
    similarity: float = Field(ge=0.0, le=1.0)
    steps: list[ShadowStep] = Field(min_length=1)


class ShadowEpisodesExport(BaseModel):
    """The ``openadapt.shadow.episodes/v2`` document Capture writes."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["openadapt.shadow.episodes/v2"] = Field(alias="schema")
    motif_id: str = Field(pattern=_DIGEST)
    tokens: list[str]

    @field_validator("tokens")
    @classmethod
    def _tokens_carry_no_digest(cls, value: list[str]) -> list[str]:
        return _check_tokens(value)

    apps: list[str]
    count_in_window: int = Field(ge=0)
    window_s: float
    exact_count: int = Field(ge=0)
    expected_by_chance: float
    p_value: float
    status: Literal["candidate"]
    privacy: dict[str, str]
    episodes: list[ShadowEpisode] = Field(min_length=1)


class ProposedCheck(BaseModel):
    """A verification check the episodes suggest. A proposal, nothing more."""

    kind: Literal["file_written", "record_written", "row_added", "page_reached"]
    summary: str
    step_id: Optional[str] = None
    support: int = Field(description="Episodes in which the signal appeared.")
    status: Literal["proposed"] = "proposed"
    admitted: Literal[False] = False
    note: str = (
        "Proposal only. The episodes that suggested this check can't approve "
        "it; a person confirms or rejects it during qualification."
    )


class MotifCandidate(BaseModel):
    """What a person reviews after clicking **Review draft**."""

    schema_version: Literal["openadapt.shadow.candidate/v1"] = CANDIDATE_SCHEMA
    motif_id: str
    status: Literal["candidate"] = "candidate"
    admitted: Literal[False] = False
    runnable: Literal[False] = False
    apps: list[str]
    n_episodes: int
    count_in_window: int
    induction_certified: bool = Field(
        description=(
            "Whether induction found the episodes structurally consistent. "
            "Structural only: it does not admit, qualify, or certify a run."
        )
    )
    induction_report: str
    draft: Optional[Workflow] = None
    record_next: list[RecordNext] = Field(default_factory=list)
    proposed_checks: list[ProposedCheck] = Field(default_factory=list)
    admission_path: list[str] = Field(default_factory=lambda: list(ADMISSION_PATH))


def _alias(ref: Optional[str]) -> str:
    """Name of a control known only by its export reference."""

    if not ref:
        return "unnamed"
    return "fld-" + ref.split("-", 1)[1]


def _shown(display_label: Optional[str]) -> Optional[str]:
    """A plain-text chrome label, or None for none or a scrubbed one."""

    if not display_label or display_label == DYNAMIC_LABEL:
        return None
    return display_label


def _placeholder_anchor(role: Optional[str], name: str, *, varies: bool) -> Anchor:
    """An anchor with no pixels. Shadow mining keeps no screenshots.

    A control whose identity varies per run (a list row, a table cell) gets a
    structural role, so induction reads it as a which-entity selection. A
    fixed control (a named button or field) is identified by its label, so
    two different buttons in a row never collapse into one "button" column.
    """

    if varies:
        return Anchor(
            template=PLACEHOLDER_TEMPLATE,
            region=(0, 0, 1, 1),
            click_point=(0, 0),
            structural=StructuralLocator(role=role or None, name=name),
        )
    return Anchor(
        template=PLACEHOLDER_TEMPLATE,
        region=(0, 0, 1, 1),
        click_point=(0, 0),
        ocr_text=name,
    )


def _is_write_label(label: Optional[str]) -> bool:
    if not label:
        return False
    low = label.lower()
    return any(re.search(rf"\b{stem}\b", low) for stem in _WRITE_STEMS)


def _is_sheet(app: Optional[str]) -> bool:
    low = (app or "").lower()
    return any(name in low for name in _SHEET_APPS)


def _episode_steps(
    episode: ShadowEpisode,
) -> tuple[list[Step], list[tuple[str, Optional[str], str]]]:
    """Map one episode to IR steps plus the check signals it shows.

    A signal is ``(kind, step_id, summary)``.
    """

    steps: list[Step] = []
    signals: list[tuple[str, Optional[str], str]] = []
    symbols = episode.steps
    for index, sym in enumerate(symbols):
        sid = f"s{index:02d}"
        app = sym.app or "unknown"
        prefix = f"[{app}]"
        nxt = symbols[index + 1] if index + 1 < len(symbols) else None
        if sym.kind in ("click", "double_click", "right_click"):
            action = {
                "click": ActionKind.CLICK,
                "double_click": ActionKind.DOUBLE_CLICK,
                "right_click": ActionKind.RIGHT_CLICK,
            }[sym.kind]
            if sym.value_ref:
                name = sym.value_ref
                what = f"{sym.role or 'control'} (varies)"
            else:
                name = _shown(sym.display_label) or _alias(sym.label_ref)
                what = f"{sym.role or 'control'} {name}"
            writes = _is_write_label(_shown(sym.display_label)) or (
                nxt is not None and nxt.kind == "submit"
            )
            steps.append(
                Step(
                    id=sid,
                    intent=f"{prefix} click {what}",
                    action=action,
                    anchor=_placeholder_anchor(
                        sym.role, name, varies=bool(sym.value_ref)
                    ),
                    risk="irreversible" if writes else "reversible",
                    risk_explanation=(
                        "Shadow-mined write-shaped control; a person must review it."
                        if writes
                        else None
                    ),
                    risk_review_required=writes,
                )
            )
            if writes:
                signals.append(
                    (
                        "record_written",
                        sid,
                        f"A record is created or changed after '{what}' in {app}.",
                    )
                )
        elif sym.kind == "type":
            field = _shown(sym.display_label) or _alias(sym.label_ref)
            intent = f"{prefix} type {sym.role or 'field'} {field}"
            if sym.secure:
                steps.append(
                    Step(
                        id=sid,
                        intent=intent,
                        action=ActionKind.TYPE,
                        secret=True,
                        param=f"secret_{_alias(sym.label_ref)[4:]}",
                    )
                )
            else:
                shape = sym.value_shape or "text"
                steps.append(
                    Step(
                        id=sid,
                        intent=intent,
                        action=ActionKind.TYPE,
                        text=f"<{shape}:{sym.value_ref or 'none'}>",
                    )
                )
            if nxt is not None and nxt.kind == "key" and _is_sheet(app):
                signals.append(("row_added", sid, f"The sheet in {app} gains a row."))
        elif sym.kind in ("copy", "paste", "shortcut"):
            mods = [m for m in sym.modifiers if m]
            key = sym.key or ("c" if sym.kind == "copy" else "v")
            label = {"copy": "copy", "paste": "paste"}.get(sym.kind, "press shortcut")
            if mods:
                steps.append(
                    Step(
                        id=sid,
                        intent=f"{prefix} {label} {'+'.join(mods)}+{key}",
                        action=ActionKind.HOTKEY,
                        key=key,
                        modifiers=mods,
                    )
                )
            else:
                steps.append(
                    Step(
                        id=sid,
                        intent=f"{prefix} press {key}",
                        action=ActionKind.KEY,
                        key=key,
                    )
                )
            if sym.kind == "shortcut" and key == "s" and set(mods) & _SAVE_MODIFIERS:
                signals.append(
                    ("file_written", sid, f"A file saved from {app} changes.")
                )
            if (
                sym.kind == "paste"
                and _is_sheet(app)
                and nxt is not None
                and (nxt.kind == "key")
            ):
                signals.append(("row_added", sid, f"The sheet in {app} gains a row."))
        elif sym.kind == "key":
            steps.append(
                Step(
                    id=sid,
                    intent=f"{prefix} press {sym.key or 'key'}",
                    action=ActionKind.KEY,
                    key=sym.key or "other",
                )
            )
        elif sym.kind == "submit":
            target = sym.url_pattern or app
            signals.append(
                ("page_reached", None, f"After submitting, the page matches {target}.")
            )
        # focus and navigate carry context only; they are not IR actions.
    return steps, signals


def episodes_to_workflows(
    export: ShadowEpisodesExport,
) -> tuple[list[Workflow], list[ProposedCheck]]:
    """Convert episodes to linear traces, and collect proposed checks."""

    workflows: list[Workflow] = []
    support: Counter[tuple[str, str]] = Counter()
    where: dict[tuple[str, str], Counter[str]] = {}
    for number, episode in enumerate(export.episodes):
        steps, signals = _episode_steps(episode)
        workflows.append(
            Workflow(name=f"shadow-episode-{export.motif_id}-{number}", steps=steps)
        )
        seen = {(kind, summary) for kind, _sid, summary in signals}
        support.update(seen)
        for kind, sid, summary in signals:
            if sid is not None:
                where.setdefault((kind, summary), Counter())[sid] += 1
    majority = (len(export.episodes) + 1) // 2
    checks: list[ProposedCheck] = []
    for (kind, summary), count in sorted(support.items()):
        if count < majority:
            continue
        ids = where.get((kind, summary))
        step_id = (
            sorted(ids.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if ids else None
        )
        checks.append(
            ProposedCheck(
                kind=kind,  # type: ignore[arg-type]
                step_id=step_id,
                summary=summary,
                support=count,
            )
        )
    return workflows, checks


def induce_candidate(export: ShadowEpisodesExport | dict) -> MotifCandidate:
    """Run the existing induction over shadow episodes; return a candidate."""

    if not isinstance(export, ShadowEpisodesExport):
        if isinstance(export, dict) and export.get("schema") in REFUSED_SCHEMAS:
            raise ValueError(
                f"{export.get('schema')} carries keyed digests and is refused; "
                f"export again with a Capture that writes {EPISODES_SCHEMA}"
            )
        export = ShadowEpisodesExport.model_validate(export)
    workflows, checks = episodes_to_workflows(export)
    traces: list[TraceInput] = list(workflows)
    result = induce_program(traces)

    record_next: list[RecordNext] = [
        RecordNext(
            code="shadow_full_demonstration",
            instruction=(
                "Record this task once with OpenAdapt Capture. Shadow mining kept "
                "no screenshots or values, so the draft has nothing to act on yet."
            ),
        )
    ]
    record_next.extend(result.record_next)
    for check in checks:
        record_next.append(
            RecordNext(
                code="shadow_confirm_check",
                instruction=f"Confirm or reject the proposed check: {check.summary}",
                location=check.step_id or "",
            )
        )
    for name, spec in sorted(result.param_specs.items()):
        record_next.append(
            RecordNext(
                code="shadow_name_value",
                instruction=(
                    f"Say what the changing value '{name}' is "
                    f"(the draft only knows it varies)."
                ),
                location=name,
                blocking=False,
            )
        )

    draft = None
    if result.workflow is not None:
        draft = result.workflow.model_copy(
            update={"name": f"shadow-draft-{export.motif_id}"}
        )
    return MotifCandidate(
        motif_id=export.motif_id,
        apps=list(export.apps),
        n_episodes=len(export.episodes),
        count_in_window=export.count_in_window,
        induction_certified=result.certified,
        induction_report=result.render(),
        draft=draft,
        record_next=record_next,
        proposed_checks=checks,
    )


def write_candidate(candidate: MotifCandidate, out_dir: Path) -> Path:
    """Write ``candidate.json`` and, when there is a draft, ``draft-workflow.json``.

    Never writes ``workflow.json``, so no Flow command treats the directory
    as a runnable bundle. Refuses a directory that already holds one.
    """

    out = Path(out_dir)
    if (out / "workflow.json").exists() or (out / "workflow.json.enc").exists():
        raise FileExistsError(
            f"{out} holds a workflow bundle; refusing to mix a candidate in"
        )
    out.mkdir(parents=True, exist_ok=True)
    meta = candidate.model_dump(mode="json", exclude={"draft"})
    meta["draft_file"] = DRAFT_FILENAME if candidate.draft is not None else None
    (out / CANDIDATE_FILENAME).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if candidate.draft is not None:
        (out / DRAFT_FILENAME).write_text(
            candidate.draft.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    return out
