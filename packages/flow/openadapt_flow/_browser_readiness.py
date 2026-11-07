"""Bounded animation-frame liveness at browser acquisition.

This checks rendering startup, not actionability, qualification or outcomes.
No timer, screenshot or page value can substitute for advancing RAF timestamps.
"""

from __future__ import annotations

import time
from typing import Any

READINESS_TIMEOUT_MS = 1000
READINESS_CLEANUP_TIMEOUT_MS = 250
_OWNED_BROWSER = object()

_START = """() => {
    const state = {frames: 0, first: -1, last: -1, raf: 0,
                   invalid: false, cancelled: false};
    const tick = timestamp => {
        if (state.cancelled) return;
        if (!Number.isFinite(timestamp) ||
            (state.frames > 0 && timestamp <= state.last)) {
            state.invalid = true;
            return;
        }
        if (state.frames === 0) state.first = timestamp;
        state.last = timestamp;
        state.frames += 1;
        if (state.frames < 2) state.raf = requestAnimationFrame(tick);
    };
    state.raf = requestAnimationFrame(tick);
    return state;
}"""
_PROGRESS = """state => !state.cancelled && !state.invalid &&
    state.frames === 2 && state.last > state.first"""
_CANCEL = """state => {
    state.cancelled = true;
    cancelAnimationFrame(state.raf);
    return true;
}"""


class BrowserReadinessError(RuntimeError):
    """A fixed, value-free reason for refusing browser startup."""


def launch_rendering_ready(chromium: Any, **options: Any) -> Any:
    """Retain a caller's exact launch options and check its owned browser."""
    browser = chromium.launch(**options)
    require_rendering_ready(browser)
    return browser


def _check_page_rendering(page: Any) -> None:
    # Both scheduling and observation share one host deadline. Numeric polling
    # observes only the private RAF state; polling timers never grant a pass.
    deadline = time.monotonic() + READINESS_TIMEOUT_MS / 1000
    handles: list[Any] = []
    state = None
    problem = None

    def remaining() -> float:
        milliseconds = (deadline - time.monotonic()) * 1000
        if milliseconds <= 0:
            raise BrowserReadinessError("browser_rendering_unavailable")
        return milliseconds

    try:
        state = page.wait_for_function(_START, polling=10, timeout=remaining())
        handles.append(state)
        observed = page.wait_for_function(
            _PROGRESS, arg=state, polling=10, timeout=remaining()
        )
        handles.append(observed)
    except Exception:
        problem = "browser_rendering_unavailable"
    finally:
        if state is not None:
            try:
                cancelled = page.wait_for_function(
                    _CANCEL,
                    arg=state,
                    polling=10,
                    timeout=READINESS_CLEANUP_TIMEOUT_MS,
                )
                handles.append(cancelled)
            except Exception:
                problem = "browser_readiness_unavailable"
        for handle in reversed(handles):
            try:
                handle.dispose()
            except Exception:
                problem = "browser_readiness_unavailable"
    if problem is not None:
        raise BrowserReadinessError(problem) from None


def require_rendering_ready(
    browser: Any, *, attached_page: Any = _OWNED_BROWSER
) -> None:
    """Require RAF progress before application navigation or input.

    Fresh launches use a new blank context, closed on every outcome. Failure
    also closes that owned browser; its caller still stops the driver.

    An attach caller must first select its explicitly authorized target page.
    That exact page is probed without navigation, focus, DOM/global writes or
    opening a window. Only a private JSHandle and at most two RAF callbacks are
    created; cleanup cancels its recorded callback and disposes the handles.
    Unsupported/closed page association refuses without closing user resources.
    A background page that does not render refuses; it is never foregrounded.

    Scheduling/observation share a 1000 ms host deadline; cancellation gets a
    separate 250 ms host deadline. Closing the owned context cancels pending
    script. If an attached transport disappears, cleanup is unproved and the
    check refuses; the finite callback chain cannot become a polling observer.
    The check uses the page's main-world RAF API, which an attached application
    can replace. Callback progress is not a native rendering attestation and
    grants no qualification, action or outcome-verification authority.
    """
    context = None
    ready = False
    owned = attached_page is _OWNED_BROWSER
    try:
        if not browser.is_connected():
            raise BrowserReadinessError("browser_readiness_unavailable")
        if owned:
            context = browser.new_context(service_workers="block")
            page = context.new_page()
        else:
            page = attached_page
            if page is None or page.is_closed() or page.context.browser is not browser:
                raise BrowserReadinessError("browser_readiness_unavailable")
        _check_page_rendering(page)
        ready = True
    except BrowserReadinessError:
        raise
    except Exception:
        raise BrowserReadinessError("browser_readiness_unavailable") from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            ready = False
            raise BrowserReadinessError("browser_readiness_unavailable") from None
        finally:
            if not ready and owned:
                try:
                    browser.close()
                except Exception:
                    # Keep the fixed refusal rather than protocol/page values.
                    pass
