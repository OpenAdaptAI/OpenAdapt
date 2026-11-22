"""Gate every tab, popup, context and worker of the shadow browser.

The candidate gets the browser-wide DevTools endpoint, so it can open a
second tab (``window.open``, a ``target=_blank`` form, ``Target.createTarget``),
a new browser context, or a worker, and send a write from there. The
per-tab hold (:class:`~openadapt_flow.agent_guard.cdp.CdpGuardPage`) sees
only the guarded tab. :class:`TargetGate` closes the rest:

1. **A browser-level request hold.** ``Fetch.enable`` on the browser target
   pauses every request of every other target (new tabs and popups, pages in
   new browser contexts, service workers and shared workers) before it is
   sent. It is on before the candidate starts, so a target the candidate
   creates and drives at once (``Target.createTarget``, then
   ``Runtime.evaluate`` on it) is held from its first request. Each paused
   request goes to the same :class:`~openadapt_flow.agent_guard.shadow_hold.ShadowHoldGuard`
   as the tab's own: reads to the app go on, other origins are refused, and
   a write is held and refused. Dedicated workers of the guarded tab are held
   by the tab's own session.
2. **Auto-attach.** ``Target.setAutoAttach`` with ``waitForDebuggerOnStart``
   and ``flatten`` on the browser target: every new target is paused until
   the gate has seen it, then resumed. The gate keeps an inventory (types
   only, no URLs) and refuses to resume a target type it has not shown to be
   held (anything other than a page, an iframe, a worker, a service worker or
   a shared worker). Auto-attach alone does not stop a write: the candidate
   can drive a paused target over its own session before the gate resumes it,
   which is why (1) is the hold.

What fails the case (``extra_target``, counted in ``failed_scope``): a write
sent from any target other than the guarded tab (it is held, but the
identity check reads the guarded tab, so the write can't be checked), a
target the gate would not resume, or a gate that lost its connection (once
it is gone, Chromium lets paused requests go on). Reads in a second tab are
not a failure.

After :meth:`TargetGate.close_hold` the gate blocks every paused request on
its own thread until the browser is closed; it never turns the hold off, so
a page timer can't send once the runner stops deciding.
"""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
from typing import Any, Optional

from openadapt_flow.agent_guard.commit_guard import BLOCK_STATUS, GUARD_HEADER
from openadapt_flow.agent_guard.requests import HeldRequest
from openadapt_flow.readiness.cdp_wire import CdpError, CdpWire, browser_ws_url

__all__ = [
    "EXTRA_PREFIX",
    "GATED_TYPES",
    "RoutedPage",
    "TargetGate",
    "is_extra_request",
]

#: Request ids of the browser-level hold carry this prefix in the guard.
EXTRA_PREFIX = "gate:"
#: Target types whose requests the browser-level hold is shown to pause.
GATED_TYPES = frozenset({"page", "iframe", "worker", "service_worker", "shared_worker"})


def is_extra_request(request_id: str) -> bool:
    return request_id.startswith(EXTRA_PREFIX)


