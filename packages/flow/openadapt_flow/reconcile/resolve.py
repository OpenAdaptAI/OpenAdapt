"""One rule for which value or record is "the" one.

Every slot the reconciler judges (a record a selector picks, a value read
out of a segment or a list, a join target, a group's latest mark, a
``distinct_by`` value) is resolved the same way from all of its candidates
in the read:

- no candidate: no value (``None``);
- one distinct candidate (identical repeats collapse): that value;
- two or more distinct candidates: :class:`Ambiguous`.

Position never decides: a first or last segment, list element or record is
not more "the" one than another. A leg whose judged slot is
:class:`Ambiguous` can't be CONFIRMED (see ``reconcile._check_leg``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

__all__ = ["Ambiguous", "candidates", "is_ambiguous", "resolve"]


class Ambiguous(str):
    """Two or more distinct candidates for one slot.

    It is text so a read stays JSON-able and comparable, and the text names
    no candidate, so it equals no stored or expected value and quotes none
    into a reason.
    """

    candidates: tuple[Any, ...]

    def __new__(cls, values: Iterable[Any]) -> "Ambiguous":
        values = tuple(values)
        obj = super().__new__(cls, f"<{len(values)} conflicting values>")
        obj.candidates = values
        return obj

    def __reduce__(self) -> Any:
        return (Ambiguous, (self.candidates,))

    def __copy__(self) -> "Ambiguous":
        return self

    def __deepcopy__(self, memo: Any) -> "Ambiguous":
        return self


def _key(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def candidates(value: Any) -> tuple[Any, ...]:
    """The candidates ``value`` stands for: its own, if it is ambiguous."""

    return value.candidates if isinstance(value, Ambiguous) else (value,)


def is_ambiguous(value: Any) -> bool:
    return isinstance(value, Ambiguous)


def resolve(values: Iterable[Any]) -> Any:
    """The one value ``values`` agree on, ``None`` when there is none, or
    :class:`Ambiguous` when they hold two or more distinct values. A
    ``None`` among other values is a distinct candidate (a record without
    the value is not one with it)."""

    seen: dict[str, Any] = {}
    for value in values:
        for one in candidates(value):
            seen.setdefault(_key(one), one)
    if not seen:
        return None
    if len(seen) == 1:
        return next(iter(seen.values()))
    return Ambiguous(seen.values())
