"""A loopback stand-in for the Anthropic and OpenAI model APIs.

It answers ``POST /v1/messages`` (Anthropic) and ``POST /v1/responses``
(OpenAI Responses) in the providers' recorded shapes, with usage and
computer actions, streaming (SSE, split across TCP writes) or not. It keeps
every request body it received, byte for byte, so a test can scan exactly
what left the proxy.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

ANTHROPIC_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 80,
    "cache_creation_input_tokens": 300,
    "cache_read_input_tokens": 500,
}
OPENAI_USAGE = {
    "input_tokens": 900,
    "output_tokens": 40,
    "input_tokens_details": {"cached_tokens": 100},
}


class FakeProvider:
    """Answers from a script of turns; records what it received."""

    def __init__(self, model: str = "claude-opus-5-5") -> None:
        self.model = model
        self.requests: list[dict[str, Any]] = []
        #: Anthropic turns: lists of content blocks; popped in order.
        self.anthropic_turns: list[list[dict[str, Any]]] = []
        #: OpenAI turns: lists of output items.
        self.openai_turns: list[list[dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "FakeProvider":
        owner = self

        class Handler(_Handler):
            provider = owner

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def received_bytes(self) -> bytes:
        return b"\n".join(r["body"] for r in self.requests)

    # -- answers ----------------------------------------------------------
    def next_anthropic(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.anthropic_turns:
                return self.anthropic_turns.pop(0)
        return [{"type": "text", "text": "Done."}]

    def next_openai(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.openai_turns:
                return self.openai_turns.pop(0)
        return [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            }
        ]


def anthropic_click(x: int, y: int, tool_id: str = "toolu_1") -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": "computer",
        "input": {"action": "left_click", "coordinate": [x, y]},
    }


def openai_click(x: int, y: int, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "type": "computer_call",
        "id": "cu_" + call_id,
        "call_id": call_id,
        "action": {"type": "click", "x": x, "y": y, "button": "left"},
        "pending_safety_checks": [],
        "status": "completed",
    }


class _Handler(BaseHTTPRequestHandler):
    provider: FakeProvider
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.provider.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        try:
            request = json.loads(body)
        except ValueError:
            request = {}
        stream = bool(request.get("stream"))
        if self.path.endswith("/v1/messages"):
            blocks = self.provider.next_anthropic()
            if stream:
                self._anthropic_stream(request, blocks)
            else:
                self._json(200, self._anthropic_message(request, blocks))
        elif self.path.endswith("/v1/responses"):
            items = self.provider.next_openai()
            response = {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": request.get("model", "computer-use-preview"),
                "output": items,
                "usage": OPENAI_USAGE,
            }
            if stream:
                self._sse(
                    [
                        {
                            "type": "response.created",
                            "response": {**response, "output": []},
                        },
                        {"type": "response.completed", "response": response},
                    ]
                )
            else:
                self._json(200, response)
        else:
            self._json(404, {"error": {"message": "unknown path"}})

    def _anthropic_message(
        self, request: dict[str, Any], blocks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        tool = any(b.get("type") == "tool_use" for b in blocks)
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": self.provider.model,
            "content": blocks,
            "stop_reason": "tool_use" if tool else "end_turn",
            "stop_sequence": None,
            "usage": ANTHROPIC_USAGE,
        }

    def _json(self, status: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _sse(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in events:
            raw = (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode(
                "utf-8"
            )
            # Split each event across two writes, so the proxy sees partial
            # events on the wire.
            for part in (raw[: len(raw) // 2], raw[len(raw) // 2 :]):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
                time.sleep(0.002)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    def _anthropic_stream(
        self, request: dict[str, Any], blocks: list[dict[str, Any]]
    ) -> None:
        usage_start = {
            "input_tokens": ANTHROPIC_USAGE["input_tokens"],
            "output_tokens": 1,
            "cache_creation_input_tokens": ANTHROPIC_USAGE[
                "cache_creation_input_tokens"
            ],
            "cache_read_input_tokens": ANTHROPIC_USAGE["cache_read_input_tokens"],
        }
        message = self._anthropic_message(request, [])
        message["usage"] = usage_start
        message["stop_reason"] = None
        events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
        tool = False
        for index, block in enumerate(blocks):
            if block["type"] == "tool_use":
                tool = True
                events.append(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {**block, "input": {}},
                    }
                )
                text = json.dumps(block["input"])
                cut = len(text) // 2
                for fragment in (text[:cut], text[cut:]):
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": fragment,
                            },
                        }
                    )
            else:
                events.append(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block.get("text", "")},
                    }
                )
            events.append({"type": "content_block_stop", "index": index})
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
                "usage": {"output_tokens": ANTHROPIC_USAGE["output_tokens"]},
            }
        )
        events.append({"type": "message_stop"})
        self._sse(events)