class TargetGate:
    """The browser-level hold and target inventory (see the module docstring)."""

    def __init__(self, cdp_http_endpoint: str, *, main_target_id: str) -> None:
        self.endpoint = cdp_http_endpoint
        self.main_target_id = main_target_id
        self._paused: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._open: dict[str, str] = {}
        self._lock = threading.Lock()
        self._closing = False
        self._baseline: set[str] = set()
        self._started = False
        #: New targets, by type (no URLs: they can carry record ids).
        self.extra_targets: dict[str, int] = {}
        #: Targets the gate would not resume, by type.
        self.ungated: dict[str, int] = {}
        self.errors: list[str] = []
        self.wire: Optional[CdpWire] = None

    # -- start ------------------------------------------------------------------

    def start(self) -> "TargetGate":
        """Turn the hold on; raises :class:`CdpError` when it can't be."""

        self.wire = CdpWire(browser_ws_url(self.endpoint), self._on_event)
        existing = self.wire.send("Target.getTargets", {}).get("targetInfos") or []
        self._baseline = {str(t.get("targetId")) for t in existing}
        self.wire.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
        )
        self.wire.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )
        self._started = True
        return self

    # -- events (the wire's dispatcher thread) ---------------------------------------

    def _on_event(
        self, method: str, params: dict[str, Any], session_id: Optional[str]
    ) -> None:
        if method == "Fetch.requestPaused" and session_id is None:
            if self._closing:
                self._fail(str(params.get("requestId") or ""))
                return
            self._paused.put(params)
            return
        if method == "Target.attachedToTarget":
            self._on_attached(params)

    def _on_attached(self, params: dict[str, Any]) -> None:
        info = dict(params.get("targetInfo") or {})
        target_id = str(info.get("targetId") or "")
        kind = str(info.get("type") or "unknown")
        child = str(params.get("sessionId") or "")
        if target_id == self.main_target_id or target_id in self._baseline:
            return
        with self._lock:
            self.extra_targets[kind] = self.extra_targets.get(kind, 0) + 1
            if kind not in GATED_TYPES or self._closing:
                # Left paused: it never runs, and the case fails closed.
                self.ungated[kind] = self.ungated.get(kind, 0) + 1
                return
        if params.get("waitingForDebugger") and self.wire is not None:
            try:
                self.wire.send_nowait(
                    "Runtime.runIfWaitingForDebugger", {}, session_id=child
                )
            except CdpError as exc:
                self.errors.append(f"resume: {exc}")

    # -- the runner's loop --------------------------------------------------------------

    def drain(self, guard: Any) -> int:
        """Hand the browser-level paused requests to ``guard``."""

        decided = 0
        for _ in range(self._paused.qsize()):  # only what was queued on entry
            try:
                params = self._paused.get_nowait()
            except queue.Empty:
                break
            raw_id = str(params.get("requestId") or "")
            held = dataclasses.replace(
                HeldRequest.from_cdp(params), request_id=EXTRA_PREFIX + raw_id
            )
            with self._lock:
                self._open[held.request_id] = raw_id
            try:
                guard.on_request(held)
            except Exception:  # noqa: BLE001 - fail closed on any guard error
                self.block_request(held.request_id, code="guard_error")
                raise
            decided += 1
        return decided

    @property
    def lost(self) -> bool:
        """The hold can no longer be trusted (its connection closed or erred)."""

        wire = self.wire
        return bool(
            not self._started
            or wire is None
            or (wire.closed.is_set() and not self._closing)
            or wire.error
        )

    def status(self) -> dict[str, Any]:
        """What the gate saw, without URLs or values."""

        return {
            "extra_targets": dict(self.extra_targets),
            "ungated_targets": dict(self.ungated),
            "gate_lost": self.lost,
            "errors": list(self.errors)
            + ([self.wire.error] if self.wire and self.wire.error else []),
        }

    # -- answers ------------------------------------------------------------------------

    def continue_request(self, request_id: str) -> None:
        with self._lock:
            raw = self._open.pop(request_id, None)
        if raw is None or self.wire is None:
            return
        try:
            self.wire.send("Fetch.continueRequest", {"requestId": raw})
        except CdpError:
            pass  # the page cancelled it

    def block_request(
        self,
        request_id: str,
        *,
        status: int = BLOCK_STATUS,
        body: Optional[str] = None,
        content_type: str = "application/json",
        headers: Optional[dict[str, str]] = None,
        code: str = "guard_stopped",
    ) -> None:
        with self._lock:
            raw = self._open.pop(request_id, None)
        if raw is None:
            return
        import base64

        body = (
            body
            if body is not None
            else json.dumps({"openadapt_guard": {"blocked": True, "code": code}})
        )
        response_headers = [{"name": "Content-Type", "value": content_type}] + [
            {"name": k, "value": v}
            for k, v in (headers or {GUARD_HEADER: f"blocked; code={code}"}).items()
        ]
        try:
            assert self.wire is not None
            self.wire.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": raw,
                    "responseCode": status,
                    "responseHeaders": response_headers,
                    "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                },
            )
        except (CdpError, AssertionError):
            self._fail(raw)

    def _fail(self, raw_id: str) -> None:
        if self.wire is None or not raw_id:
            return
        try:
            self.wire.send_nowait(
                "Fetch.failRequest",
                {"requestId": raw_id, "errorReason": "BlockedByClient"},
            )
        except CdpError:
            pass

    # -- stop ---------------------------------------------------------------------------

    def close_hold(self) -> None:
        """Stop deciding: block every open and later request until :meth:`close`."""

        self._closing = True
        with self._lock:
            open_ids = list(self._open)
        for request_id in open_ids:
            self.block_request(request_id)
        while True:
            try:
                params = self._paused.get_nowait()
            except queue.Empty:
                break
            self._fail(str(params.get("requestId") or ""))

    def close(self) -> None:
        """Close the connection (after the browser is closed)."""

        self._closing = True
        if self.wire is not None:
            self.wire.close()


class RoutedPage:
    """The guard's page: the tab's own requests to :class:`CdpGuardPage`, the
    browser-level hold's to the :class:`TargetGate`."""

    def __init__(self, tab_page: Any, gate: Optional[TargetGate]) -> None:
        self._tab = tab_page
        self._gate = gate

    def continue_request(self, request_id: str) -> None:
        if self._gate is not None and is_extra_request(request_id):
            self._gate.continue_request(request_id)
        else:
            self._tab.continue_request(request_id)

    def block_request(self, request_id: str, **kwargs: Any) -> None:
        if self._gate is not None and is_extra_request(request_id):
            self._gate.block_request(request_id, **kwargs)
        else:
            self._tab.block_request(request_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tab, name)
