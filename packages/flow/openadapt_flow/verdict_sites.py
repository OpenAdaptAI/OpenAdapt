"""Mark a function that turns evidence into a verdict, so the fail-open
harness tests it.

``@verdict_site("verify_run.aggregate", classes="C2")`` does nothing at run
time except record the site in :data:`SITES`. ``tests/failopen`` keeps one
spec per site and runs the generic mutations against it; a meta-test fails
when a decorated site has no spec. ``scripts/check_verdict_folds.py`` exempts
a decorated function from its count, so decorating and registering is how
new verdict code is added. See ``docs/FAIL_CLOSED.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

__all__ = ["SITES", "verdict_site"]

F = TypeVar("F", bound=Callable[..., object])

#: ``{site id: the fail-open classes it is tested for}``, filled at import.
SITES: dict[str, str] = {}
#: ``{site id: module.qualname}``: one id names one function.
_WHERE: dict[str, str] = {}


def verdict_site(id: str, *, classes: str) -> Callable[[F], F]:
    """Register ``id`` and return the function unchanged."""

    def mark(fn: F) -> F:
        where = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}"
        if _WHERE.get(id, where) != where or SITES.get(id, classes) != classes:
            raise ValueError(f"verdict site {id!r} registered twice")
        SITES[id] = classes
        _WHERE[id] = where
        setattr(fn, "__verdict_site__", id)
        return fn

    return mark
