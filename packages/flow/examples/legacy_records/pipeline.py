"""The steps both reference workflows share.

1. Read a document from an inbox folder with Flow intake (local, no model).
2. Flag exceptions: low-confidence values and values that fail a pattern.
3. A person in the staff role confirms or corrects each exception.
4. One person in the approver role makes the decision, from a fixed list.
5. The app-specific steps write the record and read it back (per workflow).
6. The result exchange is checked, sealed and written to the L1 directory.

People's answers come from the terminal, or from an answers file in the demo.
The same person cannot both confirm exceptions and approve. In scripted mode
an exception with no listed answer is not confirmed (the document halts), and
a scripted decision must be one of the options.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from evidence import (
    DOC_SIDE,
    EXECUTOR,
    V,
    attach_payload,
    now,
    provenance,
    seal,
    write_exchange,
)

from openadapt_flow.intake.document import read_document
from openadapt_flow.intake.extract import extract_fields
from openadapt_flow.interchange import READ_GRADES, emit_exchange
from openadapt_flow.ir import ParamSpec


@dataclass
class Spec:
    name: str
    kind: str  # interchange value type
    labels: list[str]
    pattern: Optional[str] = None


@dataclass
class People:
    staff: str
    approver: str
    answers: dict[str, Any] = field(default_factory=dict)
    #: Site policy: send decisions that write nothing (no_action) to review.
    review_decisions: bool = False

    @classmethod
    def load(
        cls, path: Optional[Path], staff_role: str, approver_role: str
    ) -> "People":
        if path is None:
            staff = input(f"{staff_role} id: ").strip()
            approver = input(f"{approver_role} id: ").strip()
            people = cls(f"{staff_role}:{staff}", f"{approver_role}:{approver}")
        else:
            data = json.loads(path.read_text())
            people = cls(
                data["staff"],
                data["approver"],
                data,
                bool(data.get("policy", {}).get("review_decisions", False)),
            )
        if people.staff == people.approver:
            raise SystemExit("the person who confirms exceptions cannot also approve")
        return people

    def correct(self, doc: str, name: str, proposed: Optional[str]) -> Optional[str]:
        """The value the staff member settled on, or None when nobody did.

        In scripted mode only a listed answer counts: an exception the answers
        file does not mention was not looked at by anyone.
        """
        if self.answers:
            scripted = self.answers.get("corrections", {}).get(doc, {})
            got = scripted.get(name)
            return None if got is None else str(got)
        got = input(f"  {name} = {proposed!r}. Enter to accept, or type a correction: ")
        return got.strip() or proposed

    def decide(self, doc: str, question: str, options: list[str]) -> str:
        if self.answers:
            choice = self.answers.get("decisions", {}).get(doc)
            if choice not in options:
                raise SystemExit(
                    f"scripted decision for {doc} is {choice!r}, not one of {options}"
                )
            return choice
        while True:
            got = input(f"  {question} {options}: ").strip()
            if got in options:
                return got


def intake(
    path: Path,
    specs: list[Spec],
    run_dir: Path,
    *,
    workflow: str,
    artifact: dict[str, Any],
    people: People,
    staff_role: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Steps 1-3: proposed values, then a person settles every exception."""
    doc = read_document(path)
    result = extract_fields(
        doc,
        {s.name: ParamSpec(name=s.name) for s in specs},
        aliases={s.name: s.labels for s in specs},
    )
    fields = []
    for s in specs:
        f = result.fields[s.name]
        value, conf = f.value, f.confidence
        reasons = []
        if value is None:
            reasons.append("not found")
        elif s.pattern and not re.fullmatch(s.pattern, value):
            reasons.append(f"does not match {s.pattern}")
            conf = min(conf, 0.5)
        elif conf < 0.9:
            reasons.append(f"confidence {conf:.2f}")
        entry: dict[str, Any] = {
            "name": s.name,
            "type": s.kind,
            "value": value,
            "confidence": round(conf, 3),
            "reads": [
                {
                    "channel": "document_parse",
                    "value": value,
                    "artifact_id": "doc",
                    "locator": {"page": 1, "line": f.source_line},
                }
            ]
            if f.source_line
            else [],
        }
        if reasons:
            log(f"  exception: {s.name} = {value!r} ({'; '.join(reasons)})")
            fixed = people.correct(path.name, s.name, value)
            if fixed is None:
                entry["needs_review"] = True
                entry["review_reason"] = (
                    f"exception ({'; '.join(reasons)}) was not confirmed or "
                    "corrected by a person"
                )
                if (
                    s.pattern
                    and value is not None
                    and not re.fullmatch(s.pattern, value)
                ):
                    # The document read keeps what was seen; the field does
                    # not carry a value that fails its pattern.
                    entry["value"] = None
                log(f"    nobody settled {s.name}; it is not confirmed")
                fields.append(entry)
                continue
            if s.pattern and not re.fullmatch(s.pattern, fixed):
                raise SystemExit(
                    f"{s.name}: {fixed!r} still does not match {s.pattern}"
                )
            entry["status"] = "corrected" if fixed != value else "confirmed"
            entry["confirmed_by"] = {
                "kind": "human",
                "id": people.staff,
                "role": staff_role,
            }
            entry["reads"].append({"channel": "human", "value": fixed})
            entry["value"] = fixed
            log(f"    {people.staff} {entry['status']} it: {fixed!r}")
        fields.append(entry)
    artifact = dict(
        artifact,
        artifact_id="doc",
        kind="source_document",
        payload=attach_payload(path, run_dir),
    )
    return {
        "schema_version": V,
        "exchange_id": path.stem,
        "direction": "to_executor",
        "workflow": workflow,
        "session_id": run_dir.name,
        "artifacts": [artifact],
        "fields": fields,
        "identities": [],
        "decisions": [],
        "outcome": "pending",
        "provenance": provenance(DOC_SIDE),
        "extensions": {},
    }


