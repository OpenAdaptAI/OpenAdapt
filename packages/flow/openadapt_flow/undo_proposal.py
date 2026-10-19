"""Propose an undo for each saving step and verify it on a test copy.

A declared undo (``after_stop``) needs a person to write or record it. Most
business apps already have an inverse path: create and delete, add a line and
remove it, set a field and set it back. This module finds candidates for that
path and keeps only the ones a test copy shows to work
(``docs/AFTER_A_STOP.md``, "Proposed undo steps").

1. **Propose.** For each saving step, :func:`find_candidates` looks in three
   places:

   - ``recording``: other compiled recordings that delete, cancel or remove
     something (a person doing the undo once);
   - ``network``: the recording's captured traffic (``network.jsonl``). A
     DELETE the recording sent to the record the save created, or, for a JSON
     ``POST`` that returned an id, a ``DELETE`` to that record's URL. A
     ``PUT``/``PATCH`` save gets the same request with the old values;
   - ``field_restore``: for a field edit, the same steps again with the
     value read back before the change.

   A control on the page after the save whose label is Delete, Cancel, Void
   or Remove is reported (``app_ui``) but is not turned into steps: Flow
   builds an undo from a control only from a recording of a person using it.

2. **Verify on a test copy only.** :func:`verify_candidate` resets the copy,
   reads every record, runs the save, runs the candidate undo, and reads every
   record again. The candidate passes only when the undo itself ran and was
   confirmed by the record read-back, and the records after the undo are the
   records from before the save: the record the save touched and every other
   record (the collateral guard). The evidence holds digests and counts,
   never record values.

3. **Approval stays.** :func:`write_proposals` writes each verified undo into
   the bundle as a proposal. Flow offers it after a stop only after a person
   approves it once (:func:`accept_proposal`, ``undo accept``), and each run
   of it still needs that person's approval at that time (``undo approve``).
   A candidate that fails is reported with the reason and never written.

Nothing here runs against a target the deployment file doesn't declare a
test copy (``compiler.probing.probe_target_refusals``).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import tempfile
import urllib.parse as up
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

from openadapt_flow.after_stop import (
    UndoRefused,
    judge_undo_report,
    resolve_undo_params,
    save_undo_steps,
    steps_from_demonstration,
    undo_workflow,
)
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    RunReport,
    Step,
    UndoLocate,
    UndoProposal,
    UndoRestore,
    UndoStep,
    Workflow,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

REPORT_SCHEMA = "openadapt.undo-propose-report/v1"
EVIDENCE_SCHEMA = "openadapt.undo-verification/v1"
EVIDENCE_DIRNAME = "undo-evidence"
LOCATE_PARAM = "undo_record_id"

CandidateSource = Literal["recording", "network", "field_restore", "app_ui"]

#: Words that name an inverse action in a recording name, a step intent or a
#: control label.
INVERSE_WORDS = re.compile(
    r"\b(delete|remove|cancel|void|discard|undo|revert|withdraw|retract|"
    r"deactivate|unlink)\b",
    re.I,
)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Report models (no record values anywhere)
# ---------------------------------------------------------------------------


class CandidateResult(BaseModel):
    candidate_id: str
    source: CandidateSource
    basis: str
    #: ``verified``: passed on the test copy and written as a proposal.
    #: ``failed``: ran on the test copy and did not pass. ``not_run``: could
    #: not be built or run, or an earlier candidate for the step passed.
    status: Literal["verified", "failed", "not_run"]
    reason: str
    evidence: Optional[dict[str, Any]] = None


class SavingStepResult(BaseModel):
    step_id: str
    intent: str
    #: The proposal written for the step, if a candidate verified.
    verified_undo: Optional[str] = None
    #: Why the step has no verified undo (empty when it has one).
    reason: str = ""
    #: What would give the step a verified undo (empty when it has one).
    to_close_the_gap: str = ""
    candidates: list[CandidateResult] = Field(default_factory=list)


class ProposeReport(BaseModel):
    schema_: str = Field(default=REPORT_SCHEMA, alias="schema")
    workflow_name: str
    test_copy: Optional[str] = None
    written: bool = False
    saving_steps: list[SavingStepResult] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def verified_count(self) -> int:
        return sum(1 for s in self.saving_steps if s.verified_undo)

    def summary(self) -> dict[str, int]:
        return {
            "saving_steps": len(self.saving_steps),
            "verified_undo": self.verified_count,
            "no_verified_undo": len(self.saving_steps) - self.verified_count,
        }


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One possible undo of one saving step."""

    candidate_id: str
    undoes: str
    source: CandidateSource
    basis: str
    #: The undo to verify. None when it could not be built (``problem``).
    undo: Optional[UndoStep] = None
    #: Bundle-relative path -> source file, for crops a recording brings.
    assets: dict[str, Path] = field(default_factory=dict)
    problem: Optional[str] = None


def saving_steps(workflow: Workflow) -> list[tuple[int, Step]]:
    """The straight-line workflow's consequential steps, with their index."""
    from openadapt_flow.run_gate import is_consequential

    return [
        (i, step)
        for i, step in enumerate(workflow.steps)
        if is_consequential(step, workflow, require_current_risk_certification=False)
    ]


def record_effect(step: Step) -> Optional[Effect]:
    """The save's checkable record effect: its first bound ``record_written``
    or ``field_equals`` effect (the API binding's when the step has none)."""
    declared = list(step.effects) or (
        list(step.api_binding.effects) if step.api_binding is not None else []
    )
    for effect in declared:
        if effect.needs_operator_confirmation:
            continue
        if effect.kind in (EffectKind.RECORD_WRITTEN, EffectKind.FIELD_EQUALS):
            if effect.kind is EffectKind.RECORD_WRITTEN and effect.expected_count < 1:
                continue
            return effect
    return None


