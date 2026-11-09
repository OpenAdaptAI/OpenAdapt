"""An exclusive cross-process file lock for small fleet state files.

The same construction as ``CheckpointStore.state_lock`` in
:mod:`openadapt_flow.runtime.durable.checkpoint`: ``flock`` on POSIX,
``msvcrt.locking`` on Windows, on a separate lock file next to the state.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) while inside."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_LOCK,  # type: ignore[attr-defined]
                1,
            )
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
