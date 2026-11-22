"""A stub BYOC control plane for the connector service tests.

It listens on 127.0.0.1 only and answers the outbound endpoints the
connector calls:

* ``POST /api/connector/poll``: 204 for the first ``--empty-polls`` polls
  (after a short sleep, so a client without a long-poll wait does not spin),
  then exactly one job, then 204 again. The job parses as a BYOC dispatch but
  carries no ``safety`` block, so the connector must refuse it before it
  fetches a bundle or touches any GUI.
* ``POST /api/internal/run-callback``: 200.
* ``POST /api/connector/ack``: 200.
* ``POST /api/connector/rotate``: following a test rotation request, accept
  the new token digest and require its matching bearer on subsequent polls.

Every request is appended to a JSON file (method, path, sorted header names,
parsed body). Header values are never recorded, so the bearer token never
reaches the log.

    python stub_server.py --log requests.json --port-file port.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

JOB_ID = "bjob_ungoverned_1"
RUN_ID = "00000000-0000-4000-8000-0000000000aa"


def ungoverned_job() -> dict[str, Any]:
    """A dispatch that parses but has no safety policy (must be refused)."""
    return {
        "id": JOB_ID,
        "payload": {
            "mode": "replay",
            "run_id": RUN_ID,
            "org_id": "org_stub",
            "workflow_id": "wf_stub",
            "target_kind": "windows",
            "storage": {
                "backend": "local",
                "bundle_ref": "bundles/wf_stub/bundle.zip",
                "report_ref": "org_stub/run_aa/report.json",
            },
            "run_token": "r" * 64,
            "bundle_version_id": "bv_stub",
            "runtime_validation_id": "rv_stub",
            "bundle_sha256": "0" * 64,
            # No "safety" key: ByocJob.ensure_governed refuses this dispatch.
        },
    }


class StubState:
    def __init__(self, log_path: Path, empty_polls: int, empty_sleep: float):
        self.log_path = log_path
        self.empty_polls = empty_polls
        self.empty_sleep = empty_sleep
        self.polls = 0
        self.job_sent = False
        self.records: list[dict[str, Any]] = []
        self.active_empty_polls = 0
        self.auth_denials = 0
        self.deny_auth = False
        self.rotate_requested = False
        self.rotated_digest: Optional[str] = None
        self.rotated_polls = 0
        self.lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(entry)
            tmp = self.log_path.with_suffix(self.log_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.records, indent=2), encoding="utf-8")
            tmp.replace(self.log_path)

    def next_poll(self) -> Optional[dict[str, Any]]:
        with self.lock:
            self.polls += 1
            if self.polls > self.empty_polls and not self.job_sent:
                self.job_sent = True
                return ungoverned_job()
        return None


def make_handler(state: StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _reply(
            self,
            status: int,
            body: Optional[dict[str, Any]],
            *,
            recommend_rotation: bool = False,
        ) -> None:
            data = json.dumps(body).encode("utf-8") if body is not None else b""
            self.send_response(status)
            if recommend_rotation:
                self.send_header(
                    "x-oa-connector-control", '{"rotate_recommended":true}'
                )
            if data:
                self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            # Test-only lifecycle controls; this server binds loopback only.
            if self.path == "/test/deny-auth":
                with state.lock:
                    state.deny_auth = True
            elif self.path == "/test/allow-auth":
                with state.lock:
                    state.deny_auth = False
            elif self.path == "/test/request-rotation":
                with state.lock:
                    state.rotate_requested = True
            elif self.path != "/test/state":
                return self._reply(404, {"error": "not_found"})
            with state.lock:
                snapshot = {
                    "polls": state.polls,
                    "active_empty_polls": state.active_empty_polls,
                    "auth_denials": state.auth_denials,
                    "rotated_polls": state.rotated_polls,
                }
            self._reply(200, snapshot)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body: Any = json.loads(raw) if raw else None
            except ValueError:
                body = {"unparsed_bytes": len(raw)}
            path = self.path.split("?", 1)[0]
            state.record(
                {
                    "method": "POST",
                    "path": path,
                    "headers": sorted(k.lower() for k in self.headers.keys()),
                    "body": body,
                    "t": time.time(),
                }
            )
            auth = self.headers.get("authorization", "")
            token_digest = (
                hashlib.sha256(auth[7:].encode()).hexdigest()
                if auth.startswith("Bearer ")
                else None
            )
            if path == "/api/connector/rotate":
                candidate = (
                    body.get("new_token_sha256") if isinstance(body, dict) else None
                )
                if not isinstance(candidate, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", candidate
                ):
                    return self._reply(400, {"error": "invalid_candidate"})
                with state.lock:
                    accepted = (
                        state.rotate_requested
                        and token_digest is not None
                        and candidate != token_digest
                    )
                    if accepted:
                        state.rotated_digest = candidate
                        state.rotate_requested = False
                return self._reply(200 if accepted else 409, {"ok": accepted})
            if path == "/api/connector/poll":
                with state.lock:
                    deny_auth = state.deny_auth or (
                        state.rotated_digest is not None
                        and token_digest != state.rotated_digest
                    )
                    recommend_rotation = state.rotate_requested
                    if deny_auth:
                        state.auth_denials += 1
                    elif state.rotated_digest is not None:
                        state.rotated_polls += 1
                if deny_auth:
                    # Deliberately hostile response: the service log must
                    # remove its token even when a server echoes it back.
                    return self._reply(
                        401, {"error": self.headers.get("authorization", "")}
                    )
                job = state.next_poll()
                if job is None:
                    with state.lock:
                        state.active_empty_polls += 1
                    try:
                        time.sleep(state.empty_sleep)
                        self._reply(204, None, recommend_rotation=recommend_rotation)
                    finally:
                        with state.lock:
                            state.active_empty_polls -= 1
                else:
                    self._reply(200, {"job": job})
            elif path == "/api/internal/run-callback":
                self._reply(200, {"ok": True})
            elif path == "/api/connector/ack":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not_found"})

    return Handler


def serve(
    log_path: Path,
    *,
    port: int = 0,
    empty_polls: int = 2,
    empty_sleep: float = 1.0,
) -> tuple[ThreadingHTTPServer, StubState]:
    """Start the stub on 127.0.0.1 in a background thread."""
    state = StubState(log_path, empty_polls, empty_sleep)
    log_path.write_text("[]", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--log", required=True, help="JSON request log path")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", help="Write the bound port here")
    parser.add_argument("--empty-polls", type=int, default=2)
    parser.add_argument("--empty-sleep", type=float, default=1.0)
    args = parser.parse_args()
    server, _ = serve(
        Path(args.log),
        port=args.port,
        empty_polls=args.empty_polls,
        empty_sleep=args.empty_sleep,
    )
    bound = server.server_address[1]
    if args.port_file:
        Path(args.port_file).write_text(str(bound), encoding="utf-8")
    print(f"stub control plane on 127.0.0.1:{bound}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