def _match_dump(effect: Effect) -> dict[str, Any]:
    return {k: v.model_dump(mode="json") for k, v in effect.match.items()}


def _prior_param(field_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", field_name)
    return f"prior_{safe}"[:64]


def _inverse_effect(step: Step, effect: Effect) -> tuple[Effect, dict[str, Any]]:
    """What the system of record must show after an undo of ``effect``, and
    the ``locate``/``restore`` the undo needs to say it."""
    if effect.kind is EffectKind.FIELD_EQUALS:
        assert effect.field is not None
        param = _prior_param(effect.field)
        inverse = Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=effect.match,
            field=effect.field,
            value=ValueExpr(param=param),
            risk="irreversible",
            probe=f"the field step {step.id} changed holds its old value again",
            timeout_s=effect.timeout_s,
        )
        restore = UndoRestore(match=_match_dump(effect), fields={effect.field: param})
        return inverse, {"restore": restore}
    id_field = effect.identity_field or "id"
    inverse = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={id_field: ValueExpr(param=LOCATE_PARAM)},
        expected_count=0,
        risk="irreversible",
        probe=f"the record step {step.id} created is gone",
        timeout_s=effect.timeout_s,
    )
    locate = UndoLocate(match=_match_dump(effect), field=id_field, param=LOCATE_PARAM)
    return inverse, {"locate": locate}


def _undo_id(step: Step, source: str, n: int) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", step.id)[:40] or "step"
    return f"undo-{base}-{source.replace('_', '-')}-{n}"


# -- recordings ---------------------------------------------------------------


def _looks_inverse(workflow: Workflow) -> bool:
    texts = [workflow.name, *(s.intent for s in workflow.steps)]
    return any(INVERSE_WORDS.search(t or "") for t in texts)


def recording_candidates(
    step: Step, effect: Effect, bundles: Sequence[Path]
) -> list[Candidate]:
    """Candidates from compiled recordings of a person deleting or cancelling.

    A recording qualifies when its name or a step intent names an inverse
    action. Its steps are used as recorded; when none declares an effect, the
    inverse of the save's effect is added to its last step.
    """
    out: list[Candidate] = []
    for path in bundles:
        try:
            demo = Workflow.load(path)
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            out.append(
                Candidate(
                    candidate_id=f"{step.id}:recording:{Path(path).name}",
                    undoes=step.id,
                    source="recording",
                    basis=f"recording {Path(path).name}",
                    problem=f"could not load it ({type(exc).__name__})",
                )
            )
            continue
        if not _looks_inverse(demo):
            continue
        undo_id = _undo_id(step, "recording", len(out) + 1)
        basis = (
            f"recording {Path(path).name!r} ({demo.name}) deletes or cancels a record"
        )
        try:
            demonstrated = steps_from_demonstration(path, undo_id)
        except ValueError as exc:
            out.append(
                Candidate(
                    candidate_id=undo_id,
                    undoes=step.id,
                    source="recording",
                    basis=basis,
                    problem=str(exc),
                )
            )
            continue
        steps = list(demonstrated.steps)
        inverse, extra = _inverse_effect(step, effect)
        if not any(s.effects for s in steps):
            steps[-1] = steps[-1].model_copy(
                update={"effects": [*steps[-1].effects, inverse]}
            )
        out.append(
            Candidate(
                candidate_id=undo_id,
                undoes=step.id,
                source="recording",
                basis=basis,
                undo=UndoStep(
                    id=undo_id,
                    undoes=step.id,
                    intent=f"{demo.name} (undoes step {step.id}: {step.intent})"[:512],
                    source="demonstration",
                    steps=steps,
                    **extra,
                ),
                assets=demonstrated.assets,
            )
        )
    return out


# -- network ------------------------------------------------------------------


def _flat_items(node: Any, out: dict[str, set[str]]) -> dict[str, set[str]]:
    """Leaf ``key -> values`` of a JSON body (nested keys by their own name)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _flat_items(value, out)
            elif value is not None:
                out.setdefault(str(key), set()).add(str(value))
    elif isinstance(node, list):
        for item in node:
            _flat_items(item, out)
    return out


def _selector_values(effect: Effect, params: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, expr in effect.match.items():
        value = expr.resolve(params)
        if value not in (None, ""):
            out[key] = value
    return out


def _request_items(line: dict[str, Any]) -> tuple[Optional[str], dict[str, set[str]]]:
    """``(kind, items)``: ``json``, ``form`` or None, and the request's
    ``key -> values`` (body, form and query)."""
    items: dict[str, set[str]] = {}
    for key, value in up.parse_qsl(up.urlsplit(line.get("url", "")).query):
        items.setdefault(key, set()).add(value)
    if "form" in line:
        for pair in line["form"]:
            if len(pair) == 2:
                items.setdefault(str(pair[0]), set()).add(str(pair[1]))
        return "form", items
    body = line.get("body")
    if isinstance(body, str):
        try:
            return "json", _flat_items(json.loads(body), items)
        except json.JSONDecodeError:
            return None, items
    return None, items


def _explains(items: dict[str, set[str]], wanted: dict[str, str]) -> bool:
    """The request names at least one field of the save's record selector,
    and every selector field it names has the selector's value."""
    named = [key for key in wanted if key in items]
    if named:
        return all(wanted[key] in items[key] for key in named)
    # A form names its fields its own way (``form_lname`` for ``lname``):
    # then every selector value must be among the request's values.
    values = {value for found in items.values() for value in found}
    return bool(wanted) and set(wanted.values()) <= values


