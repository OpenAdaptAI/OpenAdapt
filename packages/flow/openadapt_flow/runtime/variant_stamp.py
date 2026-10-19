"""Carry the variant router's choice into the run report it starts.

The router (:mod:`openadapt_flow.runtime.variant_router`) runs a variant
through the ordinary governed ``run`` path. It does not reach into the
replayer; it sets this context for the duration of the child run, and the
replayer copies it into ``RunReport.variant_route`` when it finalizes the
report. Outside a routed run the context is empty and reports are unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from openadapt_flow.ir import VariantRouteStamp

_ACTIVE: ContextVar[Optional[VariantRouteStamp]] = ContextVar(
    "openadapt_variant_route", default=None
)


def current_variant_route() -> Optional[VariantRouteStamp]:
    """The stamp of the routed run in progress, or ``None``."""

    return _ACTIVE.get()


@contextmanager
def variant_route_scope(stamp: VariantRouteStamp) -> Iterator[VariantRouteStamp]:
    """Make ``stamp`` the active route for the enclosed child run."""

    token = _ACTIVE.set(stamp)
    try:
        yield stamp
    finally:
        _ACTIVE.reset(token)
