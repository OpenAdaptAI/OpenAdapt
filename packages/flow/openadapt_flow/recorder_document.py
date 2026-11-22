"""Document ownership for deferred actions and coalescible browser batches."""

from __future__ import annotations

from typing import Any


class DocumentOwnershipError(ValueError):
    """An action cannot be coalesced without losing its document boundary."""


def same_document(pending: dict[str, Any] | None, event: dict[str, Any]) -> bool:
    """Distinguish a known boundary from absent or ambiguous ownership."""
    if pending is None:
        return True
    previous = pending.get("_oaflow_producer_doc")
    current = event.get("_oaflow_producer_doc")
    if (
        not isinstance(previous, str)
        or not previous.strip()
        or not isinstance(current, str)
        or not current.strip()
    ):
        raise DocumentOwnershipError(
            "a deferred browser action lost its producer ownership; "
            "recording stopped without complete metadata"
        )
    return previous == current


def _same_owner(documents: list[Any]) -> None:
    if (
        any(not isinstance(owner, str) or not owner.strip() for owner in documents)
        or len(set(documents)) != 1
    ):
        raise DocumentOwnershipError(
            "more than one logical action cannot be excluded because producer "
            "ownership is missing or differs; recording was refused"
        )


def validate_event_batch(batch: list[dict[str, Any]]) -> None:
    """Refuse a batch that lacks an exact frame between logical actions."""

    if len(batch) <= 1:
        return
    documents = [event.get("_oaflow_producer_doc") for event in batch]
    # A browser dialog's answer is reported by the action that raised it and
    # is part of that action, not a second one (docs/CHOICES.md, "Dialogs").
    # A native select's change is the commit of the key or click that made
    # it.
    core = [event for event in batch if event.get("kind") != "dialog"]
    # A dialog may come from the document the action loaded (a save that
    # opens the next page, which then alerts), so only the actions share an
    # owner.
    documents = [event.get("_oaflow_producer_doc") for event in core]
    if len(core) != len(batch) and len(core) <= 1:
        if core:
            _same_owner(documents)
        return
    kinds_in_order = [event.get("kind") for event in core]
    # The click that opened a native select can reach the page only when its
    # list closes, together with the committing key and the change.
    if (
        2 <= len(core) <= 3
        and kinds_in_order.count("select_choice") == 1
        and set(kinds_in_order) <= {"select_choice", "key", "click"}
        and kinds_in_order.count("click") <= 1
        and kinds_in_order.count("key") <= 1
    ):
        _same_owner(documents)
        return
    batch = core
    if (
        any(not isinstance(owner, str) or not owner.strip() for owner in documents)
        or len(set(documents)) != 1
    ):
        raise DocumentOwnershipError(
            "more than one logical action cannot be excluded because producer "
            "ownership is missing or differs; recording was refused"
        )
    kinds = {event.get("kind") for event in batch}
    if kinds == {"scroll"}:
        return
    if len(batch) == 2:
        first, second = batch
        # A double-click gesture delivers its first click and the
        # double_click marker inside one gesture window with no
        # intermediate settled frame; they merge into ONE step.
        try:
            same_point = (
                abs(int(first.get("x", 0)) - int(second.get("x", 0))) <= 5
                and abs(int(first.get("y", 0)) - int(second.get("y", 0))) <= 5
            )
        except (TypeError, ValueError):
            same_point = False
        if (
            first.get("kind") == "click"
            and second.get("kind") == "double_click"
            and same_point
        ):
            return
    if kinds == {"input"}:
        sessions = {event.get("_oaflow_input_session") for event in batch}
        fields = {event.get("field") for event in batch}
        if len(sessions) == 1 and None not in sessions and len(fields) == 1:
            return
    raise DocumentOwnershipError(
        "more than one logical browser action arrived before Flow could "
        "retain an exact intermediate frame; recording was refused ("
        + ", ".join(str(event.get("kind")) for event in batch)
        + ")"
    )