def _save_request(
    lines: Sequence[dict[str, Any]], wanted: dict[str, str]
) -> Optional[int]:
    """Index of the last successful write whose request carries the save's
    record selector values."""
    found = None
    for index, line in enumerate(lines):
        if str(line.get("method", "")).upper() not in _WRITE_METHODS:
            continue
        status = line.get("status")
        if not isinstance(status, int) or not 200 <= status < 300:
            continue
        _kind, items = _request_items(line)
        if _explains(items, wanted):
            found = index
    return found


def _response_id(line: dict[str, Any], id_field: str) -> Optional[str]:
    text = line.get("response_text")
    if isinstance(text, str):
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict):
            for key in (id_field, "id"):
                if body.get(key) not in (None, ""):
                    return str(body[key])
            data = body.get("data")
            if isinstance(data, dict) and data.get(id_field) not in (None, ""):
                return str(data[id_field])
    location = line.get("location")
    if isinstance(location, str) and location.rstrip("/"):
        return location.rstrip("/").rsplit("/", 1)[-1]
    return None


def _path(url: str) -> str:
    return up.urlsplit(url).path or "/"


def _api_undo_step(
    undo_id: str,
    step: Step,
    *,
    method: str,
    url_template: str,
    body: dict[str, Any],
    inverse: Effect,
) -> Step:
    return Step(
        id=f"{undo_id}.api",
        intent=f"{method} {url_template} (undo of step {step.id})",
        action=ActionKind.WAIT,
        risk="irreversible",
        effects=[inverse],
        api_binding=ApiBinding(
            kind="rest",
            method=method,
            url_template=url_template,
            body_template=body,
            on_unavailable="halt",
            effects=[inverse],
        ),
    )


def network_candidates(
    step: Step,
    effect: Effect,
    lines: Sequence[dict[str, Any]],
    params: Mapping[str, Any],
) -> tuple[list[Candidate], str]:
    """Candidates from captured traffic, and a note when there are none.

    Only JSON requests become undo steps: a browser form post needs the live
    page's session token, which a plain API call does not have.
    """
    binding = step.api_binding
    if binding is not None and binding.kind == "web_form":
        return [], (
            f"the save is a browser form post ({binding.method} "
            f"{_path(binding.url_template)}); Flow builds an API undo only "
            "from a JSON request"
        )
    if not lines:
        return [], "the recording has no network capture (record --capture-network)"
    wanted = _selector_values(effect, params)
    index = _save_request(lines, wanted)
    if index is None:
        return [], (
            "no captured request carries the save's record values, so the "
            "save's request could not be found"
        )
    save = lines[index]
    method = str(save.get("method", "")).upper()
    kind, _items = _request_items(save)
    path = _path(str(save.get("url", "")))
    if "graphql" in path.lower():
        return [], (
            f"the save is a GraphQL mutation ({path}); Flow doesn't derive an "
            "inverse mutation"
        )
    if kind != "json":
        return [], (
            f"the save is a browser form post ({method} {path}); Flow builds "
            "an API undo only from a JSON request"
        )
    out: list[Candidate] = []
    inverse, extra = _inverse_effect(step, effect)
    if method == "POST" and effect.kind is EffectKind.RECORD_WRITTEN:
        record_id = _response_id(save, effect.identity_field or "id")
        if record_id is None:
            return [], (
                f"the save's response ({method} {path}) carried no record id, "
                "so no record URL can be derived"
            )
        observed = [
            line
            for line in lines[index + 1 :]
            if str(line.get("method", "")).upper() == "DELETE"
            and _path(str(line.get("url", ""))).rstrip("/").endswith("/" + record_id)
        ]
        if observed:
            target = _path(str(observed[0]["url"])).rstrip("/")
            template = target[: -len(record_id)] + "{" + LOCATE_PARAM + "}"
            basis = (
                f"the recording sent DELETE {template} for the record the "
                f"save's POST {path} created"
            )
        else:
            template = path.rstrip("/") + "/{" + LOCATE_PARAM + "}"
            basis = (
                f"the save's JSON POST {path} returned a record id; REST "
                f"convention gives DELETE {template}"
            )
        undo_id = _undo_id(step, "network", 1)
        out.append(
            Candidate(
                candidate_id=undo_id,
                undoes=step.id,
                source="network",
                basis=basis,
                undo=UndoStep(
                    id=undo_id,
                    undoes=step.id,
                    intent=f"delete the record step {step.id} saved ({step.intent})"[
                        :512
                    ],
                    source="synthesized",
                    steps=[
                        _api_undo_step(
                            undo_id,
                            step,
                            method="DELETE",
                            url_template=template,
                            body={},
                            inverse=inverse,
                        )
                    ],
                    **extra,
                ),
            )
        )
        return out, ""
    if method in {"PUT", "PATCH"} and effect.kind is EffectKind.FIELD_EQUALS:
        assert effect.field is not None
        param = _prior_param(effect.field)
        body = {effect.field: "{" + param + "}"}
        undo_id = _undo_id(step, "network", 1)
        out.append(
            Candidate(
                candidate_id=undo_id,
                undoes=step.id,
                source="network",
                basis=(
                    f"the save sent {method} {path}; the undo sends the old "
                    f"value of {effect.field!r} the same way"
                ),
                undo=UndoStep(
                    id=undo_id,
                    undoes=step.id,
                    intent=f"set {effect.field} back to its old value (undoes step "
                    f"{step.id})"[:512],
                    source="synthesized",
                    steps=[
                        _api_undo_step(
                            undo_id,
                            step,
                            method=method,
                            url_template=path,
                            body=body,
                            inverse=inverse,
                        )
                    ],
                    **extra,
                ),
            )
        )
        return out, ""
    return [], (
        f"no inverse is derived for a {method} save with a {effect.kind.value} effect"
    )


