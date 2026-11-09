"""Wire contract (pydantic models) shared by the VLM service and its client.

Keeping the request/response schemas in one module means the FastAPI server and
the ``runtime.remote_vlm`` client cannot drift apart: both import these types.
Images cross the wire as base64-encoded PNG strings.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# --- /v1/identity/compare --------------------------------------------------


class IdentityCompareRequest(BaseModel):
    """Two identifier crops to compare (same record or different?)."""

    crop_a: str = Field(description="Base64-encoded PNG of identifier crop A")
    crop_b: str = Field(description="Base64-encoded PNG of identifier crop B")


class IdentityCompareResponse(BaseModel):
    """Veto-only verdict. NEVER an authorization -- only a comparison report.

    ``verdict`` is ``same`` only for a clean, confident match; anything else is
    ``different`` (an explicit mismatch) or ``uncertain`` (unparseable / backend
    error). The client treats BOTH ``different`` and ``uncertain`` as a veto.
    """

    verdict: Literal["same", "different", "uncertain"]
    confidence: Optional[float] = None
    latency_ms: float = 0.0


# --- /v1/ground ------------------------------------------------------------


class GroundRequest(BaseModel):
    """A screenshot plus the target to locate on it."""

    screenshot: str = Field(description="Base64-encoded PNG of the screen")
    target_description: str = Field(description="Human-readable target intent")
    ocr_text: Optional[str] = Field(
        default=None, description="Text label at/near the target, if known"
    )
    viewport: Optional[tuple[int, int]] = Field(
        default=None, description="Optional (width, height) of the screen"
    )


class GroundResponse(BaseModel):
    """A proposed click point, or ``point=None`` when the target is not found.

    The grounder only PROPOSES coordinates; the deterministic identity band
    still disposes before any click. ``point=None`` => the runner degrades to a
    safe-halt (no proposal), never a wrong click.
    """

    point: Optional[tuple[int, int]] = None
    confidence: float = 0.0
    latency_ms: float = 0.0


# --- /v1/verify_state ------------------------------------------------------


class VerifyStateRequest(BaseModel):
    """A screenshot plus the expected post-step state, in words."""

    screenshot: str = Field(description="Base64-encoded PNG of the screen")
    expected_state: str = Field(description="Semantic description of the state")


class VerifyStateResponse(BaseModel):
    """Drift-oracle postcondition verdict.

    ``uncertain`` is the safe direction: the client treats it (and any error)
    as "postcondition not satisfied" -> halt.
    """

    holds: Literal["yes", "no", "uncertain"]
    latency_ms: float = 0.0


# --- health / readiness ----------------------------------------------------


class HealthResponse(BaseModel):
    """Liveness: the process is up (does not imply the model is loaded)."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    """Readiness: whether the inference backend has finished loading."""

    ready: bool
    backend: str
    model: Optional[str] = None
