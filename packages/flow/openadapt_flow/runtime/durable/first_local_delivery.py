"""Single-host exclusion for a live first delivery and local continuation."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from openadapt_flow.ir import ActionDeliveryUncertainty


FIRST_LOCAL_DELIVERY = "first_local_delivery"
FIRST_LOCAL_DELIVERY_CAUSE = "FirstLocalDeliveryWriteAhead"
_ACTIVE_LOCAL_OWNER: ContextVar[Path | None] = ContextVar(
    "openadapt_active_first_local_owner", default=None
)


class FirstLocalDeliveryHeld(RuntimeError):
    """The live delivery or its durable outcome cannot be safely advanced."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"first_local_delivery_held: {reason}")


def is_first_local_delivery(uncertainty: ActionDeliveryUncertainty | None) -> bool:
    """Identify the write-ahead pause without changing older pause digests."""

    return (
        isinstance(uncertainty, ActionDeliveryUncertainty)
        and uncertainty.operation == FIRST_LOCAL_DELIVERY
        and uncertainty.cause_type == FIRST_LOCAL_DELIVERY_CAUSE
    )


@contextmanager
def local_delivery_owner(run_dir: Path, *, create: bool = True) -> Iterator[None]:
    """Hold the same-run owner independently of the short checkpoint CAS lock.

    The operating system releases the lock if its process stops. The lock
    file is never removed: replacing it while open would split ownership.
    This is local exclusion only; the durable authority still owns proof of
    the retained state and all managed delivery decisions.
    """

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = run_dir.resolve()
        digest = hashlib.sha256(os.fsencode(directory)).hexdigest()
        path = directory.parent / f".openadapt-first-delivery-{digest}.lock"
        if create:
            directory.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError as exc:
        if create:
            raise FirstLocalDeliveryHeld("the local owner file is unavailable") from exc
        yield
        return
    except (OSError, RuntimeError) as exc:
        raise FirstLocalDeliveryHeld("the local delivery owner could not be checked") from exc
    locked = False
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("the local owner is not a regular file")
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise FirstLocalDeliveryHeld(
                "another local delivery or continuation may still be active"
            ) from exc
        token = _ACTIVE_LOCAL_OWNER.set(directory) if create else None
        try:
            yield
        finally:
            if token is not None:
                _ACTIVE_LOCAL_OWNER.reset(token)
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def require_local_delivery_idle(run_dir: Path) -> None:
    """Refuse admission while a previously claimed first leg owns delivery."""

    with local_delivery_owner(run_dir, create=False):
        pass


def require_current_local_delivery_owner(run_dir: Path) -> None:
    """Only the context that holds the local lock may arm an input edge."""

    try:
        current = run_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise FirstLocalDeliveryHeld("the exact local owner is unavailable") from exc
    if _ACTIVE_LOCAL_OWNER.get() != current:
        raise FirstLocalDeliveryHeld("the current context does not own delivery")