_CONTROL = re.compile(
    r"<(button|a)\b[^>]*>(.*?)</\1>|<input\b[^>]*\bvalue\s*=\s*['\"]([^'\"]*)['\"][^>]*>",
    re.I | re.S,
)
_TAGS = re.compile(r"<[^>]+>")


def app_ui_controls(lines: Sequence[dict[str, Any]], after: int) -> list[str]:
    """Labels of delete/cancel controls on the pages loaded after the save."""
    labels: list[str] = []
    seen: set[str] = set()
    for line in lines[after + 1 :]:
        if "html" not in str(line.get("response_type", "")):
            continue
        for match in _CONTROL.finditer(str(line.get("response_text", ""))):
            text = match.group(2) if match.group(2) is not None else match.group(3)
            label = html.unescape(_TAGS.sub("", text or "")).strip()
            if label and len(label) <= 40 and INVERSE_WORDS.search(label):
                if label.lower() not in seen:
                    seen.add(label.lower())
                    labels.append(label)
    return labels


# -- field restore -------------------------------------------------------------


def field_restore_candidate(
    workflow: Workflow, index: int, effect: Effect
) -> tuple[Optional[Candidate], str]:
    """The save's own steps again, with the edited field's old value."""
    from openadapt_flow.run_gate import is_consequential

    step = workflow.steps[index]
    if effect.kind is not EffectKind.FIELD_EQUALS:
        return None, "the save creates a record; there is no old value to restore"
    if effect.value is None or effect.value.param is None:
        return None, (
            f"the field {effect.field!r} is saved as a recorded constant, not a "
            "parameter, so the steps can't type its old value"
        )
    assert effect.field is not None
    for earlier in workflow.steps[:index]:
        if is_consequential(
            earlier, workflow, require_current_risk_certification=False
        ):
            return None, (
                f"the steps that lead to the save include another save (step "
                f"{earlier.id}); repeating them would write twice"
            )
    edited = effect.value.param
    param = _prior_param(effect.field)
    steps: list[Step] = []
    typed = False
    for original in workflow.steps[: index + 1]:
        copy = original.model_copy(deep=True)
        copy.id = f"undo.{original.id}"
        copy.api_binding = None
        if copy.param == edited:
            copy.param = param
            typed = True
        steps.append(copy)
    if not typed:
        return None, (
            f"no step types the parameter {edited!r}, so the old value can't be "
            "typed back"
        )
    inverse, extra = _inverse_effect(step, effect)
    steps[-1].effects = [inverse]
    undo_id = _undo_id(step, "field_restore", 1)
    return (
        Candidate(
            candidate_id=undo_id,
            undoes=step.id,
            source="field_restore",
            basis=(
                f"step {step.id} sets {effect.field!r}; the undo runs the same "
                "steps with the value read back before the change"
            ),
            undo=UndoStep(
                id=undo_id,
                undoes=step.id,
                intent=f"set {effect.field} back to the value it had before step "
                f"{step.id}"[:512],
                source="synthesized",
                steps=steps,
                **extra,
            ),
        ),
        "",
    )


@dataclass
class Sources:
    """Where :func:`find_candidates` looks."""

    #: Compiled bundles of other recordings.
    recordings: list[Path] = field(default_factory=list)
    #: The save's recording directory (its ``network.jsonl``).
    network_lines: list[dict[str, Any]] = field(default_factory=list)


def find_candidates(
    workflow: Workflow, index: int, sources: Sources
) -> tuple[list[Candidate], list[str]]:
    """Every candidate for the saving step at ``index``, best first, and the
    notes on sources that gave none."""
    step = workflow.steps[index]
    notes: list[str] = []
    effect = record_effect(step)
    if effect is None:
        return [], [
            "the save declares no record effect Flow can check (bind its "
            "record_written or field_equals effect first)"
        ]
    params = dict(workflow.params or {})
    out = recording_candidates(step, effect, sources.recordings)
    if not sources.recordings:
        notes.append("no other recordings were given")
    elif not out:
        notes.append("no other recording deletes or cancels a record")
    found, note = network_candidates(step, effect, sources.network_lines, params)
    out.extend(found)
    if note:
        notes.append(note)
    restore, note = field_restore_candidate(workflow, index, effect)
    if restore is not None:
        out.append(restore)
    elif note:
        notes.append(note)
    if sources.network_lines:
        wanted = _selector_values(effect, params)
        at = _save_request(sources.network_lines, wanted)
        labels = app_ui_controls(sources.network_lines, at if at is not None else -1)
        if labels:
            named = ", ".join(repr(label) for label in labels)
            out.append(
                Candidate(
                    candidate_id=f"{step.id}:app_ui",
                    undoes=step.id,
                    source="app_ui",
                    basis=f"pages loaded after the save have {named} controls",
                    problem=(
                        f"Flow found {named} on the pages after the save but "
                        "builds an undo from a control only from a recording of "
                        "a person using it; record that and pass it with "
                        "--from-recording"
                    ),
                )
            )
    return out, notes


# ---------------------------------------------------------------------------
# Verification on a test copy
# ---------------------------------------------------------------------------


class UndoTestCopy(Protocol):
    """A resettable test copy an undo can be tried on."""

    name: str

    def reset(self) -> None: ...

    def snapshot(self) -> Optional[list[dict[str, Any]]]:
        """Every record the read-back can see; None when unreadable."""
        ...

    def read_records(self) -> Optional[list[dict[str, Any]]]:
        """Records through the run's own effect verifier (``undo run`` path)."""
        ...

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, Any],
    ) -> Optional[RunReport]: ...


