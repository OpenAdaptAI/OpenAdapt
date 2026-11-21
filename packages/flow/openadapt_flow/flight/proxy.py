"""A loopback proxy in front of the model API your agent already calls.

The wrapped process gets ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL``
pointing here (``http://127.0.0.1:<port>/<token>/anthropic``). Each request:

1. is scrubbed: every base64 image (Anthropic ``image`` blocks, OpenAI
   ``data:`` image URLs) goes through openadapt-privacy image redaction and
   every content text through the text scrubber. A request that can't be
   scrubbed whole (an undecodable image, an image or document by URL, a
   compressed or non-JSON body, a scrubber error) is answered 502 and is
   never forwarded. Thinking and reasoning blocks pass byte-identical (their
   signatures must verify); tool definitions, ids and model names are not
   touched.
2. is checked against the spend cap (429 past it, or when the cap can't be
   enforced because a model has no price).
3. is forwarded over HTTPS to the provider the agent already targets, with
   the agent's own headers (its key included), never logged.
4. is metered: usage from the response (or the final SSE events of a
   stream, passed through as it arrives) is priced from
   :data:`openadapt_flow.compile_agent.agents.PRICES_PER_MTOK`; an unpriced
   model's cost is recorded as unknown, never guessed. Computer actions the
   model asks for are normalised with the JIT translators and logged.

The same server answers ``/<token>/flight/decide`` and
``/<token>/flight/event`` for the executor wrappers in the agent's process
(:mod:`openadapt_flow.flight.computer`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.flight.timeline import Timeline

__all__ = [
    "DEFAULT_UPSTREAMS",
    "FlightProxy",
    "ScrubRefused",
    "price_usd",
    "scrub_request",
]

DEFAULT_UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}
_MAX_BODY = 64 * 1024 * 1024
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}
#: Blocks passed through byte-identical: their signatures must verify.
_OPAQUE_TYPES = {"thinking", "redacted_thinking", "reasoning"}
#: String values scrubbed wherever they appear (content text).
_TEXT_KEYS = {"text", "content", "system", "instructions", "output", "input"}
#: Subtrees never scrubbed: tool definitions and request metadata.
_SKIP_KEYS = {"tools", "tool_choice", "metadata", "model", "response_format"}
_ANTHROPIC_ACTIONS = {
    "screenshot",
    "left_click",
    "right_click",
    "double_click",
    "triple_click",
    "middle_click",
    "mouse_move",
    "left_click_drag",
    "left_mouse_down",
    "left_mouse_up",
    "type",
    "key",
    "hold_key",
    "scroll",
    "wait",
    "zoom",
    "cursor_position",
}


class ScrubRefused(Exception):
    """The request can't be scrubbed whole, so it is not forwarded."""


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def check_upstream(url: str) -> str:
    """Return ``url`` if it is an HTTPS (or loopback HTTP) provider URL."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and _is_loopback(parts.hostname or ""):
        return url.rstrip("/")
    raise ValueError(f"the provider URL must be https:// (got {parts.scheme}://)")


# -- pricing ---------------------------------------------------------------


def _price_row(model: Optional[str]) -> Optional[Mapping[str, float]]:
    from openadapt_flow.compile_agent.agents import PRICES_PER_MTOK

    if not model:
        return None
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    head, _, tail = model.rpartition("-")
    if head and tail.isdigit() and len(tail) == 8:
        return PRICES_PER_MTOK.get(head)
    return None


def price_usd(model: Optional[str], usage: Mapping[str, int]) -> Optional[float]:
    """USD for one call from the price table, or None for an unpriced model."""
    row = _price_row(model)
    if row is None:
        return None
    return (
        usage.get("input_tokens", 0) * row["input"]
        + usage.get("output_tokens", 0) * row["output"]
        + usage.get("cache_creation_input_tokens", 0) * row["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * row["cache_read"]
    ) / 1_000_000


def _usage(raw: Any) -> dict[str, int]:
    """Normalise Anthropic, OpenAI Responses and Chat Completions usage."""
    if not isinstance(raw, Mapping):
        return {}

    def num(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    out = {
        "input_tokens": num(raw.get("input_tokens", raw.get("prompt_tokens"))),
        "output_tokens": num(raw.get("output_tokens", raw.get("completion_tokens"))),
        "cache_creation_input_tokens": num(raw.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": num(raw.get("cache_read_input_tokens")),
    }
    details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details")
    if isinstance(details, Mapping) and not out["cache_read_input_tokens"]:
        out["cache_read_input_tokens"] = num(details.get("cached_tokens"))
    return out


# -- scrubbing ---------------------------------------------------------------


class _Scrub:
    """Memoised scrubbing of one run's request bodies (deterministic, so
    the provider's prompt cache still matches across turns)."""

    def __init__(self, scrubber: Any) -> None:
        self.scrubber = scrubber
        self._images: OrderedDict[str, bytes] = OrderedDict()
        self._texts: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def text(self, value: str) -> str:
        from openadapt_flow.privacy import scrub_outbound_text

        if not value:
            return value
        key = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._texts.get(key)
        if hit is not None:
            return hit
        out = scrub_outbound_text(value, self.scrubber)
        if not isinstance(out, str):
            raise ScrubRefused("the text scrubber returned no text")
        with self._lock:
            self._texts[key] = out
            while len(self._texts) > 8192:
                self._texts.popitem(last=False)
        return out

    def image(self, raw: bytes) -> tuple[bytes, bool]:
        """(scrubbed PNG, whether it is new this run)."""
        import io

        from PIL import Image

        from openadapt_flow.privacy import scrub_outbound_image

        key = hashlib.sha256(raw).hexdigest()
        with self._lock:
            hit = self._images.get(key)
        if hit is not None:
            return hit, False
        try:
            before = Image.open(io.BytesIO(raw))
            before.load()
        except Exception as exc:  # noqa: BLE001 - undecodable: refuse
            raise ScrubRefused(f"an image could not be decoded ({type(exc).__name__})")
        out = scrub_outbound_image(raw, self.scrubber)
        after = Image.open(io.BytesIO(out))
        if after.size != before.size:
            raise ScrubRefused("the image scrubber changed the image size")
        with self._lock:
            self._images[key] = out
            while len(self._images) > 512:
                self._images.popitem(last=False)
        return out, True


