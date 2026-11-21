"""Small real servers for the txplan tests: a JSON REST API and a mail sink.

They speak real HTTP and SMTP over loopback sockets. The lab harness
(``benchmark/ext_tx``) runs the same flows against Vikunja, Mailpit and a
MariaDB-backed app.
"""

from __future__ import annotations

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


class RestStore:
    """Records with an id, a server-set ``updated`` and ``done`` -> ``done_at``."""

    def __init__(self, *, replace: bool = False) -> None:
        self.records: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.replace = replace
        self.lock = threading.Lock()
        self.writes = 0

    def add(self, **fields: Any) -> dict[str, Any]:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            rec = {
                "id": rid,
                "done": False,
                "done_at": None,
                **fields,
                "updated": time.time(),
            }
            self.records[rid] = rec
            return dict(rec)

    def server(self) -> tuple[ThreadingHTTPServer, str]:
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def _id(self) -> Optional[int]:
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "items"]:
                    return int(parts[2])
                return None

            def do_GET(self) -> None:  # noqa: N802
                path, _, query = self.path.partition("?")
                if path == "/api/items":
                    q = dict(p.split("=") for p in query.split("&") if "=" in p)
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 50))
                    items = [store.records[k] for k in sorted(store.records)]
                    return self._send(200, items[(page - 1) * per : page * per])
                rid = self._id()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                self._send(200, store.records[rid])

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/items":
                    return self._send(404, {})
                store.writes += 1
                self._send(201, store.add(**self._body()))

            def do_PATCH(self) -> None:  # noqa: N802
                rid = self._id()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                body = self._body()
                store.writes += 1
                with store.lock:
                    old = store.records[rid]
                    if store.replace:
                        new = {"id": rid, "done": False, "done_at": None, **body}
                    else:
                        new = {**old, **body}
                    new["id"] = rid
                    if new.get("done") != old.get("done"):
                        new["done_at"] = time.time() if new.get("done") else None
                    new["updated"] = time.time()
                    store.records[rid] = new
                self._send(200, new)

            def do_DELETE(self) -> None:  # noqa: N802
                rid = self._id()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                store.writes += 1
                with store.lock:
                    del store.records[rid]
                self._send(204)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class MailSink:
    """An SMTP server that keeps messages, and a Mailpit-shaped list API."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        sink = self

        class Smtp(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.wfile.write(b"220 sink\r\n")
                data: Optional[list[bytes]] = None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    if data is not None:
                        if line in (b".\r\n", b".\n"):
                            sink._store(b"".join(data))
                            data = None
                            self.wfile.write(b"250 ok\r\n")
                        else:
                            data.append(line[1:] if line.startswith(b"..") else line)
                        continue
                    cmd = line[:4].upper()
                    if cmd == b"EHLO":
                        self.wfile.write(b"250 sink\r\n")
                    elif cmd == b"DATA":
                        data = []
                        self.wfile.write(b"354 go\r\n")
                    elif cmd == b"QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"messages": list(sink.messages)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Smtp)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        for s in (self.smtp, self.api):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.smtp_port = self.smtp.server_address[1]
        self.api_url = f"http://127.0.0.1:{self.api.server_address[1]}"

    def _store(self, raw: bytes) -> None:
        from email import message_from_bytes

        msg = message_from_bytes(raw)
        self.messages.append(
            {
                "ID": str(len(self.messages) + 1),
                "MessageID": str(msg["Message-ID"] or "").strip("<>"),
                "Subject": str(msg["Subject"] or ""),
                "To": [
                    {"Address": a.strip()}
                    for a in str(msg["To"] or "").split(",")
                    if a.strip()
                ],
            }
        )

    def close(self) -> None:
        for s in (self.smtp, self.api):
            s.shutdown()
            s.server_close()
