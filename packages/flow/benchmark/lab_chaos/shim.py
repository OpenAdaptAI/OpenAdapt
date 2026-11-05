"""HTTP chaos in front of Toxiproxy: gateway errors and a lagging list view.

Toxiproxy adds latency at the TCP level. This shim adds what TCP can't:

- each request gets HTTP 502 with probability ``--p502`` and 504 with
  ``--p504`` (seeded);
- for ``--stale-s`` seconds after any POST (a save), a GET whose path starts
  with one of ``--stale-path`` is answered with the body this shim last
  served for that exact URL before the POST: a search index that hasn't
  caught up yet.

It rewrites ``Location`` headers and absolute links from the upstream origin
to its own, so a redirect never bypasses it. Stdlib only.

    python3 chaos_shim.py --listen 9650 --upstream 127.0.0.1:9651 \\
        --public lab-host.invalid:9650 --app-origin lab-host.invalid:9635 \\
        --stale-path /societe/list.php --seed 7 --log chaos.jsonl
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


def make_handler(args: argparse.Namespace) -> type[BaseHTTPRequestHandler]:
    rng = random.Random(args.seed)
    lock = threading.Lock()
    state = {"last_post": 0.0}
    cache: dict[str, tuple[int, list[tuple[str, str]], bytes]] = {}
    host, port = args.upstream.split(":")
    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def note(**row: object) -> None:
        if log is not None:
            with lock:
                log.write(json.dumps({"t": round(time.time(), 3), **row}) + "\n")
                log.flush()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: object) -> None:  # quiet
            pass

        def _send(
            self, status: int, headers: list[tuple[str, str]], body: bytes
        ) -> None:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with lock:
                roll = rng.random()
            if roll < args.p502 + args.p504:
                code = 502 if roll < args.p502 else 504
                note(kind="gateway", status=code, method=self.command, path=self.path)
                self._send(code, [("Content-Type", "text/plain")], b"gateway error")
                return
            stale_path = self.command == "GET" and any(
                self.path.startswith(p) for p in args.stale_path
            )
            if (
                stale_path
                and time.time() - state["last_post"] < args.stale_s
                and self.path in cache
            ):
                status, headers, cached = cache[self.path]
                note(kind="stale", path=self.path)
                self._send(status, headers, cached)
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Host"] = args.app_origin
            headers["Accept-Encoding"] = "identity"
            for name in ("Origin", "Referer"):
                if name in headers:
                    headers[name] = headers[name].replace(args.public, args.app_origin)
            conn = http.client.HTTPConnection(host, int(port), timeout=60)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                out = []
                for k, v in resp.getheaders():
                    if k.lower() in HOP:
                        continue
                    if k.lower() == "location":
                        v = v.replace(args.app_origin, args.public)
                    out.append((k, v))
                ctype = resp.getheader("Content-Type") or ""
                if "html" in ctype or "javascript" in ctype or "json" in ctype:
                    data = data.replace(args.app_origin.encode(), args.public.encode())
                status = resp.status
            except Exception as exc:  # noqa: BLE001 - upstream down
                note(kind="upstream_error", error=type(exc).__name__, path=self.path)
                self._send(502, [("Content-Type", "text/plain")], b"upstream error")
                return
            finally:
                conn.close()
            if self.command == "POST":
                state["last_post"] = time.time()
                note(kind="post", path=self.path)
            elif stale_path and status == 200:
                cache[self.path] = (status, out, data)
            self._send(status, out, data)

        do_GET = do_POST = do_HEAD = _handle

    return Handler


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", type=int, required=True)
    p.add_argument("--upstream", required=True, help="host:port (Toxiproxy)")
    p.add_argument("--public", required=True, help="host:port clients use")
    p.add_argument("--app-origin", required=True, help="host:port the app thinks it is")
    p.add_argument("--p502", type=float, default=0.01)
    p.add_argument("--p504", type=float, default=0.01)
    p.add_argument("--stale-s", type=float, default=3.0)
    p.add_argument("--stale-path", action="append", default=[])
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--log", default="")
    args = p.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.listen), make_handler(args)).serve_forever()


if __name__ == "__main__":
    main()
