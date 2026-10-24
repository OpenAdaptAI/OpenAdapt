"""The stop-review queue: every stop as one card a person answers in one tap.

A run that stops for a person used to mean minutes: open the item, open the
app, look the record up, compare, then pick an action. A card puts the
question, the evidence side by side (the item's own values next to the record
or the form, with the differences marked) and the choices with what each one
does, in one place.

The queue presents and collects; it never decides. Each card's id is a digest
of everything it shows. An answer names the card ids the person saw, and the
dispatcher rebuilds the cards from the stores before it acts: a card that
changed, expired or is gone, or a choice the card doesn't offer, changes
nothing.

Cards come from three places:

* ``approve_save``: a run waiting before a save (Flow's ``each_item`` gate).
  The answer goes to Flow's ``item-approval`` with Desktop's approver key
  (Touch ID on a Mac), as Run it always has.
* ``same_record``, ``confirm_save``, ``halted``: stopped items in Needs you.
  The answer is the item's existing action (skip, done by hand, confirmed
  saved, run again, ...), with the same evidence checks. Desktop signs a
  decision receipt over the card ids and the choice with the approver key
  first, so every answer names a registered approver.
* ``rearm_problems_only``: a task whose "only ask about problems" switch went
  off, when a signed re-arm is available.

Similar cards share a ``batch_key``. A batch is answered with one choice and
one signature, and each card in it is still checked on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

CARD_SCHEMA = "openadapt.desktop-stop-card/v1"
DECISION_SCHEMA = "openadapt.desktop-stop-decision/v1"
#: What the approver key signs for a queue answer (other than a save
#: approval, which Flow signs as its own answer).
DECISION_DOMAIN = b"openadapt-desktop.stop-decision/v1\x00"
#: The most cards one batch answer covers.
MAX_BATCH = 25

#: A duplicate check that found a possible match, as Flow words the failure.
_DUPLICATE_MARKERS = ("no_duplicate_candidate", "possible duplicate", "duplicate")


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    #: What happens, in the words the card shows next to the button.
    consequence: str
    #: The work-item action, the approval decision, or "none" (changes nothing).
    action: str
    #: The decline reason an approval choice carries.
    reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "consequence": self.consequence,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _label(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


# -- choices -------------------------------------------------------------------------

_LEAVE = Choice(
    "leave",
    "Not sure, leave it",
    "Nothing changes. The stop stays in the queue.",
    "none",
)

_ITEM_CHOICES: dict[str, dict[str, Choice]] = {
    "same_record": {
        "skip": Choice(
            "same",
            "Same record: don't add it",
            "Nothing is saved. The item closes as skipped.",
            "skip",
        ),
        "done_by_person": Choice(
            "different_by_hand",
            "Different record: I entered it by hand",
            "The item closes as done by a person. OpenAdapt saves nothing.",
            "done_by_person",
        ),
        "decline": Choice(
            "decline",
            "This item shouldn't be done",
            "Nothing is saved. The item closes as declined.",
            "decline",
        ),
    },
    "confirm_save": {
        "confirmed_saved": Choice(
            "saved",
            "I checked: it saved correctly",
            "The item closes as saved.",
            "confirmed_saved",
        ),
        "retry_after_check": Choice(
            "not_saved_retry",
            "I checked: nothing saved, run it again",
            "OpenAdapt runs the item again, with every check.",
            "retry_after_check",
        ),
        "done_by_person": Choice(
            "fixed_by_hand",
            "I fixed it by hand",
            "The item closes as done by a person.",
            "done_by_person",
        ),
    },
    "halted": {
        "retry": Choice(
            "retry",
            "Run it again",
            "OpenAdapt runs the item again, with every check. Nothing was saved.",
            "retry",
        ),
        "skip": Choice(
            "skip",
            "Skip it",
            "Nothing is saved. The item closes as skipped.",
            "skip",
        ),
        "decline": Choice(
            "decline",
            "This item shouldn't be done",
            "Nothing is saved. The item closes as declined.",
            "decline",
        ),
        "done_by_person": Choice(
            "by_hand",
            "I did it by hand",
            "The item closes as done by a person.",
            "done_by_person",
        ),
    },
}

_APPROVAL_CHOICES = (
    Choice(
        "approve",
        "Approve and save",
        "OpenAdapt checks the form again, saves, then checks the record.",
        "approve",
    ),
    Choice(
        "wrong_values",
        "Don't save: wrong values",
        "Nothing is saved. The item stops for you.",
        "decline",
        "wrong_values",
    ),
    Choice(
        "wrong_record",
        "Don't save: wrong record",
        "Nothing is saved. The item stops for you.",
        "decline",
        "wrong_record",
    ),
    Choice(
        "screen_problem",
        "Don't save: something's wrong on screen",
        "Nothing is saved. The item stops for you.",
        "decline",
        "screen_problem",
    ),
)

#: Actions that also mean "I looked at the record": a card offers them only
#: as a statement of what the person found there.
RECORD_CHECK_ACTIONS = frozenset({"confirmed_saved", "retry_after_check", "done_by_person"})


# -- cards ---------------------------------------------------------------------------


def _is_duplicate_stop(item: dict) -> bool:
    reason = _norm(item.get("halt_reason"))
    return item.get("halt_kind") == "wrong_record" or any(
        marker in reason for marker in _DUPLICATE_MARKERS
    )


def item_kind(item: dict) -> str | None:
    """Which card an open Needs-you item becomes, or None."""

    actions = item.get("actions") or []
    if not actions or item.get("source") not in {None, "item"}:
        return None
    if not item.get("nothing_written"):
        return "confirm_save"
    if _is_duplicate_stop(item):
        return "same_record"
    return "halted"


def _differences(source: list[dict], record: list[dict]) -> list[dict]:
    on_file = {row["field"]: row["value"] for row in record}
    out = []
    for row in source:
        if row["field"] not in on_file:
            continue
        same = _norm(row["value"]) == _norm(on_file[row["field"]])
        out.append(
            {
                "field": row["field"],
                "label": _label(row["field"]),
                "source": row["value"],
                "record": on_file[row["field"]],
                "same": same,
            }
        )
    return out


def _item_evidence(item: dict, kind: str) -> dict[str, Any]:
    inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
    source = [
        {"field": str(k), "label": _label(str(k)), "value": str(v)}
        for k, v in sorted(inputs.items())
    ]
    record: list[dict] = []
    for diff in item.get("field_diffs") or []:
        record.append({"field": diff["field"], "value": diff.get("on_file", "")})
        if not any(row["field"] == diff["field"] for row in source):
            source.append(
                {
                    "field": diff["field"],
                    "label": _label(diff["field"]),
                    "value": diff.get("typed", ""),
                }
            )
    candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
    evidence: dict[str, Any] = {
        "source": source,
        "record": record,
        "differences": _differences(source, record),
        # Rows the app listed as possible matches, when Flow recorded them.
        "candidates": [str(row)[:300] for row in candidates[:10]],
        "reason": str(item.get("halt_reason") or "")[:500],
        "step": str(item.get("halt_step") or "")[:300],
        "expected": str(item.get("expected") or "")[:300],
        "observed": str(item.get("observed") or "")[:300],
        "screenshot": {"item_id": item["item_id"]} if item.get("run_id") else None,
    }
    return evidence


_QUESTIONS = {
    "same_record": "Same record?",
    "confirm_save": "Did it save?",
    "halted": "What should happen to this item?",
}

_SUMMARIES = {
    "same_record": (
        "OpenAdapt stopped before saving: the app may already have this record. "
        "Compare the item with what the app shows."
    ),
    "confirm_save": (
        "The save may have gone through, but OpenAdapt couldn't confirm it. "
        "Look at the record, then say what you found."
    ),
    "halted": "OpenAdapt stopped before saving. Nothing was written.",
}


def _seal(card: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in card.items() if k != "card_id"}
    return {**body, "card_id": _digest(body)}


def card_for_item(item: dict) -> dict[str, Any] | None:
    """One Needs-you item as a card, or None when it isn't a stop."""

    kind = item_kind(item)
    if kind is None:
        return None
    table = _ITEM_CHOICES[kind]
    choices = [table[a] for a in item["actions"] if a in table]
    if not choices:
        return None
    evidence = _item_evidence(item, kind)
    question = _QUESTIONS[kind]
    if kind == "same_record" and any(
        "dob" in row["field"].lower() or "name" in row["field"].lower()
        for row in evidence["source"]
    ):
        question = "Same person?"
    card = {
        "schema": CARD_SCHEMA,
        "kind": kind,
        "source": {"type": "item", "id": item["item_id"], "run_id": item.get("run_id")},
        "workflow_id": item.get("workflow_id") or "",
        "task_name": item.get("task_name") or "",
        "label": item.get("label") or "",
        "question": question,
        "summary": _SUMMARIES[kind],
        "evidence": evidence,
        "choices": [c.public() for c in [*choices, _LEAVE]],
        "expires_at": None,
        "created_at": item.get("created_at"),
        "batch_key": f"{kind}:{item.get('workflow_id') or ''}:{_reason_class(item)}",
        # The status the card was built from: an answer after it changed
        # finds a different card id and does nothing.
        "status": item.get("status"),
    }
    return _seal(card)


