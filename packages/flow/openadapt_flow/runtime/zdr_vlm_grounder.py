"""Fail-safe ZDR/BAA VLM grounder with optional Synthetic-Twin masking and anchor cache.

Calls an OpenAI-compatible vision chat-completions endpoint under Zero Data
Retention (ZDR) / BAA controls. On any failure (timeout, HTTP error, missing
ZDR confirmation header, malformed JSON, low confidence, or missing/out-of-bounds
coordinates), :meth:`ZdrVlmGrounder.locate` returns ``None`` so the resolution
ladder halts safely instead of clicking an unverified point.

When a :class:`HealedAnchorCache` is attached, a grounded target is cached
locally so subsequent runs reuse the healed anchor with zero HTTP calls.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import httpx

from openadapt_flow.ir import Point, Region
from openadapt_flow.runtime.grounder import GrounderMatch
from openadapt_flow.runtime.synthetic_twin_mask import (
    MaskReport,
    OcrTextBox,
    SyntheticTwinProjector,
    mask_screenshot_png,
    unmask_text,
)

_REGION_HALF_W = 20
_REGION_HALF_H = 10
_DEFAULT_MAX_IMAGE_DIM = 1600
_ZDR_ACCEPTED_VALUES = frozenset({"true", "1", "enforced", "zero", "none", "yes"})

_ZDR_PROMPT = (
    "Locate the UI control described below on the screenshot.\n"
    "Target intent: {intent}\n"
    "Target text label (may be stale): {ocr_text}\n"
    "Image dimensions: {width}x{height} pixels.\n\n"
    "Reply with ONLY a JSON object containing pixel coordinates and confidence "
    'between 0.0 and 1.0, for example {{"x": 123, "y": 45, "confidence": 0.92}}. '
    "If the target is not clearly visible or is ambiguous, reply with ONLY "
    '{{"x": null, "y": null, "confidence": 0.0}}.'
)


def _downscale_png(
    png_bytes: bytes, max_dim: int
) -> tuple[bytes, float, tuple[int, int], tuple[int, int]]:
    """Downscale a PNG so its longest side is at most ``max_dim``.

    Returns:
        ``(sent_png, scale, (orig_w, orig_h), (sent_w, sent_h))``.
    """
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as raw_img:
        img = raw_img.convert("RGB")
        orig_w, orig_h = img.size
        longest = max(orig_w, orig_h)
        if longest <= max_dim:
            return png_bytes, 1.0, (orig_w, orig_h), (orig_w, orig_h)
        scale = max_dim / float(longest)
        new_w = max(1, int(round(orig_w * scale)))
        new_h = max(1, int(round(orig_h * scale)))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue(), scale, (orig_w, orig_h), (new_w, new_h)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object embedded in ``text``, if any."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _openai_message_text(data: dict[str, Any]) -> str:
    """Extract assistant text content from an OpenAI chat-completions payload."""
    try:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return ""


def _default_ocr_provider(screen_png: bytes) -> Sequence[OcrTextBox]:
    """Lazy OCR helper using ``openadapt_flow.vision.ocr`` when installed."""
    from openadapt_flow.vision.ocr import ocr

    lines = ocr(screen_png)
    return [OcrTextBox.from_ocr_line(line) for line in lines]


class HealedAnchorCache:
    """In-memory and optional JSON-file cache for VLM-grounded local anchors.

    Once a shifted target is grounded by the VLM, subsequent runs with the same
    ``(intent, ocr_text, width, height)`` reuse the cached :class:`GrounderMatch`
    locally at zero API cost.
    """

    def __init__(self, cache_path: Optional[Path | str] = None) -> None:
        self._path: Optional[Path] = (
            Path(cache_path) if cache_path is not None else None
        )
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self.hits: int = 0
        self.misses: int = 0
        if self._path is not None and self._path.is_file():
            self._load_file()

    @staticmethod
    def make_key(
        intent: str,
        ocr_text: Optional[str],
        dims: tuple[int, int],
    ) -> str:
        raw = json.dumps(
            {
                "intent": intent.strip(),
                "ocr_text": (ocr_text or "").strip(),
                "w": int(dims[0]),
                "h": int(dims[1]),
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(
        self,
        intent: str,
        ocr_text: Optional[str],
        dims: tuple[int, int],
    ) -> Optional[GrounderMatch]:
        key = self.make_key(intent, ocr_text, dims)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return GrounderMatch(
                point=(int(entry["point"][0]), int(entry["point"][1])),
                region=(
                    int(entry["region"][0]),
                    int(entry["region"][1]),
                    int(entry["region"][2]),
                    int(entry["region"][3]),
                ),
                confidence=float(entry["confidence"]),
            )

    def put(
        self,
        intent: str,
        ocr_text: Optional[str],
        dims: tuple[int, int],
        match: GrounderMatch,
    ) -> None:
        key = self.make_key(intent, ocr_text, dims)
        record = {
            "point": list(match.point),
            "region": list(match.region),
            "confidence": float(match.confidence),
        }
        with self._lock:
            self._entries[key] = record
            if self._path is not None:
                self._save_file()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else 0.0

    def _load_file(self) -> None:
        assert self._path is not None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._entries = {
                    str(k): v for k, v in data.items() if isinstance(v, dict)
                }
        except (OSError, ValueError):
            self._entries = {}

    def _save_file(self) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._entries, indent=2, sort_keys=True),
            encoding="utf-8",
        )


@dataclass
class ZdrGrounderStats:
    """Telemetry counters for :class:`ZdrVlmGrounder`."""

    http_calls: int = 0
    cache_hits: int = 0
    resolved: int = 0
    abstentions: int = 0


class ZdrVlmGrounder:
    """OpenAI-compatible ZDR/BAA VLM grounder conforming to ``Grounder``.

    Properties:
    - Enforces Zero Data Retention headers when ``require_zdr_header=True``.
    - Automatically downscales screenshots to ``<= max_image_dim`` (default 1600px)
      and maps returned coordinates back to original pixel space.
    - Supports optional :class:`SyntheticTwinProjector` visual + prompt masking.
    - Supports optional :class:`HealedAnchorCache` for local zero-cost reuse.
    - Returns ``None`` (abstain) on any transport error, timeout, missing ZDR
      confirmation header, malformed JSON, low confidence, or invalid point.
    """

    MAY_EGRESS = True

    def __init__(
        self,
        endpoint_url: str,
        model: str,
        *,
        api_key: str = "",
        projector: Optional[SyntheticTwinProjector] = None,
        ocr_provider: Optional[Callable[[bytes], Sequence[OcrTextBox]]] = None,
        require_zdr_header: bool = True,
        timeout: float = 10.0,
        max_image_dim: int = _DEFAULT_MAX_IMAGE_DIM,
        min_confidence: float = 0.5,
        healed_anchor_cache: Optional[HealedAnchorCache] = None,
        client: Any = None,
    ) -> None:
        if not endpoint_url or not str(endpoint_url).strip():
            raise ValueError("ZdrVlmGrounder requires a non-empty endpoint_url")
        if not model or not str(model).strip():
            raise ValueError("ZdrVlmGrounder requires a non-empty model")
        if timeout <= 0:
            raise ValueError("ZdrVlmGrounder timeout must be positive")
        if max_image_dim <= 0:
            raise ValueError("ZdrVlmGrounder max_image_dim must be positive")

        cleaned = endpoint_url.strip().rstrip("/")
        self._url = (
            cleaned
            if cleaned.endswith("/chat/completions")
            else f"{cleaned}/chat/completions"
        )
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._projector = projector
        self._ocr_provider = ocr_provider
        self._require_zdr_header = bool(require_zdr_header)
        self._timeout = float(timeout)
        self._max_image_dim = int(max_image_dim)
        self._min_confidence = float(min_confidence)
        self._cache = healed_anchor_cache
        self._client = client
        self.stats = ZdrGrounderStats()
        self.last_mask_report: Optional[MaskReport] = None
        self.last_unmasked_reply: Optional[str] = None

    def _request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Zero-Data-Retention": "true",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _has_zdr_confirmation(resp: Any) -> bool:
        """Check whether the server confirmed Zero Data Retention in headers."""
        headers = getattr(resp, "headers", None)
        if headers is None:
            return False
        for name in ("x-zero-data-retention", "x-zdr-enforced", "x-data-retention"):
            val = headers.get(name)
            if val is not None and str(val).strip().lower() in _ZDR_ACCEPTED_VALUES:
                return True
        return False

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Locate ``intent`` on ``screen_png``, or return ``None`` to abstain.

        Never raises on network, encoding, masking, or response errors.
        """
        try:
            prepared_png = screen_png
            send_intent = intent
            send_ocr_text = ocr_text

            # Downscale first to inspect dimensions cleanly for cache lookup.
            # When a projector is active, mask at native resolution BEFORE
            # downscaling so OCR boxes align with the original pixels.
            if self._projector is not None:
                ocr_fn = self._ocr_provider or _default_ocr_provider
                boxes = ocr_fn(screen_png)
                prepared_png, report = mask_screenshot_png(
                    screen_png, boxes, self._projector
                )
                self.last_mask_report = report
                if report.raw_dossier_leaks > 0:
                    self.stats.abstentions += 1
                    return None
                send_intent, _ = self._projector.mask_text(intent)
                if ocr_text is not None:
                    send_ocr_text, _ = self._projector.mask_text(ocr_text)

            sent_png, scale, orig_dims, sent_dims = _downscale_png(
                prepared_png, self._max_image_dim
            )
        except Exception:
            self.stats.abstentions += 1
            return None

        if self._cache is not None:
            cached = self._cache.get(intent, ocr_text, orig_dims)
            if cached is not None:
                self.stats.cache_hits += 1
                self.stats.resolved += 1
                return cached

        try:
            image_b64 = base64.standard_b64encode(sent_png).decode("utf-8")
            prompt = _ZDR_PROMPT.format(
                intent=send_intent,
                ocr_text=send_ocr_text or "(none)",
                width=sent_dims[0],
                height=sent_dims[1],
            )
            body = {
                "model": self._model,
                "max_tokens": 256,
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}"
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
            self.stats.http_calls += 1
            data = self._post(body)
        except Exception:
            self.stats.abstentions += 1
            return None

        if data is None:
            self.stats.abstentions += 1
            return None

        raw_text = _openai_message_text(data)
        if not raw_text:
            self.stats.abstentions += 1
            return None

        if self._projector is not None:
            self.last_unmasked_reply = unmask_text(raw_text, self._projector)
        else:
            self.last_unmasked_reply = raw_text

        payload = _extract_json_object(raw_text)
        if payload is None:
            self.stats.abstentions += 1
            return None

        match = self._parse_match(
            payload, scale=scale, sent_dims=sent_dims, orig_dims=orig_dims
        )
        if match is None:
            self.stats.abstentions += 1
            return None

        if self._cache is not None:
            self._cache.put(intent, ocr_text, orig_dims, match)

        self.stats.resolved += 1
        return match

    def _post(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        headers = self._request_headers()
        try:
            if self._client is not None:
                resp = self._client.post(
                    self._url,
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                resp = httpx.post(
                    self._url,
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                )
        except httpx.HTTPError:
            return None

        if getattr(resp, "status_code", None) != 200:
            return None
        if self._require_zdr_header and not self._has_zdr_confirmation(resp):
            return None
        try:
            parsed = resp.json()
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _parse_match(
        self,
        payload: dict[str, Any],
        *,
        scale: float,
        sent_dims: tuple[int, int],
        orig_dims: tuple[int, int],
    ) -> Optional[GrounderMatch]:
        x = payload.get("x")
        y = payload.get("y")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not math.isfinite(x)
            or not math.isfinite(y)
        ):
            return None

        sent_w, sent_h = sent_dims
        if x < 0 or y < 0 or x >= sent_w or y >= sent_h:
            return None

        raw_conf = payload.get("confidence", 0.5)
        if (
            isinstance(raw_conf, bool)
            or not isinstance(raw_conf, (int, float))
            or not math.isfinite(raw_conf)
        ):
            return None
        confidence = float(raw_conf)
        if confidence < self._min_confidence or confidence > 1.0:
            return None

        orig_w, orig_h = orig_dims
        px = max(0, min(orig_w - 1, int(round(x / scale))))
        py = max(0, min(orig_h - 1, int(round(y / scale))))
        region: Region = (
            max(0, px - _REGION_HALF_W),
            max(0, py - _REGION_HALF_H),
            2 * _REGION_HALF_W,
            2 * _REGION_HALF_H,
        )
        point: Point = (px, py)
        return GrounderMatch(point=point, region=region, confidence=confidence)


def zdr_grounder_from_env(
    *,
    env: Optional[dict[str, str]] = None,
    projector: Optional[SyntheticTwinProjector] = None,
    ocr_provider: Optional[Callable[[bytes], Sequence[OcrTextBox]]] = None,
    healed_anchor_cache: Optional[HealedAnchorCache] = None,
) -> Optional[ZdrVlmGrounder]:
    """Build a :class:`ZdrVlmGrounder` from environment variables, or ``None``.

    Reads:
    - ``OPENADAPT_ZDR_VLM_URL`` (required; unset returns ``None``)
    - ``OPENADAPT_ZDR_VLM_MODEL`` (default ``gpt-4o``)
    - ``OPENADAPT_ZDR_VLM_API_KEY`` (optional bearer token)
    - ``OPENADAPT_ZDR_VLM_TIMEOUT`` (default ``10.0``)
    - ``OPENADAPT_ZDR_REQUIRE_HEADER`` (default ``1``; set ``0`` to disable)
    """
    src = os.environ if env is None else env
    url = (src.get("OPENADAPT_ZDR_VLM_URL") or "").strip()
    if not url:
        return None
    model = (src.get("OPENADAPT_ZDR_VLM_MODEL") or "gpt-4o").strip()
    api_key = (src.get("OPENADAPT_ZDR_VLM_API_KEY") or "").strip()
    try:
        timeout = float(src.get("OPENADAPT_ZDR_VLM_TIMEOUT", "10.0"))
    except (TypeError, ValueError):
        timeout = 10.0
    req_hdr_raw = (src.get("OPENADAPT_ZDR_REQUIRE_HEADER") or "1").strip().lower()
    require_zdr = req_hdr_raw not in ("0", "false", "no")
    return ZdrVlmGrounder(
        endpoint_url=url,
        model=model,
        api_key=api_key,
        projector=projector,
        ocr_provider=ocr_provider,
        require_zdr_header=require_zdr,
        timeout=timeout,
        healed_anchor_cache=healed_anchor_cache,
    )
