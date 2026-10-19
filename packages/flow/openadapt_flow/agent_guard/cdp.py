"""The commit guard's hands in the agent's tab: the CDP ``Fetch`` domain.

:class:`CdpGuardPage` opens its own DevTools session on the tab the recorder
attached to, pauses the tab's requests at the request stage, and follows
``Network`` events to learn when a save finished. Playwright's sync API
delivers CDP events only while it waits on the wire, so the handlers only
queue them; :meth:`CdpGuardPage.drain` hands them to the
:class:`~openadapt_flow.agent_guard.commit_guard.CommitGuard` from the
session's loop.

Paused resource types: documents (form posts), ``fetch``, XHR, beacons
(``Ping``), event sources and ``Other``. Images, scripts, stylesheets, fonts
and media are GETs and aren't paused. A save sent over a WebSocket isn't seen
(CDP ``Fetch`` doesn't intercept WebSocket frames).

When the session stops with a save still paused (waiting for a person, or an
error in the guard), the save is blocked before the DevTools session
detaches. Chromium lets paused requests go on when the session that paused
them goes away, so detaching first would let them through unchecked.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from openadapt_flow.agent_guard.commit_guard import BLOCK_STATUS, GUARD_HEADER
from openadapt_flow.agent_guard.requests import HeldRequest

__all__ = ["CdpGuardPage", "PAUSED_TYPES"]

PAUSED_TYPES = ("Document", "XHR", "Fetch", "Ping", "EventSource", "Other")

NOTICE_ID = "openadapt-guard-notice"

_READ_SCREEN = """
(selector) => {
  const old = document.getElementById(%(notice)s);
  if (old) { old.remove(); }
  const root = selector ? document.querySelector(selector) : document.body;
  return root ? (root.innerText || root.textContent || '') : null;
}
"""

_NOTIFY = """
(message, level) => {
  let box = document.getElementById(%(notice)s);
  if (!box) {
    box = document.createElement('div');
    box.id = %(notice)s;
    box.setAttribute('role', 'alert');
    box.setAttribute('aria-live', 'assertive');
    box.setAttribute('data-openadapt-guard', '1');
    box.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;'
      + 'padding:10px 14px;font:14px/1.4 system-ui,sans-serif;color:#fff;';
    (document.body || document.documentElement).appendChild(box);
  }
  box.style.background = level === 'ok' ? '#1f6f43'
    : (level === 'info' ? '#1d4f91' : '#a1271b');
  box.textContent = message;
  return true;
}
"""


def _call(source: str) -> str:
    return source % {"notice": json.dumps(NOTICE_ID)}


class CdpGuardPage:
    """A :class:`~openadapt_flow.agent_guard.commit_guard.GuardPage` over CDP."""

    def __init__(self, page: Any, *, screen_selector: Optional[str] = None) -> None:
        self.page = page
        self.screen_selector = screen_selector
        self.cdp = page.context.new_cdp_session(page)
        self._paused: list[dict[str, Any]] = []
        self._done: list[Optional[str]] = []
        #: Requests this session paused and hasn't answered yet.
        self._open: dict[str, HeldRequest] = {}
        self.cdp.on("Fetch.requestPaused", self._paused.append)
        self.cdp.on(
            "Network.loadingFinished", lambda p: self._done.append(p.get("requestId"))
        )
        self.cdp.on(
            "Network.loadingFailed", lambda p: self._done.append(p.get("requestId"))
        )
        self.cdp.send("Network.enable", {})
        self.cdp.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "resourceType": kind, "requestStage": "Request"}
                    for kind in PAUSED_TYPES
                ]
            },
        )

    # -- the loop ------------------------------------------------------------------

    def drain(self, guard: Any) -> int:
        """Hand queued requests and completions to ``guard``; returns how many
        requests it decided."""

        decided = 0
        while self._paused:
            held = HeldRequest.from_cdp(self._paused.pop(0))
            self._open[held.request_id] = held
            try:
                guard.on_request(held)
            except Exception:  # noqa: BLE001 - fail closed on any guard error
                self._block_open(held.request_id, "guard_error")
                raise
            decided += 1
        while self._done:
            guard.on_request_done(self._done.pop(0))
        guard.poll()
        return decided

    def release(self) -> None:
        """Block every request still paused, then stop pausing and detach."""

        for request_id in list(self._open):
            self._block_open(request_id, "guard_stopped")
        for params in self._paused:
            self._block_open(str(params.get("requestId") or ""), "guard_stopped")
        self._paused.clear()
        for method in ("Fetch.disable",):
            try:
                self.cdp.send(method, {})
            except Exception:  # noqa: BLE001 - the tab may be gone
                pass
        try:
            self.cdp.detach()
        except Exception:  # noqa: BLE001
            pass

    def _block_open(self, request_id: str, code: str) -> None:
        body = json.dumps(
            {
                "openadapt_guard": {
                    "blocked": True,
                    "code": code,
                    "message": "the guard stopped before this save was checked",
                }
            }
        )
        self.block_request(
            request_id,
            status=BLOCK_STATUS,
            body=body,
            content_type="application/json",
            headers={GUARD_HEADER: f"blocked; code={code}"},
        )

    # -- GuardPage -------------------------------------------------------------------

    def continue_request(self, request_id: str) -> None:
        self._open.pop(request_id, None)
        try:
            self.cdp.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:  # noqa: BLE001 - the page cancelled it
            pass

    def block_request(
        self,
        request_id: str,
        *,
        status: int,
        body: str,
        content_type: str,
        headers: dict[str, str],
    ) -> None:
        self._open.pop(request_id, None)
        response_headers = [{"name": "Content-Type", "value": content_type}] + [
            {"name": k, "value": v} for k, v in headers.items()
        ]
        try:
            self.cdp.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": status,
                    "responseHeaders": response_headers,
                    "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                },
            )
        except Exception:  # noqa: BLE001 - the page cancelled it; nothing was sent
            try:
                self.cdp.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            except Exception:  # noqa: BLE001
                pass

    def screen_text(self) -> Optional[str]:
        try:
            out = self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": _call(_READ_SCREEN),
                    "arguments": [{"value": self.screen_selector}],
                    "executionContextId": self._context_id(),
                    "returnByValue": True,
                },
            )
        except Exception:  # noqa: BLE001 - an unreadable screen is checked as such
            return None
        value = (out.get("result") or {}).get("value")
        return value if isinstance(value, str) else None

    def screenshot(self) -> Optional[bytes]:
        try:
            out = self.cdp.send("Page.captureScreenshot", {"format": "png"})
            return base64.b64decode(out["data"])
        except Exception:  # noqa: BLE001
            return None

    def notify(self, message: str, *, level: str) -> None:
        try:
            self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": _call(_NOTIFY),
                    "arguments": [{"value": message}, {"value": level}],
                    "executionContextId": self._context_id(),
                    "returnByValue": True,
                },
            )
        except Exception:  # noqa: BLE001 - a notice is best effort; the block isn't
            pass

    def _context_id(self) -> int:
        """A fresh isolated world on the tab's current document: it shares the
        DOM, so it reads what is shown, but the page's own scripts can't
        replace the functions it calls. A world lasts one document, so each
        call makes one (a few per save)."""

        tree = self.cdp.send("Page.getFrameTree", {})
        frame_id = tree["frameTree"]["frame"]["id"]
        world = self.cdp.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": "openadapt-guard"},
        )
        return int(world["executionContextId"])