def _canonical_records(
    records: list[dict[str, Any]], ignore: frozenset[str]
) -> list[str]:
    return sorted(
        json.dumps(
            {k: v for k, v in r.items() if k not in ignore}, sort_keys=True, default=str
        )
        for r in records
    )


def _record_key(record: dict[str, Any]) -> str:
    if "id" in record:
        return f"id:{record['id']}"
    return json.dumps(record, sort_keys=True, default=str)


def _differing_keys(
    before: list[dict[str, Any]], after: list[dict[str, Any]], ignore: frozenset[str]
) -> tuple[list[str], list[str], dict[str, set[str]]]:
    """``(added, removed, changed fields per key)`` between two snapshots."""
    old = {_record_key(r): r for r in before}
    new = {_record_key(r): r for r in after}
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    changed: dict[str, set[str]] = {}
    for key in set(old) & set(new):
        a = {k: v for k, v in old[key].items() if k not in ignore}
        b = {k: v for k, v in new[key].items() if k not in ignore}
        if a != b:
            changed[key] = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    return added, removed, changed


def compare_snapshots(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    ignore: frozenset[str] = frozenset(),
    after_save: Optional[list[dict[str, Any]]] = None,
) -> Optional[str]:
    """None when ``after`` holds exactly the records of ``before``; else why
    not, in counts and field names only.

    With ``after_save``, a record that differs but that the save itself did
    not add or change is named as collateral: the undo changed a record it
    had no business touching.
    """
    if _canonical_records(before, ignore) == _canonical_records(after, ignore):
        return None
    added, removed, changed = _differing_keys(before, after, ignore)
    touched: Optional[set[str]] = None
    if after_save is not None:
        s_added, s_removed, s_changed = _differing_keys(before, after_save, ignore)
        touched = set(s_added) | set(s_removed) | set(s_changed)
    parts = []
    if added:
        parts.append(f"{len(added)} record(s) added since before the save remain")
    if removed:
        parts.append(f"{len(removed)} record(s) from before the save are gone")
    if changed:
        fields = sorted({f for names in changed.values() for f in names})
        parts.append(f"{len(changed)} record(s) changed (fields: {', '.join(fields)})")
    if touched is not None:
        collateral = [k for k in [*removed, *changed] if k not in touched]
        if collateral:
            parts.append(
                f"{len(collateral)} of them the save never touched (collateral)"
            )
    if not parts:
        parts.append("the record sets differ")
    return "after the undo the test copy differs from before the save: " + "; ".join(
        parts
    )


def _snapshot_digest(records: Optional[list[dict[str, Any]]], ignore) -> Optional[str]:
    if records is None:
        return None
    return hashlib.sha256(_canonical(_canonical_records(records, ignore))).hexdigest()


def _earlier_saves(workflow: Workflow, index: int) -> list[str]:
    return [step.id for i, step in saving_steps(workflow) if i < index]


def _prefix_workflow(workflow: Workflow, index: int) -> Workflow:
    return workflow.model_copy(
        update={
            "steps": [s.model_copy(deep=True) for s in workflow.steps[:index]],
            "program": None,
            "subflows": {},
            "undo_steps": [],
        }
    )


def _save_workflow(workflow: Workflow, index: int, undo: UndoStep) -> Workflow:
    return workflow.model_copy(
        update={
            "steps": [s.model_copy(deep=True) for s in workflow.steps[: index + 1]],
            "program": None,
            "subflows": {},
            "undo_steps": [undo],
        }
    )


def _report_line(report: Optional[RunReport]) -> str:
    if report is None:
        return "left no report"
    failed = next((r for r in report.results if not r.ok and not r.skipped), None)
    if report.success:
        return "completed"
    if failed is not None:
        return f"stopped at step {failed.step_id}"
    return "did not complete"


