"""Keep page JavaScript from starting workers or new windows in a guarded context.

Playwright's ``context.route`` doesn't see a shared worker's requests, and
``route_web_socket`` doesn't see a worker's WebSockets, so a worker could
send a write that no route guard sees (OpenAdaptAI/OpenAdapt#1814). This
module keeps workers and new windows from starting at all, and stops the run
if one appears anyway:

* Every document gets a ``worker-src 'none'`` Content Security Policy before
  its first script runs. The browser itself then refuses dedicated, shared,
  service, blob and module workers in every frame, and documents created
  from it (``about:blank`` and ``srcdoc`` frames) inherit the policy. The
  policy goes in through a ``<meta>`` element inserted and removed by an init
  script: the response itself is not rewritten, so the document keeps its
  network security state, and a removed policy stays in force.
* ``window.open`` is replaced, in every document, by a non-configurable
  function that opens nothing.
* A new window or tab opened any other way (a link or form with a target)
  is refused at its first request: the route aborts every request that
  doesn't come from the guarded page, so the new window's document never
  loads and none of its scripts run. Watching for the ``page`` event alone
  is not enough, because a new window's head script can run before Python
  sees the event.
* A worker target of the guarded context that still appears (seen through
  the browser's target discovery) is reported.

Each refusal goes to ``on_violation``. The caller treats any violation as a
halt: an unseen worker request must stop the run, never pass it.
:func:`contain_workers` refuses (``problem`` set) when it can't install every
layer, for example on a browser without the DevTools target domain.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: Target types that run script outside a document.
WORKER_TARGET_TYPES = frozenset({"worker", "shared_worker", "service_worker"})

_INIT_JS = """(() => {
  const report = globalThis.__openadaptWorkerViolation;
  addEventListener('securitypolicyviolation', (event) => {
    if (event.isTrusted && event.effectiveDirective === 'worker-src') {
      report().catch(() => {});
    }
  });
  const before = document.documentElement;
  const make = (n) => document.createElementNS('http://www.w3.org/1999/xhtml', n);
  try {
    const root = before || document.appendChild(make('html'));
    const head = document.head || root.appendChild(make('head'));
    const meta = make('meta');
    meta.httpEquiv = 'Content-Security-Policy';
    meta.content = "worker-src 'none'";
    head.append(meta);
    // A policy stays in force after its element is removed. Removing the
    // temporary elements lets the parser build the real document.
    if (!before) root.remove(); else meta.remove();
  } catch (e) {}
  try {
    Object.defineProperty(window, 'open', {
      value: function open() { return null; },
      writable: false, configurable: false, enumerable: true,
    });
  } catch (e) {}
})();"""

NEW_WINDOW = "the page opened a new window or tab, and it was stopped"
WORKER_STARTED = "a worker started on the page, and its requests can't be checked"
WORKER_REFUSED = "the page tried to start a worker, and the browser refused it"


@dataclass
class WorkerContainment:
    """What :func:`contain_workers` installed, and what it has seen."""

    problem: Optional[str] = None
    violations: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _report: Optional[Callable[[str], None]] = field(default=None, repr=False)

    def violate(self, reason: str) -> None:
        with self._lock:
            self.violations.append(reason)
        if self._report is not None:
            try:
                self._report(reason)
            except Exception:  # noqa: BLE001 - the violation is kept here
                pass

    @property
    def ok(self) -> bool:
        return self.problem is None and not self.violations


def contain_workers(
    page: Any, on_violation: Optional[Callable[[str], None]] = None
) -> WorkerContainment:
    """Install worker and new-window containment on ``page``'s context.

    Call it before the page loads anything: a document that loaded earlier
    has no worker policy. Register it after the caller's own
    ``context.route`` handlers, because routes run newest first: this one
    aborts requests from any other page and hands the guarded page's
    requests on with ``route.fallback()``.
    """

    state = WorkerContainment(_report=on_violation)
    context = page.context
    try:
        session = context.new_cdp_session(page)
        info = session.send("Target.getTargetInfo")
        browser_context = info["targetInfo"].get("browserContextId")
    except Exception:  # noqa: BLE001 - no target domain: refuse
        state.problem = "this browser can't report its workers, so writes can't be held"
        return state

    def on_target(event: dict[str, Any]) -> None:
        target = event.get("targetInfo") or {}
        if target.get("type") not in WORKER_TARGET_TYPES:
            return
        # A worker with no context named (a service worker left behind by
        # another context, say) can't be ruled out, so it counts too.
        if browser_context and target.get("browserContextId") not in (
            None,
            browser_context,
        ):
            return
        state.violate(WORKER_STARTED)

    def route(route: Any) -> None:
        request = route.request
        try:
            owner = request.frame.page
        except Exception:  # noqa: BLE001 - no page yet, or a worker's request
            owner = None
        if owner is not page:
            try:
                navigating = owner is not None or request.is_navigation_request()
            except Exception:  # noqa: BLE001
                navigating = False
            state.violate(NEW_WINDOW if navigating else WORKER_STARTED)
            route.abort()
            return
        route.fallback()

    def on_page(other: Any) -> None:
        if other is page:
            return
        state.violate(NEW_WINDOW)
        try:
            other.close()
        except Exception:  # noqa: BLE001 - its requests are aborted anyway
            pass

    try:
        session.on("Target.targetCreated", on_target)
        session.send("Target.setDiscoverTargets", {"discover": True})
        # A worker refused by CSP never becomes a target. Report the browser's
        # policy violation too, so callers halt on the attempt, not only on a
        # worker that escaped the policy. The init script captures the binding
        # before page script can replace it.
        context.expose_binding(
            "__openadaptWorkerViolation",
            lambda _source: state.violate(WORKER_REFUSED),
        )
        context.add_init_script(_INIT_JS)
        context.route("**/*", route)
        context.on("page", on_page)
    except Exception:  # noqa: BLE001
        state.problem = "worker containment could not be installed"
    return state
