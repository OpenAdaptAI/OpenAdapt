"""Bounded passive metadata on the agent's existing Page, without payload reads.

Only the operator's exact target origin/path can be retained. Dynamic paths,
queries, userinfo and foreign origins are withheld. This is an observation,
never a causal write claim, complete record read or generated API binding.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)


def _endpoint(value: Any) -> tuple[str, str] | None:
    if type(value) is not str or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        # Validate the port too; malformed URLs are not useful metadata.
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        origin = (
            parsed.scheme + "://" + host + (":" + str(port) if port is not None else "")
        )
        return origin, parsed.path or "/"
    except (TypeError, ValueError):
        return None


class NetworkMetadata:
    """A single finite listener lease; never reads headers, cookies or bodies."""

    def __init__(self, page: Any, *, target_url: str | None, limit: int = 512) -> None:
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("INVALID_METADATA_LIMIT")
        self.page = page
        self.target = _endpoint(target_url)
        self.limit = limit
        self.events: list[dict[str, Any]] = []
        self.losses: set[str] = set()
        self.listeners: list[tuple[str, Any]] = []
        self.closed = False
        self.started = False

    def _event(self, phase: str, request: Any, status: Any = None) -> None:
        if self.closed:
            return
        if len(self.events) >= self.limit:
            self.losses.add("EVENT_LIMIT")
            return
        try:
            method = request.method
            endpoint = _endpoint(request.url)
            item = {
                "phase": phase,
                "method": method
                if type(method) is str and method in _METHODS
                else None,
                "status": status
                if type(status) is int and 100 <= status <= 599
                else None,
                "origin": None,
                "path": None,
            }
            if (
                endpoint is not None
                and self.target is not None
                and endpoint == self.target
            ):
                item["origin"], item["path"] = self.target
            self.events.append(item)
        except Exception:
            self.losses.add("EVENT_UNAVAILABLE")

    def _response(self, response: Any) -> None:
        if self.closed or len(self.events) >= self.limit:
            if not self.closed:
                self.losses.add("EVENT_LIMIT")
            return
        try:
            self._event("response", response.request, response.status)
        except Exception:
            self.losses.add("EVENT_UNAVAILABLE")

    def __enter__(self) -> NetworkMetadata:
        if self.started or self.closed:
            raise ValueError("METADATA_LEASE_REUSED")
        self.started = True
        if self.page is None:
            self.losses.add("PAGE_UNAVAILABLE")
            return self
        for name, callback in [
            ("request", lambda request: self._event("request", request)),
            ("response", self._response),
            ("requestfailed", lambda request: self._event("failed", request)),
        ]:
            # Record before registration, so a partial registration is detached.
            self.listeners.append((name, callback))
            try:
                self.page.on(name, callback)
            except BaseException as exc:
                self.losses.add("ATTACH_UNAVAILABLE")
                try:
                    self.close()
                except BaseException:
                    if isinstance(exc, Exception):
                        raise
                if not isinstance(exc, Exception):
                    raise
                break
        return self

    def close(self) -> None:
        self.closed = True
        pending: BaseException | None = None
        for name, callback in reversed(self.listeners):
            try:
                self.page.remove_listener(name, callback)
            except BaseException as exc:
                self.losses.add("DETACH_UNAVAILABLE")
                if not isinstance(exc, Exception) and pending is None:
                    pending = exc
        self.listeners.clear()
        if pending is not None:
            raise pending

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> None:
        try:
            self.close()
        except BaseException:
            if kind is None:
                raise

    def summary(self) -> dict[str, Any]:
        return {
            "schema": "openadapt.network-metadata/v1",
            "mode": "metadata_only",
            "events": [dict(event) for event in self.events],
            "losses": sorted(self.losses),
            "capture_closed": self.closed,
            "records_complete": None,
            "causal_attribution": "UNAVAILABLE",
            "api_binding": "UNAVAILABLE",
        }
