"""Close invocation-owned resources without losing a primary failure."""

from __future__ import annotations

from collections.abc import Callable, Iterable


def close_owned(
    callbacks: Iterable[Callable[[], None]],
    *,
    primary: BaseException | None = None,
) -> None:
    """Attempt every close in order; a cleanup error never replaces primary.

    With no primary failure, raise the first cleanup error after trying the
    remaining owners. Callback order is explicit; shared resources must not
    be included by a caller that owns only one context.
    """
    first = primary
    for callback in callbacks:
        try:
            callback()
        except BaseException as error:
            if first is None:
                first = error
            else:
                try:
                    note = getattr(first, "add_note", None)
                    if callable(note):
                        note(f"owned cleanup also raised {type(error).__name__}")
                except BaseException:
                    pass
    if primary is None and first is not None:
        raise first