def _reason_class(item: dict) -> str:
    if _is_duplicate_stop(item):
        return "duplicate"
    return str(item.get("halt_kind") or "other")


def card_for_approval(state: dict) -> dict[str, Any] | None:
    """A run waiting before a save (``get_item_approval``) as a card."""

    request = state.get("request") if isinstance(state, dict) else None
    if not state.get("waiting") or not isinstance(request, dict):
        return None
    source = [
        {
            "field": str(row.get("name")),
            "label": _label(str(row.get("name"))),
            "value": str(row.get("value") or ""),
        }
        for row in request.get("values") or []
        if isinstance(row, dict)
    ]
    can_approve = bool(request.get("screenshot"))
    choices = [c for c in _APPROVAL_CHOICES if can_approve or c.action != "approve"]
    card = {
        "schema": CARD_SCHEMA,
        "kind": "approve_save",
        "source": {
            "type": "approval",
            "id": request.get("request_id"),
            "request_sha256": request.get("request_sha256"),
            "workflow_id": state.get("workflow_id"),
        },
        "workflow_id": state.get("workflow_id") or "",
        "task_name": state.get("task_name") or "",
        "label": "Practice item" if state.get("kind") == "practice" else "",
        "question": "Approve this save?",
        "summary": (
            "OpenAdapt filled in the form and stopped before saving. Compare the "
            "item's values with the form."
        ),
        "evidence": {
            "source": source,
            "record": [],
            "differences": [],
            "candidates": [],
            "reason": "",
            "step": str(request.get("step_intent") or "")[:300],
            "expected": "",
            "observed": "",
            # The form about to be saved: the dispatcher's approval screenshot.
            "screenshot": {"approval": request.get("request_id")} if can_approve else None,
        },
        "choices": [c.public() for c in [*choices, _LEAVE]],
        "expires_at": request.get("answer_by"),
        "created_at": None,
        # One run waits at a time; save approvals are answered one by one,
        # each with its own signature.
        "batch_key": None,
        "status": "waiting",
    }
    return _seal(card)


