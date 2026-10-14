"""A scripted OpenAI-compatible policy for openadapt-mockmed-extradup.

It answers ``POST /v1/chat/completions`` with the action report for the
case named by the model: ``scripted/control`` is the gold write,
``scripted/dup`` the duplicate CREATE, and so on through
``openadapt_mockmed_extradup.ALL_CASES``. It reads the spec from the
prompt's SPEC line, so it needs no state and no model.

Serve it, then point ``vf-eval`` at it:

    python scripted_policy.py serve --port 8123
    SCRIPTED_POLICY_KEY=scripted vf-eval openadapt-mockmed-extradup \\
        -m scripted/dup -b http://127.0.0.1:8123/v1 -k SCRIPTED_POLICY_KEY

``SCRIPTED_POLICY_KEY`` is a placeholder the OpenAI client insists on. The
server never reads it.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import openadapt_mockmed_extradup as env_module


def case_for_model(model: str) -> str:
    case = model.rsplit("/", 1)[-1]
    if case not in env_module.ALL_CASES:
        raise KeyError(f"unknown scripted case {case!r}; choose from {env_module.ALL_CASES}")
    return case


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def completion_for(body: dict[str, Any]) -> dict[str, Any]:
    case = case_for_model(str(body.get("model", "")))
    spec = env_module.spec_from_prompt(_last_user_text(body.get("messages", [])))
    text = env_module.scripted_completion(case, spec)
    return {
        "id": f"scripted-{case}-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, document: dict[str, Any]) -> None:
        payload = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": f"scripted/{case}", "object": "model"}
                        for case in env_module.ALL_CASES
                    ],
                },
            )
            return
        self._send(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            document = completion_for(body)
        except (KeyError, ValueError) as error:
            self._send(400, {"error": {"message": str(error)}})
            return
        self._send(200, document)


def serve(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("serve", help="serve the scripted policy until interrupted")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8123)
    args = parser.parse_args(argv)
    server = serve(args.host, args.port)
    print(f"scripted policy on http://{args.host}:{server.server_address[1]}/v1", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
