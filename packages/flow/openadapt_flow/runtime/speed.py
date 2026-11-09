"""Named speed profiles: one switch for the settings that trade time for margin.

The engine's strict improvements (DOM-first checks, fewer round trips, the
off-screen scroll probe) are always on. The settings below have a tradeoff, so
they are grouped under a name instead of changing by default:

``standard`` (default)
    Every key is typed as a key event; a page counts as settled after 100 ms
    of quiet; OCR uses onnxruntime's default threads.
``fast``
    Ordinary text fields get their text in one insert (their ``input`` and
    ``change`` events still fire; per-key handlers don't); a page counts as
    settled after 50 ms of quiet; OCR uses 2 threads so parallel workers don't
    oversubscribe the CPU.

Every check runs in both: identity, input read-back, postconditions and the
system-of-record read-back. See docs/THROUGHPUT.md.

The settings are process-wide (environment variables and one module value),
so apply a profile before the run starts, once per process.
"""

from __future__ import annotations

import os
from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "standard": {"native_fill": False, "page_quiet_ms": 100, "ocr_threads": None},
    "fast": {"native_fill": True, "page_quiet_ms": 50, "ocr_threads": 2},
}

_ENV_NATIVE_FILL = "OPENADAPT_FLOW_NATIVE_FILL"
_ENV_OCR_THREADS = "OPENADAPT_FLOW_OCR_THREADS"
_ENV_PROFILE = "OPENADAPT_FLOW_SPEED"


def apply_speed_profile(name: str) -> dict[str, Any]:
    """Apply profile ``name`` to this process and return its settings.

    Call before any backend or OCR engine is created: the OCR thread count is
    read once, when the engine is first built.
    """

    if name not in PROFILES:
        raise ValueError(f"unknown speed profile {name!r}; use {', '.join(PROFILES)}")
    from openadapt_flow.vision import settle

    settings = dict(PROFILES[name])
    os.environ[_ENV_PROFILE] = name
    os.environ[_ENV_NATIVE_FILL] = "1" if settings["native_fill"] else "0"
    if settings["ocr_threads"] is None:
        os.environ.pop(_ENV_OCR_THREADS, None)
    else:
        os.environ[_ENV_OCR_THREADS] = str(settings["ocr_threads"])
    settle.PAGE_QUIET_WINDOW_S = settings["page_quiet_ms"] / 1000.0
    return {"speed_profile": name, **settings}


def current_speed_settings() -> dict[str, Any]:
    """What this process runs with now (for run metadata)."""

    from openadapt_flow.vision import settle

    window = settle.PAGE_QUIET_WINDOW_S
    return {
        "speed_profile": os.environ.get(_ENV_PROFILE, "standard"),
        "native_fill": os.environ.get(_ENV_NATIVE_FILL, "0") != "0",
        "page_quiet_ms": round(window * 1000) if window is not None else 100,
        "ocr_threads": (
            int(os.environ[_ENV_OCR_THREADS])
            if os.environ.get(_ENV_OCR_THREADS)
            else None
        ),
    }