def verify_candidate(
    candidate: Candidate,
    workflow: Workflow,
    index: int,
    *,
    target: UndoTestCopy,
    bundle_dir: Path,
    work_dir: Path,
    params: Optional[dict[str, Any]] = None,
    ignore_fields: Sequence[str] = (),
) -> CandidateResult:
    """Run the save, then ``candidate``'s undo, on ``target``; judge both.

    Passes only when the save changed the test copy, the undo ran and its
    declared effect was confirmed by an independent read-back, and every
    record afterwards equals the record before the save (the save's own record
    and every other one). The copy is reset before and after.
    """
    assert candidate.undo is not None
    undo = candidate.undo
    ignore = frozenset(ignore_fields)
    params = dict(params if params is not None else (workflow.params or {}))
    stage = work_dir / "bundles" / candidate.candidate_id
    runs = work_dir / "runs" / candidate.candidate_id
    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "candidate_id": candidate.candidate_id,
        "undoes": candidate.undoes,
        "source": candidate.source,
        "basis": candidate.basis,
        "test_copy": target.name,
        "undo_digest": _sha256(undo.model_dump(mode="json")),
        "ignored_fields": sorted(ignore),
        "started_at": _now(),
    }

    def done(status: str, reason: str) -> CandidateResult:
        evidence["finished_at"] = _now()
        evidence["verdict"] = status
        evidence["reason"] = reason
        try:
            target.reset()
        except Exception:  # noqa: BLE001 - every attempt resets first
            pass
        return CandidateResult(
            candidate_id=candidate.candidate_id,
            source=candidate.source,
            basis=candidate.basis,
            status="verified" if status == "verified" else "failed",
            reason=reason,
            evidence=evidence,
        )

    try:
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(bundle_dir, stage)
        for rel, source in candidate.assets.items():
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, stage / rel)
        if runs.exists():
            shutil.rmtree(runs)
        target.reset()
    except Exception as exc:  # noqa: BLE001 - nothing ran yet
        return done("failed", f"could not prepare the test copy ({exc})")
    earlier = _earlier_saves(workflow, index)
    evidence["earlier_saves"] = len(earlier)
    if earlier:
        # "Before the save" is the state the steps ahead of it leave: run
        # them once, read every record, and reset for the real attempt.
        try:
            prefix_report = target.run(
                _prefix_workflow(workflow, index),
                bundle_dir=stage,
                run_dir=runs / "before-save",
                params=params,
            )
        except Exception as exc:  # noqa: BLE001 - recorded as a failure
            prefix_report = None
            evidence["prefix_error"] = type(exc).__name__
        evidence["before_save_steps"] = _report_line(prefix_report)
        if prefix_report is None or not prefix_report.success:
            return done(
                "failed",
                "the steps before the save did not complete on the test copy "
                f"({evidence['before_save_steps']})",
            )
    try:
        before = target.snapshot()
        if earlier:
            target.reset()
    except Exception as exc:  # noqa: BLE001 - nothing more ran
        return done("failed", f"could not read or reset the test copy ({exc})")
    evidence["before"] = {
        "records": None if before is None else len(before),
        "sha256": _snapshot_digest(before, ignore),
    }
    if before is None:
        return done(
            "failed", "the test copy's records could not be read before the save"
        )

    save_wf = _save_workflow(workflow, index, undo)
    try:
        save_report = target.run(
            save_wf, bundle_dir=stage, run_dir=runs / "save", params=params
        )
    except Exception as exc:  # noqa: BLE001 - a crashed replay decides nothing
        save_report = None
        evidence["save_error"] = type(exc).__name__
    evidence["save"] = _report_line(save_report)
    evidence["save_model_calls"] = int(getattr(save_report, "model_calls", 0) or 0)
    if save_report is None or not save_report.success:
        return done(
            "failed",
            f"the save did not complete on the test copy ({evidence['save']}), "
            "so there is nothing to undo",
        )
    mid = target.snapshot()
    evidence["after_save"] = {
        "records": None if mid is None else len(mid),
        "sha256": _snapshot_digest(mid, ignore),
    }
    if mid is None:
        return done(
            "failed", "the test copy's records could not be read after the save"
        )
    if compare_snapshots(before, mid, ignore=ignore) is None:
        return done(
            "failed",
            "the save changed no record on the test copy, so the undo can't be "
            "shown to reverse it",
        )
    try:
        undo_params = resolve_undo_params(
            undo, runs / "save", params, target.read_records
        )
    except UndoRefused as exc:
        return done("failed", f"the undo could not be prepared: {exc}")
    try:
        undo_report = target.run(
            undo_workflow(save_wf, undo),
            bundle_dir=stage,
            run_dir=runs / "undo",
            params=undo_params,
        )
    except Exception as exc:  # noqa: BLE001 - recorded as a failed undo
        undo_report = None
        evidence["undo_error"] = type(exc).__name__
    evidence["undo"] = _report_line(undo_report)
    evidence["undo_model_calls"] = int(getattr(undo_report, "model_calls", 0) or 0)
    confirmed, judged = judge_undo_report(undo, undo_report)
    evidence["undo_judgement"] = judged
    after = target.snapshot()
    evidence["after_undo"] = {
        "records": None if after is None else len(after),
        "sha256": _snapshot_digest(after, ignore),
    }
    if after is None:
        return done(
            "failed", "the test copy's records could not be read after the undo"
        )
    difference = compare_snapshots(before, after, ignore=ignore, after_save=mid)
    if difference is not None:
        return done("failed", difference)
    if not confirmed:
        return done("failed", f"the undo was not confirmed: {judged}")
    return done(
        "verified",
        "the undo ran, its record read-back confirmed it, and every record "
        "matches the read-back from before the save",
    )


# ---------------------------------------------------------------------------
# The whole pass
# ---------------------------------------------------------------------------

_ORDER = {"recording": 0, "network": 1, "field_restore": 2, "app_ui": 3}


