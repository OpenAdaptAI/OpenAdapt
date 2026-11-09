"""FastAPI application for the on-prem VLM inference service.

One open VLM, loaded once, serves the whole GPU-less fleet over the LAN. All
inference endpoints are gated by a shared bearer token and funnelled through a
:class:`MicroBatcher` so concurrent runners share the single GPU efficiently.

The server NEVER authorizes an action. Identity ``compare`` reports a veto-only
same/different judgement; ``ground`` proposes a point (the deterministic
identity band still disposes); ``verify_state`` reports a semantic postcondition.
The safety decision always lives in the runner.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException

from openadapt_flow.services.vlm_service.backends import (
    InferenceBackend,
    build_backend,
    compose_identifier_pair,
)
from openadapt_flow.services.vlm_service.batching import MicroBatcher
from openadapt_flow.services.vlm_service.config import ServiceConfig
from openadapt_flow.services.vlm_service.prompts import (
    GROUND_PROMPT,
    IDENTITY_PROMPT,
    VERIFY_STATE_PROMPT,
    parse_identity_veto,
    parse_state_answer,
)
from openadapt_flow.services.vlm_service.schemas import (
    GroundRequest,
    GroundResponse,
    HealthResponse,
    IdentityCompareRequest,
    IdentityCompareResponse,
    ReadyResponse,
    VerifyStateRequest,
    VerifyStateResponse,
)

# --- batcher payloads ------------------------------------------------------


@dataclass
class _IdentityJob:
    png_a: bytes
    png_b: bytes


@dataclass
class _GroundJob:
    screenshot: bytes
    intent: str
    ocr_text: Optional[str]


@dataclass
class _StateJob:
    screenshot: bytes
    expected_state: str


def _decode_png(b64: str, field: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid base64 for {field}"
        ) from exc


def _extract_json_object(text: str) -> Optional[dict]:
    match = re.search(r"\{.*?\}", text or "", re.DOTALL)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def create_app(
    config: Optional[ServiceConfig] = None,
    backend: Optional[InferenceBackend] = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        config: service configuration (defaults to :meth:`ServiceConfig.from_env`).
        backend: pre-built inference backend (tests inject a stub); otherwise the
            backend is constructed from ``config.backend`` and loaded on startup.
    """
    cfg = config or ServiceConfig.from_env()
    bk = backend if backend is not None else build_backend(cfg.backend, cfg.model)

    def _handle(payload: object) -> object:
        """Blocking per-request work; runs in the batcher's worker thread."""
        if isinstance(payload, _IdentityJob):
            pair = compose_identifier_pair(payload.png_a, payload.png_b)
            raw = bk.generate(IDENTITY_PROMPT, [pair], cfg.max_tokens)
            return parse_identity_veto(raw)
        if isinstance(payload, _StateJob):
            prompt = VERIFY_STATE_PROMPT.format(expected_state=payload.expected_state)
            raw = bk.generate(prompt, [payload.screenshot], cfg.max_tokens)
            return parse_state_answer(raw)
        if isinstance(payload, _GroundJob):
            prompt = GROUND_PROMPT.format(
                intent=payload.intent, ocr_text=payload.ocr_text or "(none)"
            )
            raw = bk.generate(prompt, [payload.screenshot], cfg.ground_max_tokens)
            return _extract_json_object(raw)
        raise TypeError(f"unknown payload: {type(payload)!r}")

    batcher = MicroBatcher(
        _handle, window_ms=cfg.window_ms, max_batch_size=cfg.max_batch_size
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not bk.is_ready():
            bk.load()
        batcher.start()
        yield
        await batcher.stop()

    app = FastAPI(title="openadapt-flow VLM service", lifespan=lifespan)

    def require_auth(authorization: str = Header(default="")) -> None:
        """Reject requests without the shared bearer token.

        An empty configured token disables auth (dev only); production always
        sets ``VLM_SERVICE_TOKEN``.
        """
        if not cfg.token:
            return
        expected = f"Bearer {cfg.token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    # --- health / readiness (unauthenticated) ---
    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/ready", response_model=ReadyResponse)
    async def ready() -> ReadyResponse:
        return ReadyResponse(
            ready=bk.is_ready(), backend=bk.name, model=getattr(bk, "model", None)
        )

    # --- inference (authenticated) ---
    @app.post(
        "/v1/identity/compare",
        response_model=IdentityCompareResponse,
        dependencies=[Depends(require_auth)],
    )
    async def identity_compare(req: IdentityCompareRequest) -> IdentityCompareResponse:
        t0 = time.time()
        job = _IdentityJob(
            _decode_png(req.crop_a, "crop_a"), _decode_png(req.crop_b, "crop_b")
        )
        verdict = await batcher.submit(job)
        # verdict is "same" | "different"; the server never emits authorization.
        out = verdict if verdict in ("same", "different") else "uncertain"
        return IdentityCompareResponse(
            verdict=out, latency_ms=(time.time() - t0) * 1000.0
        )

    @app.post(
        "/v1/ground",
        response_model=GroundResponse,
        dependencies=[Depends(require_auth)],
    )
    async def ground(req: GroundRequest) -> GroundResponse:
        t0 = time.time()
        job = _GroundJob(
            _decode_png(req.screenshot, "screenshot"),
            req.target_description,
            req.ocr_text,
        )
        payload = await batcher.submit(job)
        latency = (time.time() - t0) * 1000.0
        if not payload:
            return GroundResponse(point=None, confidence=0.0, latency_ms=latency)
        x, y = payload.get("x"), payload.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return GroundResponse(point=None, confidence=0.0, latency_ms=latency)
        return GroundResponse(
            point=(int(round(x)), int(round(y))), confidence=0.5, latency_ms=latency
        )

    @app.post(
        "/v1/verify_state",
        response_model=VerifyStateResponse,
        dependencies=[Depends(require_auth)],
    )
    async def verify_state(req: VerifyStateRequest) -> VerifyStateResponse:
        t0 = time.time()
        job = _StateJob(_decode_png(req.screenshot, "screenshot"), req.expected_state)
        holds = await batcher.submit(job)
        out = holds if holds in ("yes", "no") else "uncertain"
        return VerifyStateResponse(holds=out, latency_ms=(time.time() - t0) * 1000.0)

    app.state.config = cfg
    app.state.backend = bk
    app.state.batcher = batcher
    return app
