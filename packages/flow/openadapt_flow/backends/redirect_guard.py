"""Judge redirect hops that Playwright's context routes do not receive.

The caller's route still judges initial requests. A separate Chromium Fetch
session pauses subsequent hops before they leave the browser and hands each
to that same guard. It does not replay requests or rewrite responses.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


@dataclass
class RedirectGuard:
    problem: Optional[str] = None
    violations: list[str] = field(default_factory=list)
    _session: Any = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.problem is None and not self.violations


class _Request:
    def __init__(self, event: Mapping[str, Any], page: Any, main_frame: str):
        request = event["request"]
        self.url = str(request["url"])
        self.method = str(request["method"]).upper()
        self.headers = {
            str(k).lower(): str(v) for k, v in request.get("headers", {}).items()
        }
        # A hop is independently judged, never excused as the server's answer.
        self.redirected_from = None
        self.redirect_guarded = True
        self.frame = page.main_frame if event.get("frameId") == main_frame else None
        self._navigation = event.get("resourceType") == "Document"
        entries = request.get("postDataEntries")
        if entries:
            self.post_data_buffer: Optional[bytes] = b"".join(
                base64.b64decode(entry["bytes"], validate=True) for entry in entries
            )
        elif isinstance(request.get("postData"), str):
            content_type = self.headers.get("content-type", "").split(";")[0]
            if content_type not in (
                "application/json",
                "application/x-www-form-urlencoded",
                "text/plain",
            ):
                raise ValueError("the redirected body cannot be read exactly")
            self.post_data_buffer = request["postData"].encode("utf-8")
        elif request.get("hasPostData"):
            raise ValueError("the redirected body is unavailable")
        else:
            self.post_data_buffer = None

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self, session: Any, request_id: str, request: _Request):
        self.request = request
        self._session = session
        self._request_id = request_id
        self.done = False

    def abort(self) -> None:
        if not self.done:
            self._session.send(
                "Fetch.failRequest",
                {"requestId": self._request_id, "errorReason": "BlockedByClient"},
            )
            self.done = True

    def continue_(self) -> None:
        self._session.send("Fetch.continueRequest", {"requestId": self._request_id})
        self.done = True


def guard_redirects(
    page: Any,
    handler: Callable[[Any], None],
    on_violation: Callable[[str], None],
) -> RedirectGuard:
    """Install before navigation; setup failure and every violation must halt."""

    state = RedirectGuard()
    try:
        session = page.context.new_cdp_session(page)
        state._session = session
        main_frame = session.send("Page.getFrameTree", {})["frameTree"]["frame"]["id"]
        seen_network: set[str] = set()

        def refuse(request_id: str) -> None:
            reason = "a redirected request could not be checked; it was blocked"
            state.violations.append(reason)
            on_violation(reason)
            try:
                session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            except Exception:  # noqa: BLE001 - the context must halt regardless
                pass

        def paused(event: Mapping[str, Any]) -> None:
            request_id = str(event.get("requestId") or "")
            route = None
            try:
                network_id = str(event.get("networkId") or "")
                redirected = bool(event.get("redirectedRequestId")) or (
                    network_id in seen_network
                )
                if not network_id or not request_id or len(seen_network) >= 100000:
                    raise ValueError("request redirect provenance is unavailable")
                seen_network.add(network_id)
                if redirected:
                    route = _Route(
                        session, request_id, _Request(event, page, main_frame)
                    )
                    handler(route)
                    if not route.done:
                        raise ValueError("the redirect guard left a request undecided")
                else:
                    # The context route judges this initial request. This
                    # session only adds the missing later-hop coverage.
                    session.send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:  # noqa: BLE001 - no unchecked redirect can proceed
                if route is None or not route.done:
                    refuse(request_id)

        session.on("Fetch.requestPaused", paused)
        session.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "http://*", "requestStage": "Request"},
                    {"urlPattern": "https://*", "requestStage": "Request"},
                ]
            },
        )
    except Exception:  # noqa: BLE001 - no redirect coverage means refusal
        state.problem = "this browser cannot guard redirected requests"
    return state
