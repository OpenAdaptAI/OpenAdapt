"""Keep a recorded dialog answer on its original producer-document arm."""

from __future__ import annotations

from typing import Any, Callable

from openadapt_flow.runtime.step_timing import timed_span

_DIALOG_WAIT_S = 5.0


def arm_dialog(arm: Callable[..., Any], dialog: Any) -> None:
    options = {"kind": dialog.kind} if dialog.kind is not None else {}
    if dialog.message_pattern is not None:
        arm(dialog.response, None, message_pattern=dialog.message_pattern, **options)
    else:
        arm(dialog.response, dialog.message, **options)


@timed_span("postcondition")
def wait_for_dialog(_self: Any, take: Callable[..., Any], dialog: Any) -> list[Any]:
    """Wait on the existing arm; the legacy dialog argument grants no new arm."""

    seen = take(_DIALOG_WAIT_S)
    return list(seen) if isinstance(seen, list) else []