def propose_undos(
    workflow: Workflow,
    *,
    bundle_dir: Path,
    sources: Sources,
    target: Optional[UndoTestCopy],
    work_dir: Optional[Path] = None,
    ignore_fields: Sequence[str] = (),
) -> tuple[ProposeReport, dict[str, tuple[Candidate, CandidateResult]]]:
    """Find and verify candidates for every saving step.

    Returns the report and, per saving step, the first verified candidate.
    Without a ``target`` nothing runs and nothing can verify: every built
    candidate is ``not_run``.
    """
    report = ProposeReport(
        workflow_name=workflow.name, test_copy=target.name if target else None
    )
    verified: dict[str, tuple[Candidate, CandidateResult]] = {}
    if workflow.program is not None:
        raise ValueError(
            "undo propose works on straight-line bundles; this one is a program graph"
        )
    from openadapt_flow.after_stop import _has_loop

    if _has_loop(workflow):
        raise ValueError(
            "undo propose does not run on a workflow with a loop: an undo runs "
            "with the run's base parameters"
        )
    declared = {u.undoes: u.id for u in workflow.undo_steps}
    own_tmp = work_dir is None
    work = Path(tempfile.mkdtemp(prefix="undo-propose-")) if own_tmp else work_dir
    assert work is not None
    try:
        for index, step in saving_steps(workflow):
            row = SavingStepResult(step_id=step.id, intent=step.intent)
            report.saving_steps.append(row)
            if step.id in declared:
                row.verified_undo = None
                row.reason = (
                    f"the bundle already has undo {declared[step.id]!r} for this step"
                )
                continue
            candidates, notes = find_candidates(workflow, index, sources)
            candidates.sort(key=lambda c: _ORDER[c.source])
            for candidate in candidates:
                if candidate.undo is None:
                    row.candidates.append(
                        CandidateResult(
                            candidate_id=candidate.candidate_id,
                            source=candidate.source,
                            basis=candidate.basis,
                            status="not_run",
                            reason=candidate.problem or "could not be built",
                        )
                    )
                    continue
                if step.id in verified:
                    reason = "an earlier candidate for this step verified"
                elif target is None:
                    reason = "no test copy was given (--config), so nothing ran"
                else:
                    reason = ""
                if reason:
                    row.candidates.append(
                        CandidateResult(
                            candidate_id=candidate.candidate_id,
                            source=candidate.source,
                            basis=candidate.basis,
                            status="not_run",
                            reason=reason,
                        )
                    )
                    continue
                assert target is not None
                result = verify_candidate(
                    candidate,
                    workflow,
                    index,
                    target=target,
                    bundle_dir=bundle_dir,
                    work_dir=work,
                    ignore_fields=ignore_fields,
                )
                row.candidates.append(result)
                if result.status == "verified":
                    verified[step.id] = (candidate, result)
                    row.verified_undo = candidate.candidate_id
            if row.verified_undo is None:
                failed = [c for c in row.candidates if c.status == "failed"]
                if failed:
                    row.reason = "; ".join(f"{c.source}: {c.reason}" for c in failed)
                elif row.candidates:
                    row.reason = "; ".join(
                        f"{c.source}: {c.reason}" for c in row.candidates
                    )
                else:
                    row.reason = "no candidate: " + "; ".join(notes)
                if notes and row.candidates:
                    row.reason += " (also: " + "; ".join(notes) + ")"
                row.to_close_the_gap = _gap_hint(row, notes, target is not None)
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
    return report, verified


def _gap_hint(row: SavingStepResult, notes: list[str], had_target: bool) -> str:
    """What a person can do so the step gets a verified undo."""
    if any("declares no record effect" in note for note in notes):
        return (
            "bind the save's record check (a record_written or field_equals "
            "effect), then propose again"
        )
    if any(c.status == "failed" for c in row.candidates):
        return (
            "record a person doing the app's own delete or cancel path and pass "
            "it with --from-recording; if the only difference is a timestamp or "
            "audit field, a person may name it with --ignore-field"
        )
    built = [c for c in row.candidates if c.source != "app_ui"]
    if not had_target and any(c.status == "not_run" for c in built):
        return "run again with --config naming a declared test copy"
    ui = next((c for c in row.candidates if c.source == "app_ui"), None)
    if ui is not None:
        return (
            "record a person doing the app's own delete or cancel path ("
            + ui.basis
            + ") and pass it with --from-recording"
        )
    return (
        "record a person doing the app's own delete or cancel path and pass it "
        "with --from-recording"
    )


