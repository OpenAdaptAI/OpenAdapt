"""Pluggable event sinks and SSRF-safe URL validation for openadapt monitor."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse


class ChangeEventLike(Protocol):
    """Minimal read-only structural protocol satisfied by ChangeEvent."""

    @property
    def event_id(self) -> str: ...

    @property
    def monitor_name(self) -> str: ...

    @property
    def event_type(self) -> str: ...

    @property
    def timestamp(self) -> str: ...

    @property
    def record_key(self) -> str | None: ...

    @property
    def before(self) -> Mapping[str, Any] | None: ...

    @property
    def after(self) -> Mapping[str, Any] | None: ...

    @property
    def changed_fields(self) -> tuple[str, ...]: ...

    @property
    def error(self) -> str | None: ...

    def to_dict(self) -> dict[str, Any]: ...


class EventSink(Protocol):
    """Receives batches of change events produced by MonitorEngine."""

    def emit(self, events: Sequence[ChangeEventLike]) -> None: ...


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_blocked_ip(raw_ip: str) -> bool:
    ip = ipaddress.ip_address(raw_ip)
    if ip.is_loopback:
        return False
    return (
        ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_http_endpoint(
    url: str,
    *,
    resolver: Callable[[str], Sequence[str]] | None = None,
) -> str:
    """Validate an HTTP(S) endpoint against SSRF and plaintext-egress risks.

    Rules (fail-closed):
    - Only ``https`` and ``http`` schemes are allowed.
    - Plain ``http`` is permitted strictly for explicit loopback hosts
      (``localhost``, ``127.0.0.1``, ``::1``).
    - Non-loopback private (RFC 1918 / ULA), link-local (including cloud
      metadata ``169.254.169.254``), multicast, reserved, and unspecified IPs
      are rejected whether given directly or resolved via DNS.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme {scheme!r}; only http/https allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("Endpoint URL must include a hostname")

    is_loopback_host = host in _LOOPBACK_HOSTS
    if scheme == "http" and not is_loopback_host:
        raise ValueError(
            f"Plaintext http is only allowed for loopback hosts, not {host!r}; use https"
        )

    # Check literal IP addresses immediately.
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _is_blocked_ip(str(ip_obj)):
            raise ValueError(f"Blocked non-public IP address: {host}")
        return url

    if is_loopback_host:
        return url

    # Resolve hostname and verify every returned IP is public.
    resolved_ips: Sequence[str]
    if resolver is not None:
        resolved_ips = resolver(host)
    else:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            resolved_ips = [str(info[4][0]) for info in infos]
        except OSError as exc:
            raise ValueError(f"Could not resolve host {host!r}: {exc}") from exc

    if not resolved_ips:
        raise ValueError(f"Host {host!r} resolved to no IP addresses")

    for ip_str in resolved_ips:
        if _is_blocked_ip(ip_str):
            raise ValueError(f"Host {host!r} resolved to blocked IP {ip_str}")
    return url


def sign_webhook_payload(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature header value for a webhook payload."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class JsonlEventSink:
    """Append-only NDJSON event stream sink."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def emit(self, events: Sequence[ChangeEventLike]) -> None:
        if not events:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event.to_dict(), sort_keys=True, default=str) + "\n")


class MemoryMarkdownSink:
    """Event-sourced Markdown memory sink (`memory_log.md` + `memory_current.md`).

    - `memory_log.md` is strictly append-only (audit trail of every event).
    - `memory_current.md` is atomically rewritten from the current materialized
      state of active records.
    """

    def __init__(self, directory: Path | str, *, title: str = "Monitor State") -> None:
        self.directory = Path(directory)
        self.title = title
        self.log_path = self.directory / "memory_log.md"
        self.current_path = self.directory / "memory_current.md"
        self._state: dict[str, dict[str, Any]] = {}

    def emit(self, events: Sequence[ChangeEventLike]) -> None:
        if not events:
            return
        self.directory.mkdir(parents=True, exist_ok=True)

        log_lines: list[str] = []
        if not self.log_path.exists():
            log_lines.append(f"# {self.title} — Event Log\n\n")

        state_changed = False
        for ev in events:
            if ev.event_type == "record.created" and ev.record_key is not None:
                self._state[ev.record_key] = dict(ev.after or {})
                state_changed = True
                summary = json.dumps(ev.after or {}, sort_keys=True, default=str)
                log_lines.append(
                    f"- `{ev.timestamp}` **{ev.event_type}** `{ev.record_key}`: `{summary}`\n"
                )
            elif ev.event_type == "record.updated" and ev.record_key is not None:
                self._state[ev.record_key] = dict(ev.after or {})
                state_changed = True
                changed = ", ".join(ev.changed_fields)
                summary = json.dumps(ev.after or {}, sort_keys=True, default=str)
                log_lines.append(
                    f"- `{ev.timestamp}` **{ev.event_type}** `{ev.record_key}` "
                    f"(fields: `{changed}`): `{summary}`\n"
                )
            elif ev.event_type == "record.deleted" and ev.record_key is not None:
                self._state.pop(ev.record_key, None)
                state_changed = True
                log_lines.append(f"- `{ev.timestamp}` **{ev.event_type}** `{ev.record_key}`\n")
            else:
                log_lines.append(
                    f"- `{ev.timestamp}` **{ev.event_type}**: `{ev.error or 'unknown error'}`\n"
                )

        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.writelines(log_lines)

        if state_changed or not self.current_path.exists():
            self._write_current()

    def _write_current(self) -> None:
        lines: list[str] = [f"# {self.title} — Current State\n\n"]
        if not self._state:
            lines.append("_No active records._\n")
        else:
            all_cols: list[str] = sorted(
                {col for row in self._state.values() for col in row.keys()}
            )
            header = ["record_key", *all_cols]
            lines.append("| " + " | ".join(header) + " |\n")
            lines.append("| " + " | ".join("---" for _ in header) + " |\n")
            for key in sorted(self._state):
                row = self._state[key]
                cells = [
                    str(key).replace("|", "\\|"),
                    *(str(row.get(col, "")).replace("|", "\\|") for col in all_cols),
                ]
                lines.append("| " + " | ".join(cells) + " |\n")

        tmp_path = self.current_path.with_suffix(".md.tmp")
        tmp_path.write_text("".join(lines), encoding="utf-8")
        os.replace(tmp_path, self.current_path)


class WebhookEventSink:
    """Optional HMAC-SHA256 signed webhook emitter with strict SSRF protection."""

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        timeout_s: float = 5.0,
        headers: Mapping[str, str] | None = None,
        sender: Callable[[str, bytes, dict[str, str], float], None] | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._resolver = resolver
        self.url = validate_http_endpoint(url, resolver=resolver)
        self.secret = secret
        self.timeout_s = timeout_s
        self.headers = dict(headers or {})
        self._sender = sender

    def emit(self, events: Sequence[ChangeEventLike]) -> None:
        if not events:
            return
        validate_http_endpoint(self.url, resolver=self._resolver)
        payload = {
            "schema": "openadapt.monitor.webhook.v1",
            "events": [ev.to_dict() for ev in events],
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        req_headers = {
            "Content-Type": "application/json",
            **self.headers,
        }
        if self.secret is not None:
            req_headers["X-OpenAdapt-Signature-256"] = sign_webhook_payload(body, self.secret)

        if self._sender is not None:
            self._sender(self.url, body, req_headers, self.timeout_s)
            return

        req = urllib.request.Request(self.url, data=body, headers=req_headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise RuntimeError(f"Webhook returned HTTP {status}")