def _b64(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ScrubRefused("an image's base64 data could not be decoded") from None


def scrub_request(
    body: Any, scrub: Any, on_frame: Optional[Callable[[bytes], None]] = None
) -> Any:
    """Return a scrubbed copy of a JSON request body, or raise ScrubRefused.

    ``scrub`` has ``text(str)`` and ``image(bytes) -> (png, new)``.
    ``on_frame`` gets each scrubbed image seen for the first time.
    """

    def image(raw: bytes) -> bytes:
        png, new = scrub.image(raw)
        if new and on_frame is not None:
            on_frame(png)
        return png

    def data_url(value: str) -> str:
        head, sep, data = value.partition(",")
        if not sep or ";base64" not in head:
            raise ScrubRefused("an image data URL is not base64")
        png = image(_b64(data))
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    def walk(node: Any, key: Optional[str]) -> Any:
        if isinstance(node, str):
            if node.startswith("data:"):
                if node[5:].lower().startswith("image/"):
                    return data_url(node)
                raise ScrubRefused("a non-image data URL can't be scrubbed")
            if key in ("image_url", "url") and node.startswith(("http://", "https://")):
                raise ScrubRefused("an image by URL can't be scrubbed before it is sent")
            if key in _TEXT_KEYS:
                return scrub.text(node)
            return node
        if isinstance(node, list):
            return [walk(item, key) for item in node]
        if not isinstance(node, dict):
            return node
        kind = node.get("type")
        if kind in _OPAQUE_TYPES:
            return node
        source = node.get("source")
        if kind in ("image", "document") and isinstance(source, dict):
            if kind == "image" and source.get("type") == "base64":
                png = image(_b64(str(source.get("data", ""))))
                return {
                    **{k: walk(v, k) for k, v in node.items() if k != "source"},
                    "source": {
                        **source,
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                }
            if kind == "document" and source.get("type") == "text":
                return {**node, "source": {**source, "data": scrub.text(str(source.get("data", "")))}}
            raise ScrubRefused(f"a {kind} sent as {source.get('type')!r} can't be scrubbed")
        if kind in ("input_file", "file"):
            raise ScrubRefused("a file input can't be scrubbed")
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _SKIP_KEYS or k in ("id", "tool_use_id", "call_id", "name", "signature"):
                out[k] = v
            elif k == "encrypted_content":
                out[k] = v
            else:
                out[k] = walk(v, k)
        return out

    return walk(body, None)


# -- response parsing ---------------------------------------------------------


class _Meter:
    """Collects usage and computer actions from one response."""

    def __init__(self, provider: str, request_model: Optional[str]) -> None:
        self.provider = provider
        self.model = request_model
        self.usage: dict[str, int] = {}
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self._blocks: dict[int, dict[str, Any]] = {}
        self._buffer = b""

    # Anthropic ----------------------------------------------------------
    def _tool_use(self, block: Mapping[str, Any]) -> None:
        name = str(block.get("name", ""))
        payload = block.get("input")
        if not isinstance(payload, dict):
            return
        if name == "computer" and "action" in payload:
            self.actions.append(("anthropic", dict(payload)))
        elif block.get("toolset_name") or name in _ANTHROPIC_ACTIONS:
            if name in _ANTHROPIC_ACTIONS:
                self.actions.append(("anthropic", {"action": name, **payload}))

    def _merge_usage(self, raw: Any) -> None:
        for key, value in _usage(raw).items():
            if value:
                self.usage[key] = value

    def json_body(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        self.model = str(data.get("model") or self.model or "") or None
        if "usage" in data:
            self._merge_usage(data["usage"])
        content = data.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    self._tool_use(block)
        self._openai_output(data.get("output"))

    # OpenAI -------------------------------------------------------------
    def _openai_output(self, output: Any) -> None:
        if not isinstance(output, list):
            return
        for item in output:
            if isinstance(item, Mapping) and item.get("type") == "computer_call":
                self.actions.append(("openai", dict(item)))

    # SSE ----------------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            self._event(event)

    def close(self) -> None:
        if self._buffer.strip():
            self._event(self._buffer)
        self._buffer = b""
        for index in sorted(self._blocks):
            block = self._blocks[index]
            text = block.pop("_json", "")
            if text:
                try:
                    block["input"] = json.loads(text)
                except ValueError:
                    continue
            self._tool_use(block)
        self._blocks.clear()

    def _event(self, raw: bytes) -> None:
        lines = [
            line[5:].lstrip() for line in raw.decode("utf-8", "replace").split("\n")
            if line.startswith("data:")
        ]
        if not lines:
            return
        text = "\n".join(lines)
        if text.strip() == "[DONE]":
            return
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, Mapping):
            return
        kind = data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            self.model = str(message.get("model") or self.model or "") or None
            self._merge_usage(message.get("usage"))
        elif kind == "message_delta":
            self._merge_usage(data.get("usage"))
        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._blocks[int(data.get("index", 0))] = {**block, "_json": ""}
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            index = int(data.get("index", 0))
            if delta.get("type") == "input_json_delta" and index in self._blocks:
                self._blocks[index]["_json"] += str(delta.get("partial_json", ""))
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            response = data.get("response") or {}
            self.model = str(response.get("model") or self.model or "") or None
            self._merge_usage(response.get("usage"))
            self._openai_output(response.get("output"))
        elif "usage" in data and data.get("usage"):  # Chat Completions chunk
            self.model = str(data.get("model") or self.model or "") or None
            self._merge_usage(data.get("usage"))


# -- the server ---------------------------------------------------------------


def _error_body(message: str) -> bytes:
    return json.dumps(
        {"type": "error", "error": {"type": "openadapt_flight", "message": message}}
    ).encode("utf-8")


class FlightProxy:
    """The loopback model-API proxy and gate endpoint for one flight run."""

    def __init__(
        self,
        timeline: Timeline,
        *,
        upstreams: Optional[Mapping[str, str]] = None,
        scrubber: Any = None,
        scrub: bool = True,
        max_cost_usd: Optional[float] = None,
        gate: Any = None,
        timeout_s: float = 600.0,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        if scrub and scrubber is None:
            raise ValueError("scrubbing is on but no scrubber was given")
        self.timeline = timeline
        self.upstreams = {
            name: check_upstream(url)
            for name, url in {**DEFAULT_UPSTREAMS, **dict(upstreams or {})}.items()
        }
        self._scrub = _Scrub(scrubber) if scrub else None
        self.max_cost_usd = max_cost_usd
        self.gate = gate
        self.timeout_s = timeout_s
        self._ssl = ssl_context
        self.token = secrets.token_urlsafe(24)
        self._gate_lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "FlightProxy":
        proxy = self

        class Handler(_Handler):
            owner = proxy

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="flight-proxy", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FlightProxy":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the proxy is not running")
        return int(self._server.server_address[1])

    def base_url(self, provider: str) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/{provider}"

    @property
    def gate_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/flight"

    def child_env(self) -> dict[str, str]:
        """The variables the wrapped process gets."""
        from openadapt_flow.flight.computer import GATE_URL_ENV

        return {
            "ANTHROPIC_BASE_URL": self.base_url("anthropic"),
            "OPENAI_BASE_URL": self.base_url("openai"),
            GATE_URL_ENV: self.gate_url,
        }

    def _ssl_context(self) -> ssl.SSLContext:
        if self._ssl is None:
            try:
                import certifi

                self._ssl = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                self._ssl = ssl.create_default_context()
        return self._ssl

    # -- gate endpoints ---------------------------------------------------
    def decide(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from openadapt_flow.flight.gate import GateAction

        try:
            action = GateAction.from_dict(dict(payload.get("action") or {}))
            shot = payload.get("screenshot_b64")
            png = base64.b64decode(shot) if shot else None
        except Exception:  # noqa: BLE001 - a malformed ask refuses
            return {"decision": "refuse"}
        decision = "refuse"
        if self.gate is not None:
            with self._gate_lock:
                try:
                    decision = self.gate.decide(action, png)
                except Exception:  # noqa: BLE001
                    decision = "refuse"
        if decision not in ("allow", "hold", "refuse"):
            decision = "refuse"
        kind = {"allow": "allowed", "hold": "held", "refuse": "refused"}[decision]
        self.timeline.gate_event(kind, action.reason, label=action.label, frame_png=png)
        return {"decision": decision}

    def event(self, payload: Mapping[str, Any]) -> None:
        fields = {
            k: payload.get(k)
            for k in ("dialect", "action", "commit_class", "label", "label_source", "decision")
        }
        frame = payload.get("frame_b64")
        try:
            png = base64.b64decode(frame) if frame else None
        except (binascii.Error, ValueError):
            png = None
        self.timeline.executor_action(
            dialect=str(fields["dialect"] or ""),
            action=dict(fields["action"] or {}),
            commit_class=bool(fields["commit_class"]),
            label=fields["label"],
            label_source=str(fields["label_source"] or "none"),
            decision=str(fields["decision"] or ""),
            frame_png=png,
        )

    # -- spend cap --------------------------------------------------------
    def spend_refusal(self, model: Optional[str]) -> Optional[str]:
        cap = self.max_cost_usd
        if cap is None:
            return None
        if self.timeline.unpriced_models or _price_row(model) is None:
            return (
                f"the spend cap (${cap:.2f}) can't be enforced: model "
                f"{model or 'unknown'!r} has no price, so the call was not sent"
            )
        spent = self.timeline.priced_cost()
        if spent >= cap:
            return (
                f"the spend cap (${cap:.2f}) is reached (${spent:.4f} spent), "
                "so the call was not sent"
            )
        return None

    # -- one model call ---------------------------------------------------
    def prepare(self, provider: str, raw_body: bytes, headers: Mapping[str, str]) -> bytes:
        """The body to forward. Raises ScrubRefused to answer 502."""
        if not raw_body:
            return raw_body
        if self._scrub is None:
            return raw_body
        encoding = (headers.get("content-encoding") or "identity").lower()
        if encoding not in ("identity", ""):
            raise ScrubRefused("a compressed request body can't be scrubbed")
        try:
            body = json.loads(raw_body)
        except ValueError:
            raise ScrubRefused("a request body that is not JSON can't be scrubbed") from None

        def frame(png: bytes) -> None:
            rel = self.timeline.frame(png, scrubbed=True)
            self.timeline.record("frame", provider=provider, source="request", frame=rel)

        try:
            clean = scrub_request(body, self._scrub, on_frame=frame)
        except ScrubRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - any scrub failure refuses
            raise ScrubRefused(f"scrubbing failed ({type(exc).__name__})") from None
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def record_response(self, meter: _Meter, status: int, streamed: bool, seconds: float) -> None:
        from openadapt_flow.jit.adapters import UnsupportedAction, translate_anthropic, translate_openai

        cost = price_usd(meter.model, meter.usage)
        self.timeline.model_call(
            provider=meter.provider,
            model=meter.model,
            usage=meter.usage,
            cost_usd=None if cost is None else round(cost, 8),
            streamed=streamed,
            status=status,
            seconds=seconds,
        )
        for dialect, raw in meter.actions:
            note = None
            try:
                ops = translate_anthropic(raw) if dialect == "anthropic" else translate_openai(raw)
                op_dicts = [
                    {k: v for k, v in asdict(op).items() if v not in (None, (), 0, 0.0, False)}
                    for op in ops
                ]
            except (UnsupportedAction, KeyError, TypeError, ValueError) as exc:
                op_dicts, note = [], f"not translatable: {exc}"
            action = raw.get("action") if dialect == "openai" and isinstance(raw.get("action"), dict) else raw
            if dialect == "openai" and isinstance(raw.get("actions"), list):
                action = {"actions": len(raw["actions"]), "call_id": raw.get("call_id")}
            self.timeline.model_action(
                provider=meter.provider,
                dialect=dialect,
                action=action if isinstance(action, dict) else {},
                ops=op_dicts,
                note=note,
            )


class _Handler(BaseHTTPRequestHandler):
    owner: FlightProxy
    protocol_version = "HTTP/1.1"
    server_version = "openadapt-flight"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The path holds the run's token; nothing is logged.
        return None

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _route(self) -> Optional[tuple[str, str]]:
        path = self.path
        parts = path.split("/", 3)
        # "", token, provider, rest
        if len(parts) < 3 or not hmac.compare_digest(parts[1], self.owner.token):
            return None
        provider = parts[2].split("?", 1)[0]
        rest = "/" + parts[3] if len(parts) > 3 else ""
        if len(parts) == 3 and "?" in parts[2]:
            rest = "?" + parts[2].split("?", 1)[1]
        return provider, rest

    def _body(self) -> Optional[bytes]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._send(411, _error_body("send a Content-Length; chunked bodies are refused"))
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self._send(413, _error_body("request body too large"))
            return None
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        route = self._route()
        if route is None:
            self._send(404, _error_body("not found"))
            return
        provider, rest = route
        body = self._body()
        if body is None:
            return
        owner = self.owner
        if provider == "flight":
            self._flight(method, rest, body)
            return
        upstream = owner.upstreams.get(provider)
        if upstream is None:
            self._send(404, _error_body(f"unknown provider {provider!r}"))
            return
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            request_model = None
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        request_model = parsed.get("model")
                except ValueError:
                    pass
            refusal = owner.spend_refusal(request_model) if method == "POST" and body else None
            if refusal:
                owner.timeline.refused_request(provider=provider, status=429, reason=refusal)
                self._send(429, _error_body(refusal))
                return
            forward = owner.prepare(provider, body, headers)
        except ScrubRefused as exc:
            message = f"not sent: {exc}. Nothing left this machine."
            owner.timeline.refused_request(provider=provider, status=502, reason=str(exc))
            self._send(502, _error_body(message))
            return
        self._forward(method, provider, upstream, rest, forward, request_model)

    def _flight(self, method: str, rest: str, body: bytes) -> None:
        if method != "POST":
            self._send(405, _error_body("POST only"))
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._send(400, _error_body("not JSON"))
            return
        if rest == "/decide":
            self._send(200, json.dumps(self.owner.decide(payload)).encode("utf-8"))
        elif rest == "/event":
            self.owner.event(payload)
            self._send(200, b"{}")
        else:
            self._send(404, _error_body("not found"))

    def _forward(
        self,
        method: str,
        provider: str,
        upstream: str,
        rest: str,
        body: bytes,
        request_model: Optional[str],
    ) -> None:
        owner = self.owner
        target = urlsplit(upstream)
        path = (target.path or "") + rest
        out_headers = {
            k: v for k, v in self.headers.items() if k.lower() not in _HOP
        }
        out_headers["Host"] = target.netloc
        out_headers["Accept-Encoding"] = "identity"
        if body or method in ("POST", "PUT", "PATCH"):
            out_headers["Content-Length"] = str(len(body))
        started = time.monotonic()
        if target.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                target.hostname or "", target.port or 443,
                timeout=owner.timeout_s, context=owner._ssl_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                target.hostname or "", target.port or 80, timeout=owner.timeout_s
            )
        meter = _Meter(provider, request_model)
        streamed = False
        try:
            conn.request(method, path or "/", body=body or None, headers=out_headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            reason = f"the provider could not be reached ({type(exc).__name__})"
            owner.timeline.refused_request(provider=provider, status=502, reason=reason)
            self._send(502, _error_body(reason))
            return
        try:
            content_type = response.getheader("Content-Type") or ""
            encoded = (response.getheader("Content-Encoding") or "identity").lower() not in (
                "identity",
                "",
            )
            streamed = "text/event-stream" in content_type
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in _HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            if streamed:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    if not encoded:
                        meter.feed(chunk)
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                meter.close()
            else:
                data = response.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if not encoded and "json" in content_type:
                    try:
                        meter.json_body(json.loads(data))
                    except ValueError:
                        pass
            self.close_connection = True
        except (OSError, http.client.HTTPException):
            self.close_connection = True
        finally:
            conn.close()
            if method == "POST":
                owner.record_response(
                    meter, response.status, streamed, time.monotonic() - started
                )
