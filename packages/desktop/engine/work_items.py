"""Items a task runs on, and the people decisions that follow a stop.

A *work item* is one row of input values (for example one member ID and one
note) that one run of a task processes. Desktop records every item in the
local index database so that three things stay true:

* The Needs-you inbox lists each item that a person must decide on, with the
  plain question and the actions that its outcome allows.
* Results counts only what the evidence proves: an item is "done and checked"
  only when its run's transaction outcome is VERIFIED.
* No item is retried blindly. While an item's save may have gone through,
  Desktop refuses to run more items of that task until a person checks the
  record. An item whose run proved that nothing was written may run again.

Input values can identify a patient. They stay in the local database inside
the customer boundary, reach Flow only through a private short-lived params
file, and never appear in events, logs, notifications, or Cloud payloads.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from engine.outcomes import (
    CHECK_RECORD,
    DID_NOT_FINISH,
    DID_NOT_START,
    DONE_CHECKED,
    FINISHED_UNCHECKED,
    STOPPED_BEFORE_SAVING,
    plain_result,
)

MAX_INPUTS = 100
MAX_VALUE_CHARS = 4000
MAX_LABEL_CHARS = 80

# Item status values in the ``work_items`` table.
QUEUED = "queued"
DONE = "done"  # done and checked: transaction outcome VERIFIED
OPEN = "open"  # waiting for a person
SKIPPED = "skipped"
DECLINED = "declined"
DONE_BY_PERSON = "done_by_person"
CONFIRMED_SAVED = "confirmed_saved"
RETRIED = "retried"
TEACHING = "teaching"

CLOSED_STATUSES = frozenset(
    {DONE, SKIPPED, DECLINED, DONE_BY_PERSON, CONFIRMED_SAVED, RETRIED}
)
#: Statuses that mean the item's change is in the record.
COMPLETED_STATUSES = frozenset({DONE, DONE_BY_PERSON, CONFIRMED_SAVED})

# The actions a person may take, by what the evidence proves about the save.
NO_EFFECT_ACTIONS = frozenset({"retry", "skip", "decline", "teach"})
UNCERTAIN_ACTIONS = frozenset({"confirmed_saved", "retry_after_check", "done_by_person"})


class WorkItemError(ValueError):
    """A refused item action. The message is safe to show to the operator."""


def validate_inputs(raw: Any, declared: list[dict]) -> dict[str, Any]:
    """Return the item's input values, or refuse before anything runs.

    Every key must be a declared, non-secret parameter of the task. Secret
    parameters come from the protected environment, never from a pasted list.
    """

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise WorkItemError("The item's values must be a set of named inputs.")
    if len(raw) > MAX_INPUTS:
        raise WorkItemError("An item can have at most 100 inputs.")
    by_name = {str(item.get("name")): item for item in declared}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise WorkItemError("Every input needs a name.")
        spec = by_name.get(key)
        if spec is None:
            raise WorkItemError(f"This task has no input named {key!r}.")
        if spec.get("secret"):
            raise WorkItemError(
                f"{key!r} is a protected input. It comes from this computer's "
                "protected settings, not from a list."
            )
        if isinstance(value, bool) or value is None:
            raise WorkItemError(f"{key!r} needs a text or number value.")
        if isinstance(value, (int, float)):
            values[key] = value
            continue
        if not isinstance(value, str):
            raise WorkItemError(f"{key!r} needs a text or number value.")
        if len(value) > MAX_VALUE_CHARS:
            raise WorkItemError(f"The value for {key!r} is too long.")
        values[key] = value
    # A missing input never falls back to the value from the recording: Flow
    # would otherwise fill it with the demonstration's record, and the item
    # would act on the wrong record.
    missing = [
        str(spec.get("name"))
        for spec in declared
        if spec.get("required")
        and not spec.get("secret")
        and str(spec.get("name")) not in values
    ]
    if missing:
        raise WorkItemError("This item is missing: " + ", ".join(sorted(missing)) + ".")
    for key, value in values.items():
        if isinstance(value, str) and not value.strip() and by_name[key].get("required"):
            raise WorkItemError(f"{key!r} can't be empty.")
    return values


def inputs_digest(workflow_id: str, values: dict[str, Any]) -> str:
    """Stable digest of one item's values for duplicate and retry checks."""

    canonical = json.dumps(
        {"workflow_id": workflow_id, "inputs": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def item_label(values: dict[str, Any], declared: list[dict]) -> str:
    """A short local label for the item, from its first declared input."""

    for spec in declared:
        name = str(spec.get("name"))
        if name in values and not spec.get("secret"):
            text = str(values[name]).strip()
            if text:
                return text[:MAX_LABEL_CHARS]
    return ""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def result_for_run(report: dict) -> dict[str, Any]:
    """The plain result for one dispatcher run report."""

    return plain_result(
        report.get("outcome"),
        report.get("transaction_outcome"),
        pre_action_refusal=bool(report.get("pre_action_refusal")),
    )


def status_after_run(result_key: str) -> str:
    """Item status right after its run ends."""

    return DONE if result_key == DONE_CHECKED else OPEN


def blocks_more_runs(item: dict) -> bool:
    """An open item whose save may have gone through blocks the whole task."""

    if item.get("status") != OPEN:
        return False
    return not bool(item.get("nothing_written"))


def allowed_actions(item: dict) -> list[str]:
    """Actions the evidence allows for one open item, in display order."""

    if item.get("status") not in {OPEN, TEACHING}:
        return []
    if item.get("nothing_written"):
        return ["retry", "skip", "decline", "teach"]
    return ["confirmed_saved", "retry_after_check", "done_by_person"]


def check_action(item: dict, action: str, *, record_checked: bool) -> None:
    """Refuse an action the item's evidence does not allow."""

    allowed = allowed_actions(item)
    if action not in allowed:
        raise WorkItemError("That choice isn't available for this item.")
    if action == "retry_after_check" and not record_checked:
        raise WorkItemError(
            "Look at the record first. Run it again only after you confirm "
            "that nothing was saved."
        )
    # Closing a possible save as done also rests on what the person saw in
    # the record, so the engine asks for the same confirmation the screen does.
    if action in {"confirmed_saved", "done_by_person"} and not record_checked:
        raise WorkItemError(
            "Look at the record first, then say what you found."
        )


def resolution_status(action: str) -> str:
    return {
        "retry": RETRIED,
        "retry_after_check": RETRIED,
        "skip": SKIPPED,
        "decline": DECLINED,
        "teach": TEACHING,
        "confirmed_saved": CONFIRMED_SAVED,
        "done_by_person": DONE_BY_PERSON,
    }[action]


def public_item(row: dict, *, include_values: bool = False) -> dict[str, Any]:
    """Project one stored item for the local Desktop UI."""

    result = plain_result(
        row.get("execution_outcome"),
        row.get("transaction_outcome"),
        pre_action_refusal=row.get("execution_outcome") == "refused",
    ) if row.get("execution_outcome") else None
    item = {
        "item_id": row["item_id"],
        "batch_id": row["batch_id"],
        "workflow_id": row["workflow_id"],
        "position": row["position"],
        "label": row.get("label") or "",
        "run_id": row.get("run_id"),
        "status": row["status"],
        "result": result["key"] if result else None,
        "nothing_written": bool(result and result["nothing_written"]),
        "execution_outcome": row.get("execution_outcome"),
        "transaction_outcome": row.get("transaction_outcome"),
        "halt_kind": row.get("halt_kind"),
        "halt_step": row.get("halt_step") or "",
        "halt_reason": row.get("halt_reason") or "",
        "expected": row.get("expected") or "",
        "observed": row.get("observed") or "",
        "duration_s": row.get("duration_s"),
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
        "resolved_at": row.get("resolved_at"),
        "retry_of": row.get("retry_of"),
    }
    item["actions"] = allowed_actions(item)
    if include_values:
        try:
            item["inputs"] = json.loads(row.get("inputs_json") or "{}")
        except ValueError:
            item["inputs"] = {}
    return item


def summarize(rows: list[dict], *, baselines: dict[str, float | None]) -> dict[str, Any]:
    """Results counts per task and in total. Only evidence counts as done."""

    def empty() -> dict[str, Any]:
        return {
            "done_checked": 0,
            "needed_person": 0,
            "waiting": 0,
            "wrong_record_prevented": 0,
            "done_by_person": 0,
            "skipped": 0,
            "declined": 0,
            "items": 0,
        }

    per_task: dict[str, dict[str, Any]] = {}
    total = empty()
    for row in rows:
        if row.get("status") == QUEUED:
            continue
        item = public_item(row)
        bucket = per_task.setdefault(row["workflow_id"], empty())
        for target in (bucket, total):
            target["items"] += 1
            if (
                item["result"] == STOPPED_BEFORE_SAVING
                and item["nothing_written"]
                and item["halt_kind"] == "wrong_record"
            ):
                target["wrong_record_prevented"] += 1
            if item["result"] == DONE_CHECKED:
                target["done_checked"] += 1
            else:
                # Every item that stopped needed a person, including one that a
                # person later ran again (the new run is its own item).
                target["needed_person"] += 1
            if item["status"] in {OPEN, TEACHING}:
                target["waiting"] += 1
            if item["status"] in {DONE_BY_PERSON, CONFIRMED_SAVED}:
                target["done_by_person"] += 1
            if item["status"] == SKIPPED:
                target["skipped"] += 1
            if item["status"] == DECLINED:
                target["declined"] += 1
    for workflow_id, bucket in per_task.items():
        baseline = baselines.get(workflow_id)
        bucket["baseline_seconds"] = baseline
        bucket["estimated_seconds_saved"] = (
            round(bucket["done_checked"] * baseline) if baseline else None
        )
    return {"total": total, "tasks": per_task}


__all__ = [
    "CHECK_RECORD",
    "CLOSED_STATUSES",
    "COMPLETED_STATUSES",
    "DID_NOT_FINISH",
    "DID_NOT_START",
    "FINISHED_UNCHECKED",
    "WorkItemError",
    "allowed_actions",
    "blocks_more_runs",
    "check_action",
    "inputs_digest",
    "item_label",
    "new_id",
    "public_item",
    "resolution_status",
    "result_for_run",
    "status_after_run",
    "summarize",
    "validate_inputs",
]
