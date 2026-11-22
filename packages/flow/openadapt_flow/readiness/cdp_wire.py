"""A small DevTools client over one WebSocket, with its own reader thread.

The readiness target gate (:mod:`openadapt_flow.readiness.targets`) needs a
browser-level DevTools connection whose events are handled the moment they
arrive, not when Playwright's sync loop next waits: a new tab is paused until
the gate has put the hold on it, and the gate must answer at once. It also
needs flat sessions (``sessionId`` on every message), which Playwright's
``CDPSession`` doesn't expose. This client is standard library only (RFC 6455
text frames, client masking, ping and close) and talks to the loopback
DevTools port the runner opened.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import struct
import threading
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

__all__ = ["CdpError", "CdpWire", "browser_ws_url"]


class CdpError(RuntimeError):
    """A DevTools command failed or the connection closed."""


def browser_ws_url(http_endpoint: str, timeout_s: float = 5.0) -> str:
    """The browser target's WebSocket URL from ``/json/version``."""

    with urllib.request.urlopen(  # noqa: S310 - loopback DevTools port
        http_endpoint.rstrip("/") + "/json/version", timeout=timeout_s
    ) as resp:
        return str(json.loads(resp.read().decode("utf-8"))["webSocketDebuggerUrl"])


class CdpWire:
    """One DevTools WebSocket. ``on_event(method, params, session_id)`` runs on
    one dispatcher thread, in arrival order; :meth:`send` may be called from
    any thread, including from inside ``on_event`` (the reader thread only
    reads, so a command sent from a handler still gets its answer)."""

    def __init__(
        self,
        ws_url: str,
        on_event: Callable[[str, dict[str, Any], Optional[str]], None],
        *,
        timeout_s: float = 10.0,
    ) -> None:
        parts = urlsplit(ws_url)
        if parts.scheme != "ws" or parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise CdpError("the DevTools endpoint must be a loopback ws:// URL")
        self._on_event = on_event
        self._timeout_s = timeout_s
        self._sock = socket.create_connection(
            (parts.hostname, parts.port or 80), timeout=timeout_s
        )
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpError("the DevTools endpoint closed during the handshake")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0]:
            raise CdpError("the DevTools endpoint refused the WebSocket upgrade")
        self._buf = bytearray(rest)
        self._sock.settimeout(None)
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, list[Any]] = {}
        self._pending_lock = threading.Lock()
        self.closed = threading.Event()
        self.error: Optional[str] = None
        self._events: "queue.Queue[Optional[tuple[str, dict[str, Any], Optional[str]]]]" = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_loop, name="openadapt-cdp-wire", daemon=True
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="openadapt-cdp-events", daemon=True
        )
        self._reader.start()
        self._dispatcher.start()

    # -- frames ---------------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise CdpError("the DevTools connection closed")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _recv_message(self) -> Optional[str]:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b1 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def _read_loop(self) -> None:
        try:
            while True:
                text = self._recv_message()
                if text is None:
                    break
                msg = json.loads(text)
                if "id" in msg:
                    with self._pending_lock:
                        slot = self._pending.get(int(msg["id"]))
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
                    continue
                self._events.put(
                    (
                        str(msg.get("method")),
                        dict(msg.get("params") or {}),
                        msg.get("sessionId"),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            if not self.closed.is_set():
                self.error = self.error or f"{type(exc).__name__}: {exc}"
        finally:
            self.closed.set()
            self._events.put(None)
            with self._pending_lock:
                for slot in self._pending.values():
                    slot[0].set()

    def _dispatch_loop(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            try:
                self._on_event(*item)
            except Exception as exc:  # noqa: BLE001 - recorded; the owner fails closed
                self.error = f"event handler: {type(exc).__name__}: {exc}"

    # -- commands ---------------------------------------------------------------

    def send(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send one command and wait for its result; raises :class:`CdpError`."""

        if self.closed.is_set():
            raise CdpError("the DevTools connection is closed")
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
            slot: list[Any] = [threading.Event(), None]
            self._pending[msg_id] = slot
        message: dict[str, Any] = {
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        try:
            try:
                self._send_frame(0x1, json.dumps(message).encode("utf-8"))
            except OSError as exc:
                raise CdpError(f"{method}: {exc}") from exc
            if not slot[0].wait(timeout_s or self._timeout_s):
                raise CdpError(f"{method}: no answer")
        finally:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
        reply = slot[1]
        if reply is None:
            raise CdpError(f"{method}: the DevTools connection closed")
        if "error" in reply:
            raise CdpError(f"{method}: {reply['error'].get('message')}")
        return dict(reply.get("result") or {})

    def send_nowait(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
    ) -> None:
        """Send one command without waiting for its answer (an answer that
        waits on a paused request would stall the caller)."""

        if self.closed.is_set():
            raise CdpError("the DevTools connection is closed")
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
        message: dict[str, Any] = {
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        try:
            self._send_frame(0x1, json.dumps(message).encode("utf-8"))
        except OSError as exc:
            raise CdpError(f"{method}: {exc}") from exc

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
