"""Generic operating-system notifications for attended decisions.

A lock screen is a public surface.  The design's threat model requires that a
notification disclose nothing about the workflow, the person, the application,
the question, or the observed values -- only that OpenAdapt needs a decision.

The safety property here is *structural*, not editorial.  This module never
forwards upstream text.  It reads a single integer count from Flow's PHI-free
notification endpoint and renders the body from a fixed template.  There is no
code path by which a question, a value, a record identifier, a run label, or an
application name can reach a notification payload, because no string from the
run ever enters this function.
"""

from __future__ import annotations

from typing import Any, Mapping

#: The complete set of keys a notification payload may carry.  Anything else is
#: a leak by definition, and :func:`assert_generic_notification` refuses it.
NOTIFICATION_FIELDS = frozenset({"title", "body", "open_count", "route"})

#: Fixed, workflow-independent title.
NOTIFICATION_TITLE = "OpenAdapt needs a decision"

#: Where the local task shell should open.  Contains no identifiers.
NOTIFICATION_ROUTE = "/"

#: Bounded so a corrupt upstream count cannot render an unbounded string.
MAX_OPEN_COUNT = 9999


class NotificationLeak(AssertionError):
    """A notification payload carried content it must never carry."""


def _safe_count(value: Any) -> int:
    """Coerce any upstream value to a bounded, non-negative integer.

    ``bool`` is rejected explicitly: it is an ``int`` subclass and would render
    a nonsense count.  Every other non-integer becomes ``0`` rather than
    propagating upstream data into the payload.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(MAX_OPEN_COUNT, value))


def build_notification(open_count: Any) -> dict[str, Any]:
    """Build the only payload an operating-system notification may render.

    Args:
        open_count: The number of open attended decisions.  Any other type is
            coerced to ``0``; upstream strings are never consulted.

    Returns:
        ``{title, body, open_count, route}`` with the body derived solely from
        the count.
    """
    count = _safe_count(open_count)
    noun = "decision" if count == 1 else "decisions"
    body = (
        "Open OpenAdapt to review."
        if count == 0
        else f"{count} {noun} waiting on this computer. Open OpenAdapt to review."
    )
    return {
        "title": NOTIFICATION_TITLE,
        "body": body,
        "open_count": count,
        "route": NOTIFICATION_ROUTE,
    }


def notification_from_upstream(payload: Any) -> dict[str, Any]:
    """Project Flow's notification response, keeping only its integer count.

    Flow's ``/api/attention/notification`` is already PHI-free, but Desktop
    must not depend on that.  Only ``open_count`` is read; every upstream
    string -- including ``title`` and ``body`` -- is discarded.
    """
    count: Any = 0
    if isinstance(payload, Mapping):
        count = payload.get("open_count", 0)
    return build_notification(count)


def assert_generic_notification(payload: Any) -> dict[str, Any]:
    """Refuse any payload that is not the exact generic notification.

    This is the enforcement point: the dispatcher and the tests both call it,
    so a future edit that widens the payload fails loudly instead of quietly
    putting protected content on a lock screen.

    Raises:
        NotificationLeak: If the payload has unexpected keys, a non-fixed
            title or route, or a body that is not the derived template.
    """
    if not isinstance(payload, dict):
        raise NotificationLeak("A notification payload must be a dict")
    extra = set(payload) - NOTIFICATION_FIELDS
    if extra:
        raise NotificationLeak(
            f"A notification payload must not carry {sorted(extra)}; it may only "
            f"carry {sorted(NOTIFICATION_FIELDS)}."
        )
    missing = NOTIFICATION_FIELDS - set(payload)
    if missing:
        raise NotificationLeak(f"A notification payload is missing {sorted(missing)}")
    if payload["title"] != NOTIFICATION_TITLE:
        raise NotificationLeak("A notification title must be the fixed generic title")
    if payload["route"] != NOTIFICATION_ROUTE:
        raise NotificationLeak("A notification route must not carry an identifier")
    expected = build_notification(payload["open_count"])
    if payload["body"] != expected["body"]:
        raise NotificationLeak(
            "A notification body must be derived from the open count alone"
        )
    return payload
