"""Owned-process startup checks for the native census test fixture."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, NoReturn, Optional


class NativeAppStartupError(AssertionError):
    """The owned fixture did not prove that its rendered window is ready."""


def _tail(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode("utf-8", errors="replace")
    except OSError as exc:
        return f"<unavailable: {type(exc).__name__}>"


def wait_for_rendered_items(
    proc: subprocess.Popen,
    ready_path: Path,
    expected_rows: int,
    find_items: Callable[[int], Optional[list[Any]]],
    stdout_path: Path,
    stderr_path: Path,
    *,
    timeout_s: float,
) -> list[Any]:
    """Wait for the owned render signal and UIA items within one deadline."""
    started = monotonic()
    deadline = started + timeout_s
    phase = "ContentRendered signal"
    signal: Optional[dict[str, Any]] = None

    def fail(reason: str) -> NoReturn:
        raise NativeAppStartupError(
            f"native fixture startup: {reason}; pid={proc.pid}; "
            f"exit={proc.poll()}; phase={phase}; "
            f"elapsed={monotonic() - started:.3f}s; "
            f"ready={ready_path.exists()}; "
            f"stdout_tail={_tail(stdout_path)!r}; "
            f"stderr_tail={_tail(stderr_path)!r}"
        )

    while True:
        if proc.poll() is not None:
            fail("owned child exited")
        if monotonic() >= deadline:
            fail("overall startup deadline exceeded")
        if signal is None and ready_path.exists():
            try:
                with ready_path.open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError("oversized signal")
                candidate = json.loads(raw)
                if (
                    not isinstance(candidate, dict)
                    or set(candidate) != {"event", "pid", "hwnd", "rows"}
                    or candidate["event"] != "content_rendered"
                    or type(candidate["pid"]) is not int
                    or candidate["pid"] != proc.pid
                    or type(candidate["hwnd"]) is not int
                    or candidate["hwnd"] <= 0
                    or type(candidate["rows"]) is not int
                    or candidate["rows"] != expected_rows
                ):
                    raise ValueError("signal does not identify the owned fixture")
                signal = candidate
            except (OSError, ValueError, TypeError):
                fail("invalid ContentRendered signal")
        if signal is not None:
            phase = "owned window UIA items"
            try:
                items = find_items(signal["hwnd"])
            except Exception as exc:
                fail(f"UIA discovery failed ({type(exc).__name__}: {str(exc)[:400]})")
            if proc.poll() is not None:
                fail("owned child exited during UIA discovery")
            if monotonic() >= deadline:
                fail("overall startup deadline exceeded")
            if items is not None and len(items) == expected_rows:
                return items
        sleep(min(0.1, max(0.0, deadline - monotonic())))


def stop_owned_process(proc: subprocess.Popen, timeout_s: float = 5.0) -> None:
    """Reap only the process returned by this fixture's Popen call."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_s)
