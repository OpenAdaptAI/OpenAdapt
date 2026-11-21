"""Bounded observations of a late typed echo; this module delivers no input.

A display may paint focus before the typed value. Re-read only while the
field changed and the exact value remains unreadable. The caller retains
its original baseline, field scope and exact-value reader on every call.
A positive result has the same strength as that reader's ordinary result;
this helper adds no record-effect or identity authority.

An unreadable changed field must halt. Refocusing/select-all/retyping could
hit a different target or destroy earlier content. The caller may retry
input only when its existing browser/structural and unchanged-value guards
independently permit it. Unconfirmed input never becomes success here.
"""

from __future__ import annotations

import time
from typing import Callable

TYPED_ECHO_REREADS = 2
TYPED_ECHO_WAIT_S = 1.0


def read_typed_echo(read: Callable[[], tuple[bool, bool]]) -> tuple[bool, bool]:
    """Repeat only observations, with the original two-read/one-second bound."""
    landed, changed = read()
    rereads = 0
    while changed and not landed and rereads < TYPED_ECHO_REREADS:
        rereads += 1
        time.sleep(TYPED_ECHO_WAIT_S)
        landed, changed = read()
    return landed, changed