def decide(
    ex: dict[str, Any],
    people: People,
    *,
    question: str,
    options: list[str],
    role: str,
    output_field: str,
    log: Callable[[str], None],
) -> str:
    """Step 4: one approval by a person in ``role``."""
    choice = people.decide(ex["exchange_id"] + ".pdf", question, options)
    ex["fields"].append(
        {
            "name": output_field,
            "type": "enum",
            "value": choice,
            "confidence": 1.0,
            "reads": [{"channel": "human", "value": choice}],
        }
    )
    ex["decisions"].append(
        {
            "decision_id": f"{ex['exchange_id']}-{output_field}",
            "question": question,
            "options": options,
            "choice": choice,
            "output_field": output_field,
            "authorized_roles": [role],
            "decided_by": {"kind": "human", "id": people.approver, "role": role},
            "decided_at": now(),
        }
    )
    log(f"  {people.approver} chose {choice!r}")
    return choice


def unsettled(ex: dict[str, Any]) -> list[str]:
    """Names of request fields that still need a person."""
    return [f["name"] for f in ex["fields"] if f.get("needs_review")]


def record_no_action(
    req: dict[str, Any],
    res: dict[str, Any],
    *,
    reason_code: str,
    required_grade: str,
    evidence: list[str],
) -> Optional[str]:
    """Mark ``res`` as ``no_action``: decided, and nothing meant to be written.

    ``evidence`` names fields of ``res`` whose reads show no new or changed
    record; each must be graded at least ``required_grade``, the check level
    a commit of the workflow needs. Returns a halt reason instead when the
    evidence is weaker than that, and leaves ``res`` unchanged.
    """
    grades = {f["name"]: f.get("grade", "unchecked") for f in res["fields"]}
    weak = [
        n
        for n in evidence
        if READ_GRADES.index(grades.get(n, "unchecked"))
        < READ_GRADES.index(required_grade)
    ]
    if weak:
        return (
            f"could not show at {required_grade} that nothing was written "
            f"({', '.join(f'{n}: {grades.get(n)}' for n in weak)})"
        )
    decision = req["decisions"][-1]
    res["outcome"] = "no_action"
    res["no_action"] = {
        "decided_by": decision["decided_by"],
        "reason_code": reason_code,
        "decision_id": decision["decision_id"],
        "decided_at": decision["decided_at"],
        "required_grade": required_grade,
        "evidence": evidence,
    }
    return None


def result_exchange(ex: dict[str, Any], *, artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": V,
        "exchange_id": ex["exchange_id"] + "-result",
        "direction": "from_executor",
        "workflow": ex["workflow"],
        "session_id": ex["session_id"],
        "in_reply_to": ex["exchange_id"],
        "artifacts": [dict(artifact, kind="structured_observation")],
        "fields": [],
        "identities": [],
        "outcome": "pending",
        "provenance": provenance(EXECUTOR),
    }


def finish(
    request: dict[str, Any],
    result: dict[str, Any],
    run_dir: Path,
    l1_dir: Path,
    log: Callable[[str], None],
    *,
    review: bool = True,
) -> None:
    """Step 6: check, seal, write both exchanges, emit to the L1 directory.

    A halted result, or one with values to review, goes to the review queue
    unless ``review`` is False. A ``no_action`` result passes
    ``review=people.review_decisions``: it goes to review only when the site's
    policy reviews decisions.
    """
    request = seal(request, run_dir)
    result = seal(result, run_dir)
    for doc in (request, result):
        path = write_exchange(doc, run_dir)
        refs = emit_exchange(path, l1_dir)
        for r in refs:
            log(f"  L1: {r.path.name}")
    to_review = [
        f"{f['name']}: {f['review_reason']}"
        for f in result["fields"]
        if f.get("needs_review")
    ]
    if review and (result["outcome"] in ("halted", "no_action") or to_review):
        queue = run_dir / "review_queue"
        queue.mkdir(exist_ok=True)
        (queue / f"{result['exchange_id']}.json").write_text(
            json.dumps(
                {
                    "exchange": f"{result['exchange_id']}.json",
                    "outcome": result["outcome"],
                    "halt_reason": result.get("halt_reason"),
                    "fields_to_review": to_review,
                    "needs": "a person",
                },
                indent=2,
            )
        )
        log(f"  review queue: {result['exchange_id']}")