def write_proposals(
    bundle_dir: Path | str,
    verified: Mapping[str, tuple[Candidate, CandidateResult]],
) -> Workflow:
    """Write each verified undo into the bundle as a proposal awaiting a
    person's approval, with its evidence under ``undo-evidence/``."""
    bundle_dir = Path(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    if not verified:
        return workflow
    taken = {u.id for u in workflow.undo_steps}
    targets = {u.undoes for u in workflow.undo_steps}
    new: list[UndoStep] = []
    copies: dict[str, Path] = {}
    payloads: dict[str, bytes] = {}
    for step_id, (candidate, result) in verified.items():
        assert candidate.undo is not None and result.evidence is not None
        if candidate.undo.id in taken or step_id in targets:
            raise ValueError(
                f"the bundle already has an undo for step {step_id!r} or the id "
                f"{candidate.undo.id!r}"
            )
        rel = f"{EVIDENCE_DIRNAME}/{candidate.undo.id}.json"
        payload = json.dumps(result.evidence, indent=2, sort_keys=True).encode("utf-8")
        payloads[rel] = payload
        for asset_rel, source in candidate.assets.items():
            copies[asset_rel] = source
        new.append(
            candidate.undo.model_copy(
                update={
                    "proposal": UndoProposal(
                        candidate_source=candidate.source,  # type: ignore[arg-type]
                        basis=candidate.basis[:1024],
                        test_copy=str(result.evidence.get("test_copy", "test copy")),
                        verified_at=str(result.evidence.get("finished_at", _now())),
                        evidence_path=rel,
                        evidence_sha256=hashlib.sha256(payload).hexdigest(),
                    )
                }
            )
        )
    for rel in [*copies, *payloads]:
        if (bundle_dir / rel).exists():
            raise ValueError(f"the bundle already has {rel}")
    for rel, source in copies.items():
        (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, bundle_dir / rel)
    for rel, payload in payloads.items():
        (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle_dir / rel).write_bytes(payload)
    return save_undo_steps(
        bundle_dir,
        workflow,
        [*workflow.undo_steps, *new],
        reason="a verified undo was proposed after certification",
    )


def proposal_problem(bundle_dir: Path | str, undo: UndoStep) -> Optional[str]:
    """Why ``undo``'s proposal can't be approved, or None."""
    if undo.proposal is None:
        return f"undo {undo.id!r} is not a proposal"
    if undo.proposal.approved_by is not None:
        return f"undo {undo.id!r} was already approved by {undo.proposal.approved_by}"
    path = Path(bundle_dir) / undo.proposal.evidence_path
    try:
        data = path.read_bytes()
    except OSError:
        return f"the verification evidence {undo.proposal.evidence_path} is missing"
    if hashlib.sha256(data).hexdigest() != undo.proposal.evidence_sha256:
        return (
            f"the verification evidence {undo.proposal.evidence_path} changed "
            "since the undo was verified"
        )
    try:
        evidence = json.loads(data)
    except json.JSONDecodeError:
        return "the verification evidence is not JSON"
    if evidence.get("verdict") != "verified":
        return "the verification evidence does not record a verified undo"
    proposed = undo.model_copy(update={"proposal": None})
    if evidence.get("undo_digest") != _sha256(proposed.model_dump(mode="json")):
        return "the undo changed since it was verified on the test copy"
    return None


def accept_proposal(bundle_dir: Path | str, undo_id: str, *, approver: str) -> Workflow:
    """Record a person's one-time approval of a proposed undo; re-seal.

    Refuses when the evidence file is missing or changed, or the undo
    changed since it was verified. Each run of the undo still needs its own
    approval after a stop (``undo approve``).
    """
    bundle_dir = Path(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    undo = next((u for u in workflow.undo_steps if u.id == undo_id), None)
    if undo is None:
        raise ValueError(f"the bundle has no undo {undo_id!r}")
    problem = proposal_problem(bundle_dir, undo)
    if problem:
        raise ValueError(problem)
    assert undo.proposal is not None
    approved = undo.model_copy(
        update={
            "proposal": undo.proposal.model_copy(
                update={"approved_by": approver, "approved_at": _now()}
            )
        }
    )
    steps = [approved if u.id == undo_id else u for u in workflow.undo_steps]
    return save_undo_steps(
        bundle_dir,
        workflow,
        steps,
        reason="a proposed undo was approved after certification",
    )


# ---------------------------------------------------------------------------
# The deployment-file test copy
# ---------------------------------------------------------------------------


class DeploymentUndoTestCopy:
    """The test copy a deployment file declares (``test_copy``).

    Reset: the probe path's reset (``reset_url`` / ``reset_command``).
    Snapshot: the reward contract's oracle, which reads every record in the
    collection. Runs: a fresh session each time, wired with the deployment's
    own effect verifier and API actuator, so the undo is checked the way
    ``undo run`` checks it. Model grounding and egress stay off.
    """

    def __init__(self, cfg: Any, *, deployment_path: Path) -> None:
        from openadapt_flow.compiler.probing import DeploymentProbeTarget
        from openadapt_flow.reward.models import RewardBundle
        from openadapt_flow.reward.oracles import build_oracle

        self._probe = DeploymentProbeTarget(cfg)  # refuses a non-test target
        self.cfg = cfg
        self.name = str(cfg.name)
        contract = Path(str(cfg.test_copy.reward_contract)).expanduser()
        if not contract.is_absolute():
            contract = Path(deployment_path).parent / contract
        bundle = RewardBundle.load(contract)
        self._oracle = build_oracle(bundle.oracle, bundle.directory)
        self._params: dict[str, Any] = {}

    def reset(self) -> None:
        self._probe.reset()

    def snapshot(self) -> Optional[list[dict[str, Any]]]:
        from openadapt_flow.reward.oracles import records_of

        return records_of(self._oracle.read({}))

    def read_records(self) -> Optional[list[dict[str, Any]]]:
        from openadapt_flow.after_stop import records_reader
        from openadapt_flow.deployment import build_effect_verifier

        reader = records_reader(build_effect_verifier(self.cfg.effects, self._params))
        return reader() if reader is not None else None

    def _replay(self, backend, workflow, bundle_dir, run_dir, params) -> RunReport:
        from openadapt_flow.deployment import (
            build_api_actuator,
            build_effect_verifier,
            build_replayer,
        )

        replayer = build_replayer(
            backend,
            allow_egress=False,
            effect_verifier=build_effect_verifier(self.cfg.effects, params),
            api_actuator=build_api_actuator(self.cfg.actuation),
            durable=False,
            use_structural=True,
        )
        return replayer.run(
            workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
        )

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, Any],
    ) -> Optional[RunReport]:
        from openadapt_flow.backends.factory import build_backend

        self._params = dict(params)
        backend_cfg = self.cfg.backend
        if backend_cfg.kind != "web":
            backend = build_backend(backend_cfg)
            try:
                return self._replay(backend, workflow, bundle_dir, run_dir, params)
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        if not backend_cfg.url:
            raise ValueError("a web test copy needs backend.url")
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import launch_chromium

        with sync_playwright() as p:
            browser = launch_chromium(p.chromium, headless=not backend_cfg.headed)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.goto(backend_cfg.url)
                backend = build_backend(backend_cfg, page=page)
                return self._replay(backend, workflow, bundle_dir, run_dir, params)
            finally:
                browser.close()


def load_network_lines(recording: Optional[Path | str]) -> list[dict[str, Any]]:
    if recording is None:
        return []
    from openadapt_flow.network_capture import NETWORK_FILE, load

    if not (Path(recording) / NETWORK_FILE).is_file():
        return []
    return list(load(recording))


__all__ = [
    "Candidate",
    "CandidateResult",
    "DeploymentUndoTestCopy",
    "ProposeReport",
    "SavingStepResult",
    "Sources",
    "UndoTestCopy",
    "accept_proposal",
    "app_ui_controls",
    "compare_snapshots",
    "field_restore_candidate",
    "find_candidates",
    "load_network_lines",
    "network_candidates",
    "proposal_problem",
    "propose_undos",
    "record_effect",
    "recording_candidates",
    "saving_steps",
    "verify_candidate",
    "write_proposals",
]