def card_for_rearm(switch: dict) -> dict[str, Any] | None:
    """A task whose problems-only switch went off, when it can be re-armed."""

    if switch.get("state") != "off" or not switch.get("rearm_available"):
        return None
    card = {
        "schema": CARD_SCHEMA,
        "kind": "rearm_problems_only",
        "source": {"type": "switch", "id": switch["workflow_id"]},
        "workflow_id": switch["workflow_id"],
        "task_name": switch.get("task_name") or "",
        "label": "",
        "question": 'Turn "only ask about problems" back on?',
        "summary": (
            "It went off, so every save of this task waits for a person. "
            "Turn it back on only if the reason it went off is fixed."
        ),
        "evidence": {
            "source": [],
            "record": [],
            "differences": [],
            "candidates": [],
            "reason": str(switch.get("off_reason") or ""),
            "step": str(switch.get("off_step") or ""),
            "expected": "",
            "observed": "",
            "screenshot": None,
            "off_at": switch.get("off_at"),
        },
        "choices": [
            Choice(
                "rearm",
                "Turn it back on",
                "Saves without a problem go through without asking again.",
                "rearm",
            ).public(),
            _LEAVE.public(),
        ],
        "expires_at": None,
        "created_at": switch.get("off_at"),
        "batch_key": None,
        "status": "off",
    }
    return _seal(card)


def choice_for(card: dict, choice_id: str) -> Choice | None:
    """The full choice behind a card's button, or None if it doesn't offer it."""

    if choice_id not in {c["id"] for c in card.get("choices") or []}:
        return None
    if choice_id == _LEAVE.id:
        return _LEAVE
    if card["kind"] == "approve_save":
        return next((c for c in _APPROVAL_CHOICES if c.id == choice_id), None)
    if card["kind"] == "rearm_problems_only":
        return Choice("rearm", "", "", "rearm") if choice_id == "rearm" else None
    table = _ITEM_CHOICES.get(card["kind"], {})
    return next((c for c in table.values() if c.id == choice_id), None)


def expired(card: dict, *, now: datetime | None = None) -> bool:
    deadline = card.get("expires_at")
    if not deadline:
        return False
    try:
        at = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except ValueError:
        return True  # unreadable deadline: treat as expired, change nothing
    if at.tzinfo is None:
        return True
    return (now or datetime.now(timezone.utc)) >= at


def decision_message(card_ids: Iterable[str], choice_id: str, *, decided_at: str) -> bytes:
    """The bytes the approver key signs for one (batch) answer."""

    return DECISION_DOMAIN + _canonical(
        {
            "schema": DECISION_SCHEMA,
            "card_ids": sorted(card_ids),
            "choice": choice_id,
            "decided_at": decided_at,
        }
    )


def sort_cards(cards: list[dict]) -> list[dict]:
    """Waiting saves first (they expire), then the oldest stop first."""

    order = {"approve_save": 0, "confirm_save": 1, "same_record": 2, "halted": 3}
    return sorted(
        cards,
        key=lambda c: (order.get(c["kind"], 4), str(c.get("created_at") or "")),
    )


__all__ = [
    "CARD_SCHEMA",
    "DECISION_DOMAIN",
    "DECISION_SCHEMA",
    "MAX_BATCH",
    "RECORD_CHECK_ACTIONS",
    "Choice",
    "card_for_approval",
    "card_for_item",
    "card_for_rearm",
    "choice_for",
    "decision_message",
    "expired",
    "item_kind",
    "sort_cards",
]
