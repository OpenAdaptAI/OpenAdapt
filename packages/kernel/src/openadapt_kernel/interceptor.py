"""Wire and DOM-effect commit interceptor.

Detects state mutations from actual network methods (POST, PUT, PATCH, DELETE)
or non-empty dirty form submissions instead of button-label regex heuristics.
Excludes search/filter inputs from dirty buffers and escapes escalation actions
so nested quotes never break downstream action parsers.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Set
from urllib.parse import urlparse

MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SEARCH_FILTER_RE = re.compile(
    r"(?:^|[\s_-])(?:search|searchbox|search[\s_-]query|filter|keyword|pagination|page[\s_-]?size|sort[\s_-]?by|order[\s_-]?by)(?:$|[\s_-])",
    re.IGNORECASE,
)
_TELEMETRY_HOSTS = frozenset({
    "google-analytics.com", "analytics.google.com", "posthog.com",
    "segment.io", "segment.com", "sentry.io", "mixpanel.com", "plausible.io",
})
_TELEMETRY_PATH_RE = re.compile(
    r"/(?:analytics|telemetry|metrics|beacon|sentry|posthog|_next/data)(?:/|$)", re.IGNORECASE
)


def _is_telemetry_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if any(host == th or host.endswith(f".{th}") for th in _TELEMETRY_HOSTS):
        return True
    return bool(_TELEMETRY_PATH_RE.search(parsed.path or ""))


def _url_matches_origin(url: str, target_origin: str) -> bool:
    if not target_origin:
        return True
    up = urlparse(url or "")
    tp = urlparse(target_origin)
    if not up.scheme or not up.netloc or not tp.scheme or not tp.netloc:
        return False
    if tp.port is not None:
        return up.scheme.lower() == tp.scheme.lower() and up.netloc.lower() == tp.netloc.lower()
    return up.scheme.lower() == tp.scheme.lower() and (up.hostname or "").lower() == (tp.hostname or "").lower()


@dataclass
class NetworkEvent:
    method: str
    url: str
    status: Optional[int] = None
    resource_type: str = "fetch"
    post_data_preview: str = ""


@dataclass
class CommitClassification:
    is_commit: bool
    kind: Literal["read_only_nav", "http_mutation", "form_submit", "file_upload"]
    reason: str
    mutating_events: List[NetworkEvent] = field(default_factory=list)


def is_search_or_filter_field(
    field_id: str,
    input_type: str = "",
    aria_role: str = "",
    label: str = "",
) -> bool:
    if input_type.strip().lower() == "search" or aria_role.strip().lower() == "searchbox":
        return True
    combined = f"{field_id} {label}"
    return bool(_SEARCH_FILTER_RE.search(combined))


def sanitize_action_string(prefix: str, message: str, max_len: int = 240) -> str:
    """Format a safe single-line escalation payload with no unescaped quote hazards."""
    compact = re.sub(r"\s+", " ", str(message or "")).strip()[:max_len]
    safe_payload = json.dumps(f"{prefix} {compact}".strip())
    return safe_payload


class CommitInterceptor:
    """Observes form edits and wire events to separate navigation from commits."""

    def __init__(self, ignored_url_patterns: Sequence[str] = ()) -> None:
        self._dirty_fields: Dict[str, str] = {}
        self._pending_events: List[NetworkEvent] = []
        self._ignored_res = [re.compile(p, re.IGNORECASE) for p in ignored_url_patterns]
        self._attached_pages: List[Any] = []
        self._inflight_mutating: Set[int] = set()

    @property
    def dirty_fields(self) -> Dict[str, str]:
        return dict(self._dirty_fields)

    def mark_field_edited(
        self,
        field_id: str,
        value: str,
        *,
        input_type: str = "",
        aria_role: str = "",
        label: str = "",
    ) -> bool:
        """Record a form mutation unless the field is a search/filter control."""
        if is_search_or_filter_field(field_id, input_type=input_type, aria_role=aria_role, label=label):
            return False
        self._dirty_fields[field_id] = value
        return True

    def clear_dirty_fields(self, field_ids: Optional[Sequence[str]] = None) -> None:
        if field_ids is None:
            self._dirty_fields.clear()
            return
        for fid in field_ids:
            self._dirty_fields.pop(fid, None)

    def begin_step(self) -> None:
        self._pending_events.clear()
        self._inflight_mutating.clear()

    def record_network_event(self, event: NetworkEvent) -> None:
        self._pending_events.append(event)

    def attach_playwright_page(self, page: Any) -> None:
        """Attach non-blocking Playwright request/response observers."""
        if page in self._attached_pages:
            return
        pending_by_req: Dict[int, NetworkEvent] = {}

        def _on_request(req: Any) -> None:
            try:
                method = str(req.method or "GET").upper()
                url = str(req.url or "")
                rtype = str(getattr(req, "resource_type", "") or "fetch")
                preview = ""
                if method in MUTATING_HTTP_METHODS:
                    raw = getattr(req, "post_data", None)
                    if isinstance(raw, str):
                        preview = raw[:200]
                ev = NetworkEvent(method=method, url=url, resource_type=rtype, post_data_preview=preview)
                pending_by_req[id(req)] = ev
                self._pending_events.append(ev)
                if self._is_mutating_event(ev):
                    self._inflight_mutating.add(id(req))
            except Exception:
                return

        def _on_response(resp: Any) -> None:
            try:
                req = resp.request
                self._inflight_mutating.discard(id(req))
                ev = pending_by_req.get(id(req))
                if ev is not None:
                    ev.status = int(resp.status)
            except Exception:
                return

        def _on_request_failed(req: Any) -> None:
            try:
                self._inflight_mutating.discard(id(req))
            except Exception:
                return

        page.on("request", _on_request)
        page.on("response", _on_response)
        page.on("requestfailed", _on_request_failed)
        self._attached_pages.append(page)

    def _is_mutating_event(self, ev: NetworkEvent) -> bool:
        if ev.method.upper() not in MUTATING_HTTP_METHODS:
            return False
        if _is_telemetry_url(ev.url):
            return False
        if any(rx.search(ev.url) for rx in self._ignored_res):
            return False
        return True

    def has_target_origin_2xx_mutation(self, target_origin: str = "") -> bool:
        return any(
            self._is_mutating_event(ev)
            and _url_matches_origin(ev.url, target_origin)
            and ev.status is not None
            and 200 <= ev.status < 300
            for ev in self._pending_events
        )

    def wait_for_commit_settle(
        self,
        page: Any,
        *,
        target_origin: str = "",
        timeout_ms: int = 5000,
        quiet_ms: int = 120,
    ) -> bool:
        """Wait for pending target-origin mutating requests + short DOM quiet window."""
        deadline = time.monotonic() + max(timeout_ms, 50) / 1000.0
        page.wait_for_timeout(40)
        while time.monotonic() < deadline:
            inflight = bool(self._inflight_mutating) or any(
                self._is_mutating_event(ev)
                and _url_matches_origin(ev.url, target_origin)
                and ev.status is None
                for ev in self._pending_events
            )
            if inflight:
                page.wait_for_timeout(30)
                continue
            rem_ms = max(int((deadline - time.monotonic()) * 1000), 20)
            try:
                page.evaluate(
                    """([q, cap]) => new Promise((res) => {
                      let done = false;
                      const fin = (v) => { if (!done) { done = true; obs.disconnect(); res(v); } };
                      let t = setTimeout(() => fin(true), q);
                      const obs = new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => fin(true), q); });
                      if (document.body) obs.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true });
                      setTimeout(() => fin(false), Math.min(cap, q * 4));
                    })""",
                    [quiet_ms, rem_ms],
                )
            except Exception:
                page.wait_for_timeout(min(quiet_ms, rem_ms))
            if not self._inflight_mutating:
                return True
        return False

    def classify_step(
        self,
        *,
        element_tag: str = "",
        element_role: str = "",
        had_file_upload: bool = False,
        form_submitted: bool = False,
    ) -> CommitClassification:
        """Classify the step strictly by observed wire/DOM mutation effects.

        Navigation links (<a>, role='link') that trigger zero mutating HTTP calls
        and submit zero dirty fields never qualify as commits, preventing
        destructive reloads on SPA menu clicks.
        """
        mutating = [ev for ev in self._pending_events if self._is_mutating_event(ev)]

        if had_file_upload:
            return CommitClassification(True, "file_upload", "file_upload_dispatched", mutating)
        if mutating:
            return CommitClassification(
                True, "http_mutation", f"observed_{len(mutating)}_mutating_http_requests", mutating
            )
        tag_l, role_l = element_tag.strip().lower(), element_role.strip().lower()
        if form_submitted and self._dirty_fields and tag_l != "a" and role_l != "link":
            return CommitClassification(
                True, "form_submit", f"form_submitted_with_{len(self._dirty_fields)}_dirty_fields", []
            )
        return CommitClassification(False, "read_only_nav", "no_wire_mutation_or_dirty_submit", [])
