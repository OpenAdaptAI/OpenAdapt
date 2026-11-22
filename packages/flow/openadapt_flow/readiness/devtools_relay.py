"""The shadow browser's only DevTools door: a pipe, and a token-gated relay.

A ``--remote-debugging-port`` is open to every local user: anyone on the
machine could drive the signed-in session. So the readiness runner starts
the headless browser with ``--remote-debugging-pipe`` and no TCP port, and
:class:`DevToolsRelay` owns the pipe. It serves DevTools on loopback only
under a random per-run token in the URL path::

    http://127.0.0.1:<port>/<token>              (the candidate's OPENADAPT_GUARD_CDP_URL)
    http://127.0.0.1:<port>/<token>/json/version
    ws://127.0.0.1:<port>/<token>/devtools/browser

Any request without the token (HTTP or WebSocket upgrade) is answered 403.

Every client (the runner's own Playwright, the target gate, the candidate)
shares the one pipe connection. Each WebSocket client gets its own browser
target session (``Target.attachToBrowserTarget``): its messages without a
``sessionId`` go to that session, the sessions it opens are its own, and
events are routed to the client that owns their session, so to a client
the relay looks like a DevTools connection of its own. Message ids are
remapped per client. When a client disconnects, every session it owned is
detached, as Chromium does for a closed connection.

Network: with ``allowed_origins`` the browser gets ``--proxy-server`` at a
loopback port that refuses every connection (bound, never listening) and
``--proxy-bypass-list`` naming only those origins' hosts and ports, with
``<-loopback>`` so other loopback ports are not exempt either. A page can
then reach the app and nothing else, by ``GET`` or WebSocket; only the
candidate's process is under the OS egress block, so this is the browser's
half (:attr:`DevToolsRelay.network_limited`).

The limit can't be undone through the relay: a ``Target.createBrowserContext``
may carry only :data:`CONTEXT_PARAMS` (no ``proxyServer``,
``proxyBypassList`` or ``originsWithUniversalNetworkAccess``, and no key the
relay doesn't know), and :data:`REFUSED_METHODS` (fetching from the browser
process, exposing DevTools to a page, browser commands) are refused. WebRTC
may not send UDP that bypasses the proxy
(``--force-webrtc-ip-handling-policy=disable_non_proxied_udp``), and DNS
prefetching is off.

The hold is unchanged: the gate's ``Fetch.enable`` on its browser session
pauses every other target's requests and the tab's own session pauses the
tab's, exactly as over a TCP port. Close the browser (:meth:`close`) before
any hold is released.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

__all__ = ["DevToolsRelay", "headless_shell"]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: The only parameters a client may give ``Target.createBrowserContext``.
CONTEXT_PARAMS = frozenset({"disposeOnDetach"})
#: Methods no client may send through the relay (fail closed).
REFUSED_METHODS = frozenset(
    {
        "Network.loadNetworkResource",
        "Target.exposeDevToolsProtocol",
        "Browser.executeBrowserCommand",
    }
)


def headless_shell(executable_path: str) -> str:
    """The headless shell Playwright installs next to ``executable_path`` (its
    full Chromium), else ``executable_path`` itself; ``OPENADAPT_READINESS_CHROMIUM``
    overrides both."""

    override = os.environ.get("OPENADAPT_READINESS_CHROMIUM")
    if override:
        return override
    path = Path(executable_path)
    for parent in path.parents:
        if parent.name.startswith("chromium-"):
            rev = parent.name.split("-", 1)[1]
            shells = parent.parent.glob(f"chromium_headless_shell-{rev}/*/*")
            for found in sorted(
                p
                for p in shells
                if p.name.startswith(("chrome-headless-shell", "headless_shell"))
            ):
                if found.is_file() and os.access(found, os.X_OK):
                    return str(found)
            break
    return str(path)


class _Client:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.browser_session: Optional[str] = None
        self.sessions: set[str] = set()
        self.closed = False

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 1 << 16:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        with self.lock:
            if self.closed:
                return
            try:
                self.sock.sendall(bytes(header) + data)
            except OSError:
                self.closed = True


class DevToolsRelay:
    """Start a pipe-only headless browser and serve it under a token."""

    def __init__(
        self,
        executable: str,
        *,
        extra_args: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        block_webrtc: bool = True,
    ) -> None:
        """``block_webrtc=False`` exists for the test that shows the block is
        needed; nothing else turns it off."""
        self.block_webrtc = block_webrtc
        self.executable = executable
        self.extra_args = tuple(extra_args)
        self.allowed_origins = tuple(allowed_origins)
        self._dead_port: Optional[socket.socket] = None
        self.token = secrets.token_urlsafe(24)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[Optional[_Client], Any, Any]] = {}
        self._owners: dict[str, _Client] = {}
        self._clients: list[_Client] = []
        self._write_lock = threading.Lock()
        self.refused = 0
        self.proc: Optional[subprocess.Popen] = None
        self._profile: Optional[str] = None
        self._server: Optional[socket.socket] = None
        self.port = 0

    # -- lifecycle -----------------------------------------------------------------

    @property
    def network_limited(self) -> bool:
        """The browser can reach only :attr:`allowed_origins`."""

        return self._dead_port is not None

    def _network_args(self) -> list[str]:
        if not self.allowed_origins:
            return []
        from urllib.parse import urlsplit

        hosts = []
        for origin in self.allowed_origins:
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"not an http(s) origin: {origin!r}")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            hosts.append(f"{host}:{port}")
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))  # bound and never listening: refuses all
        self._dead_port = dead
        proxy = f"http://127.0.0.1:{dead.getsockname()[1]}"
        args = [
            f"--proxy-server={proxy}",
            "--proxy-bypass-list=" + ";".join(["<-loopback>", *hosts]),
            "--dns-prefetch-disable",
        ]
        if self.block_webrtc:
            args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
        return args

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/{self.token}/devtools/browser"

    def start(self) -> "DevToolsRelay":
        to_browser_r, self._to_browser = os.pipe()
        self._from_browser, from_browser_w = os.pipe()
        self._profile = tempfile.mkdtemp(prefix="openadapt-readiness-")
        argv = [
            self.executable,
            "--headless",
            "--remote-debugging-pipe",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--password-store=basic",
            "--use-mock-keychain",
            # Playwright's own Linux defaults: CI containers have no user
            # namespaces for Chromium's sandbox, and a small /dev/shm.
            *(
                ("--no-sandbox", "--disable-dev-shm-usage")
                if sys.platform.startswith("linux")
                else ()
            ),
            *self._network_args(),
            *self.extra_args,
            "about:blank",
        ]

        def child_fds() -> None:
            os.dup2(to_browser_r, 3)
            os.dup2(from_browser_w, 4)

        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=child_fds,  # noqa: PLW1509 - the pipe must be fds 3 and 4
            # 3 and 4 too: kept open if fds are closed after the dup2.
            pass_fds=(to_browser_r, from_browser_w, 3, 4),
            start_new_session=True,
        )
        os.close(to_browser_r)
        os.close(from_browser_w)
        threading.Thread(
            target=self._read_pipe, name="openadapt-relay-pipe", daemon=True
        ).start()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        self._server = server
        self.port = server.getsockname()[1]
        threading.Thread(
            target=self._accept, name="openadapt-relay-accept", daemon=True
        ).start()
        return self

    def close(self) -> None:
        """Close the browser (every target with it), then the relay."""

        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                self._write({"id": next(self._ids), "method": "Browser.close"})
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        for client in list(self._clients):
            self._drop(client, detach=False)
        for fd in (
            getattr(self, "_to_browser", None),
            getattr(self, "_from_browser", None),
        ):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
        if self._dead_port is not None:
            self._dead_port.close()

    # -- the pipe ------------------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message).encode("utf-8") + b"\0"
        with self._write_lock:
            view = memoryview(data)
            while view:
                n = os.write(self._to_browser, view)
                view = view[n:]

    def _read_pipe(self) -> None:
        buf = b""
        while True:
            try:
                chunk = os.read(self._from_browser, 1 << 16)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                try:
                    self._from_browser_message(json.loads(raw))
                except Exception:  # noqa: BLE001 - one bad message drops nothing else
                    continue

    def _from_browser_message(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            with self._lock:
                entry = self._pending.pop(int(msg["id"]), None)
            if entry is None:
                return
            client, original, callback = entry
            if callback is not None:
                callback(msg)
                return
            if client is None:
                return
            result = msg.get("result") or {}
            child = result.get("sessionId") if isinstance(result, dict) else None
            if child:
                self._own(client, str(child))
            out = {k: v for k, v in msg.items() if k != "sessionId"}
            out["id"] = original
            if msg.get("sessionId") and msg["sessionId"] != client.browser_session:
                out["sessionId"] = msg["sessionId"]
            client.send_text(json.dumps(out))
            return
        session = msg.get("sessionId")
        if not session:
            return
        with self._lock:
            client = self._owners.get(str(session))
        if client is None:
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget" and params.get("sessionId"):
            self._own(client, str(params["sessionId"]))
        elif method == "Target.detachedFromTarget" and params.get("sessionId"):
            with self._lock:
                self._owners.pop(str(params["sessionId"]), None)
                client.sessions.discard(str(params["sessionId"]))
        out = dict(msg)
        if session == client.browser_session:
            out.pop("sessionId", None)
        client.send_text(json.dumps(out))

    def _own(self, client: _Client, session: str) -> None:
        with self._lock:
            self._owners[session] = client
            client.sessions.add(session)

    # -- the server ----------------------------------------------------------------

    def _accept(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _refuse(self, conn: socket.socket, code: str = "403 Forbidden") -> None:
        self.refused += 1
        try:
            conn.sendall(
                f"HTTP/1.1 {code}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
        except OSError:
            pass
        conn.close()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                head += chunk
                if len(head) > 65536:
                    self._refuse(conn, "431 Request Header Fields Too Large")
                    return
        except OSError:
            conn.close()
            return
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2 or parts[0] != "GET":
            self._refuse(conn)
            return
        path = parts[1].split("?", 1)[0]
        prefix = f"/{self.token}"
        if not secrets.compare_digest(
            path[: len(prefix)].encode(), prefix.encode()
        ) or (len(path) > len(prefix) and path[len(prefix)] != "/"):
            self._refuse(conn)
            return
        rest = path[len(prefix) :] or "/"
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() == "websocket":
            if rest != "/devtools/browser":
                self._refuse(conn, "404 Not Found")
                return
            self._websocket(conn, headers.get("sec-websocket-key", ""))
            return
        if rest.rstrip("/") in ("/json/version", "/json/version/"):
            body = json.dumps(
                {
                    "Browser": "HeadlessChrome (OpenAdapt readiness relay)",
                    "Protocol-Version": "1.3",
                    "webSocketDebuggerUrl": self.ws_url,
                }
            ).encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            conn.close()
            return
        self._refuse(conn, "404 Not Found")

    def _websocket(self, conn: socket.socket, key: str) -> None:
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        conn.settimeout(None)
        client = _Client(conn)
        ready = threading.Event()

        def attached(msg: dict[str, Any]) -> None:
            session = (msg.get("result") or {}).get("sessionId")
            if session:
                client.browser_session = str(session)
                self._own(client, str(session))
            ready.set()

        msg_id = next(self._ids)
        with self._lock:
            self._pending[msg_id] = (None, None, attached)
        self._clients.append(client)
        try:
            self._write({"id": msg_id, "method": "Target.attachToBrowserTarget"})
        except OSError:
            self._drop(client, detach=False)
            return
        if not ready.wait(10) or client.browser_session is None:
            self._drop(client, detach=False)
            return
        try:
            self._client_loop(client)
        except (ConnectionError, OSError, ValueError):
            pass  # the client went away (or sent something unreadable)
        finally:
            self._drop(client, detach=True)

    def _client_loop(self, client: _Client) -> None:
        buf = bytearray()
        parts: list[bytes] = []

        def need(n: int) -> bytes:
            while len(buf) < n:
                chunk = client.sock.recv(1 << 16)
                if not chunk:
                    raise ConnectionError
                buf.extend(chunk)
            out = bytes(buf[:n])
            del buf[:n]
            return out

        while True:
            b0, b1 = need(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", need(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", need(8))[0]
            mask = need(4) if b1 & 0x80 else b"\0\0\0\0"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(need(n)))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                with client.lock:
                    client.sock.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if not b0 & 0x80:
                continue
            text = b"".join(parts).decode("utf-8")
            parts.clear()
            self._from_client(client, json.loads(text))

    def _refusal(self, msg: dict[str, Any]) -> Optional[str]:
        method = str(msg.get("method") or "")
        if method in REFUSED_METHODS:
            return f"{method} is refused by the readiness relay"
        if method == "Target.createBrowserContext":
            extra = set(msg.get("params") or {}) - CONTEXT_PARAMS
            if extra:
                return (
                    "Target.createBrowserContext with "
                    + ", ".join(sorted(extra))
                    + " is refused by the readiness relay (the browser's network limit applies)"
                )
        return None

    def _from_client(self, client: _Client, msg: dict[str, Any]) -> None:
        original = msg.get("id")
        session = msg.get("sessionId")
        refusal = self._refusal(msg)
        if refusal is not None:
            self.refused += 1
            reply: dict[str, Any] = {
                "id": original,
                "error": {"code": -32000, "message": refusal},
            }
            if session:
                reply["sessionId"] = session
            client.send_text(json.dumps(reply))
            return
        if session:
            with self._lock:
                owner = self._owners.get(str(session))
            if owner is not client:
                client.send_text(
                    json.dumps(
                        {
                            "id": original,
                            "error": {
                                "code": -32001,
                                "message": "Session with given id not found.",
                            },
                            "sessionId": session,
                        }
                    )
                )
                return
        else:
            session = client.browser_session
        msg_id = next(self._ids)
        with self._lock:
            self._pending[msg_id] = (client, original, None)
        out = {**msg, "id": msg_id, "sessionId": session}
        self._write(out)

    def _drop(self, client: _Client, *, detach: bool) -> None:
        if client.closed and client not in self._clients:
            return
        client.closed = True
        if client in self._clients:
            self._clients.remove(client)
        with self._lock:
            owned = list(client.sessions)
            for s in owned:
                self._owners.pop(s, None)
            client.sessions.clear()
        if detach:
            for session in owned:
                try:
                    self._write(
                        {
                            "id": next(self._ids),
                            "method": "Target.detachFromTarget",
                            "params": {"sessionId": session},
                        }
                    )
                except OSError:
                    break
        try:
            client.sock.close()
        except OSError:
            pass
