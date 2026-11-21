"""Fail-safe clients for the on-prem VLM service (GPU-less runner side).

The runner runs on machines with NO GPU (Windows VMs, Citrix, desktops). It
calls a shared GPU appliance over the LAN. This module is the client half.

**Fail-safe is the whole point.** When the appliance is unreachable, slow,
returns an auth error, a 5xx, or a malformed body, every client here returns the
SAFE outcome so the runtime HALTS rather than proceeds:

* identity  -> ``IdentityVerdict.ABSTAIN``  (do-not-verify => tier abstains => halt)
* grounder  -> ``None``                      (no proposal => resolution ladder halts)
* state     -> ``"uncertain"``               (postcondition unproven => halt)

A GPU-less runner degrades to safe-halt, never to a wrong action, when the
appliance is down. Nothing here can turn an outage into a click.
"""

from __future__ import annotations

import base64
import math
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

from openadapt_flow.ir import Point, Region
from openadapt_flow.runtime.grounder import GrounderMatch

# Grounder region half-extents (mirrors runtime.grounder.AnthropicGrounder).
_REGION_HALF_W = 20
_REGION_HALF_H = 10


class IdentityVerdict(str, Enum):
    """Result of the same/different identity veto, in the ladder's vocabulary.

    * ``VERIFY``   -- the comparator returned a clean, confident SAME. Per the
      veto-only rule this is a *fail-to-veto*, NOT an authorization: it lets the
      deterministic identity authority stand; it never grants a pass a
      string-compare would not.
    * ``MISMATCH`` -- an explicit DIFFERENT. Affirmative wrong-entity evidence: halt.
    * ``ABSTAIN``  -- uncertain, unreachable, timeout, auth error, or malformed.
      The tier cannot vouch => it abstains => the ladder halts.

    The identity ladder's VLM tier maps ``VERIFY -> pass-through``,
    ``MISMATCH -> halt``, ``ABSTAIN -> halt``.
    """

    VERIFY = "verify"
    MISMATCH = "mismatch"
    ABSTAIN = "abstain"


def _b64(png: bytes) -> str:
    return base64.standard_b64encode(png).decode("utf-8")


# End-to-end validation (benchmark/appliance_validation) measured a hard image
# ceiling: the served 4-bit VLM (Qwen3-VL-4B-4bit) emits empty/degenerate output
# on large screenshots (~1800px+), so at native 2x Retina the grounder and
# state-verifier silently went inert (every call -> null/uncertain -> safe-halt,
# but never useful). Full-frame screenshots are downscaled below this before
# they cross the wire; identity crops are already small and are left untouched.
_MAX_MODEL_IMAGE_DIM = 1024
_GOLD_DIVIDER_PX = 4
_GOLD_DIVIDER_RGB = (48, 48, 48)
_GOLD_DEFAULT_PAD_RATIO = 0.15
_GOLD_MIN_PAD_PX = 16


def _downscale_for_model(
    png: bytes, max_dim: int = _MAX_MODEL_IMAGE_DIM
) -> tuple[bytes, float]:
    """Downscale a PNG so its longest side is ``<= max_dim``.

    Returns ``(png_bytes, scale)`` where ``scale <= 1.0`` (``1.0`` when the
    image was already small enough). A coordinate in the returned image maps
    back to the original by DIVIDING by ``scale``. On any failure returns the
    original bytes and ``1.0`` — the model may then abstain, which is still a
    safe-halt, so this never trades safety for the optimisation.
    """
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(png))
        w, h = img.size
        longest = max(w, h)
        if longest <= max_dim:
            return png, 1.0
        scale = max_dim / longest
        resized = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        buf = BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue(), scale
    except Exception:
        return png, 1.0


def _png_dimensions(png: bytes) -> Optional[tuple[int, int]]:
    """Return ``(width, height)`` for decodable image bytes, or ``None``."""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(png)) as img:
            w, h = img.size
            return (int(w), int(h)) if w > 0 and h > 0 else None
    except Exception:
        return None


def extract_roi_crop(
    png: bytes,
    roi: Region,
    max_dim: int = _MAX_MODEL_IMAGE_DIM,
    *,
    pad_ratio: float = _GOLD_DEFAULT_PAD_RATIO,
    min_pad_px: int = _GOLD_MIN_PAD_PX,
) -> Optional[bytes]:
    """Extract a padded ROI crop bounded by ``max_dim``, or ``None`` if invalid.

    Unlike :func:`gold_crop_for_model`, this always crops to ``roi`` regardless
    of whether the source frame exceeds ``max_dim`` so callers comparing two
    frames (e.g. identity checks) never pair a cropped ROI with an uncropped
    full screenshot.
    """
    if max_dim <= 0:
        return None
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(png)).convert("RGB")
        w, h = img.size
        rx, ry, rw, rh = roi
        if rw <= 0 or rh <= 0:
            return None
        x0 = max(0, min(w, int(rx)))
        y0 = max(0, min(h, int(ry)))
        x1 = max(0, min(w, int(rx + rw)))
        y1 = max(0, min(h, int(ry + rh)))
        if x1 <= x0 or y1 <= y0:
            return None

        pad_x = max(min_pad_px, round((x1 - x0) * pad_ratio))
        pad_y = max(min_pad_px, round((y1 - y0) * pad_ratio))
        cx0 = max(0, x0 - pad_x)
        cy0 = max(0, y0 - pad_y)
        cx1 = min(w, x1 + pad_x)
        cy1 = min(h, y1 + pad_y)

        crop = img.crop((cx0, cy0, cx1, cy1))
        cw, ch = crop.size
        if max(cw, ch) > max_dim:
            local_scale = max_dim / max(cw, ch)
            crop = crop.resize(
                (max(1, round(cw * local_scale)), max(1, round(ch * local_scale))),
                Image.Resampling.LANCZOS,
            )
        buf = BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


@dataclass(frozen=True)
class GoldCropResult:
    """Deterministic Global-to-Local (GOLD) view of a screenshot within ``max_dim``.

    * ``global_png``    -- downscaled full frame (``max(w, h) <= max_dim``)
    * ``global_scale``  -- scale factor of ``global_png`` (``<= 1.0``)
    * ``local_png``     -- high-resolution ROI crop extracted from the original
      unscaled frame (``max(w, h) <= max_dim``)
    * ``local_scale``   -- scale factor of ``local_png`` relative to original
      pixels (``1.0`` whenever the padded ROI fits inside ``max_dim``)
    * ``local_origin``  -- ``(crop_x, crop_y)`` top-left of the padded ROI in
      original full-frame pixel coordinates
    * ``composite_png`` -- single-image GOLD composite (global overview stacked
      above the high-resolution local ROI crop, bounded by ``max_dim`` on both
      axes) for single-image VLM endpoints
    * ``used_gold``     -- ``True`` iff the original frame exceeded ``max_dim``
      and a valid ROI preserved higher pixel resolution than uniform downscaling
    """

    global_png: bytes
    global_scale: float
    local_png: bytes
    local_scale: float
    local_origin: tuple[int, int]
    composite_png: bytes
    used_gold: bool


def gold_crop_for_model(
    png: bytes,
    roi: Optional[Region] = None,
    max_dim: int = _MAX_MODEL_IMAGE_DIM,
    *,
    pad_ratio: float = _GOLD_DEFAULT_PAD_RATIO,
    min_pad_px: int = _GOLD_MIN_PAD_PX,
) -> GoldCropResult:
    """Build a deterministic Global-to-Local (GOLD) view within ``max_dim``.

    When ``roi`` is ``None``, empty/out-of-bounds, or the screenshot already
    fits inside ``max_dim``, this falls back cleanly to
    :func:`_downscale_for_model` with ``used_gold=False`` (zero behavior
    change for existing callers).

    When ``max(w, h) > max_dim`` and a valid ``roi = (x, y, w, h)`` is given,
    the target/alert region is cropped directly from the original unscaled
    screenshot with deterministic padding so small text and banners retain
    native 1:1 resolution (or the highest scale that fits ``max_dim``), paired
    with a global overview frame inside a single ``<= max_dim`` composite.
    """
    global_png, global_scale = _downscale_for_model(png, max_dim=max_dim)
    fallback = GoldCropResult(
        global_png=global_png,
        global_scale=global_scale,
        local_png=global_png,
        local_scale=global_scale,
        local_origin=(0, 0),
        composite_png=global_png,
        used_gold=False,
    )
    if roi is None or max_dim <= _GOLD_DIVIDER_PX + 2:
        return fallback

    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(png)).convert("RGB")
        w, h = img.size
        if max(w, h) <= max_dim:
            return fallback

        rx, ry, rw, rh = roi
        if rw <= 0 or rh <= 0:
            return fallback
        x0 = max(0, min(w, int(rx)))
        y0 = max(0, min(h, int(ry)))
        x1 = max(0, min(w, int(rx + rw)))
        y1 = max(0, min(h, int(ry + rh)))
        if x1 <= x0 or y1 <= y0:
            return fallback

        pad_x = max(min_pad_px, round((x1 - x0) * pad_ratio))
        pad_y = max(min_pad_px, round((y1 - y0) * pad_ratio))
        cx0 = max(0, x0 - pad_x)
        cy0 = max(0, y0 - pad_y)
        cx1 = min(w, x1 + pad_x)
        cy1 = min(h, y1 + pad_y)

        crop = img.crop((cx0, cy0, cx1, cy1))
        cw, ch = crop.size
        if max(cw, ch) > max_dim:
            local_scale = max_dim / max(cw, ch)
            local_img = crop.resize(
                (max(1, round(cw * local_scale)), max(1, round(ch * local_scale))),
                Image.Resampling.LANCZOS,
            )
        else:
            local_scale = 1.0
            local_img = crop

        local_buf = BytesIO()
        local_img.save(local_buf, format="PNG")
        local_png = local_buf.getvalue()

        # Build the single-image GOLD composite inside (max_dim x max_dim).
        # Preserve native local crop resolution up to 2/3 of the vertical budget
        # and fit the global overview in the remaining height.
        avail_h = max_dim - _GOLD_DIVIDER_PX
        local_max_h = min(local_img.height, max(1, (avail_h * 2) // 3))
        if local_img.height > local_max_h or local_img.width > max_dim:
            comp_local_scale = min(
                max_dim / local_img.width, local_max_h / local_img.height
            )
            local_panel = local_img.resize(
                (
                    max(1, round(local_img.width * comp_local_scale)),
                    max(1, round(local_img.height * comp_local_scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        else:
            local_panel = local_img

        global_max_h = max(1, avail_h - local_panel.height)
        comp_global_scale = min(max_dim / w, global_max_h / h)
        global_panel = img.resize(
            (
                max(1, round(w * comp_global_scale)),
                max(1, round(h * comp_global_scale)),
            ),
            Image.Resampling.LANCZOS,
        )

        comp_w = max(global_panel.width, local_panel.width)
        comp_h = global_panel.height + _GOLD_DIVIDER_PX + local_panel.height
        canvas = Image.new("RGB", (comp_w, comp_h), _GOLD_DIVIDER_RGB)
        canvas.paste(global_panel, (0, 0))
        canvas.paste(local_panel, (0, global_panel.height + _GOLD_DIVIDER_PX))
        comp_buf = BytesIO()
        canvas.save(comp_buf, format="PNG")

        return GoldCropResult(
            global_png=global_png,
            global_scale=global_scale,
            local_png=local_png,
            local_scale=local_scale,
            local_origin=(cx0, cy0),
            composite_png=comp_buf.getvalue(),
            used_gold=local_scale > global_scale,
        )
    except Exception:
        return fallback


class RemoteVLMClient:
    """Thin HTTP client for the VLM service. Never raises on transport failure.

    Each method returns a plain dict on success and ``None`` on ANY failure
    (unreachable, timeout, non-2xx, malformed JSON); the typed wrappers below
    turn ``None`` into the safe outcome for their domain.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _post(self, path: str, body: dict) -> Optional[dict]:
        try:
            resp = httpx.post(
                f"{self._base_url}{path}",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return None  # unreachable, timeout, connection reset, ...
        if resp.status_code != 200:
            return None  # auth error, 5xx, validation error
        try:
            data = resp.json()
        except ValueError:
            return None  # malformed body
        return data if isinstance(data, dict) else None

    def compare_identity(self, crop_a: bytes, crop_b: bytes) -> Optional[dict]:
        return self._post(
            "/v1/identity/compare", {"crop_a": _b64(crop_a), "crop_b": _b64(crop_b)}
        )

    def ground(
        self,
        screenshot: bytes,
        target_description: str,
        ocr_text: Optional[str] = None,
        viewport: Optional[tuple[int, int]] = None,
    ) -> Optional[dict]:
        body: dict = {
            "screenshot": _b64(screenshot),
            "target_description": target_description,
        }
        if ocr_text is not None:
            body["ocr_text"] = ocr_text
        if viewport is not None:
            body["viewport"] = list(viewport)
        return self._post("/v1/ground", body)

    def verify_state(self, screenshot: bytes, expected_state: str) -> Optional[dict]:
        return self._post(
            "/v1/verify_state",
            {"screenshot": _b64(screenshot), "expected_state": expected_state},
        )


class RemoteIdentityVLM:
    """Same/different veto client for the identity ladder's VLM tier.

    Maps the service's veto-only verdict onto :class:`IdentityVerdict`:

    * confident ``same``   -> ``VERIFY``   (fail-to-veto; authority still governs)
    * ``different``        -> ``MISMATCH`` (halt)
    * ``uncertain`` / unreachable / timeout / auth / malformed -> ``ABSTAIN`` (halt)

    Only a confident SAME avoids the veto, and even then it does not by itself
    grant a pass -- it merely declines to halt.
    """

    # EGRESS (PHI audit REM-3): sends screenshots/crops to the on-prem VLM
    # appliance over HTTP. Off the local box, so the replayer requires an
    # explicit opt-in (allow_model_grounding) to wire it and flags the run.
    MAY_EGRESS = True

    def __init__(self, client: RemoteVLMClient) -> None:
        self._client = client

    def compare(self, crop_a: bytes, crop_b: bytes) -> IdentityVerdict:
        data = self._client.compare_identity(crop_a, crop_b)
        if data is None:
            return IdentityVerdict.ABSTAIN  # SAFE: service down => halt
        verdict = data.get("verdict")
        if verdict == "same":
            return IdentityVerdict.VERIFY
        if verdict == "different":
            return IdentityVerdict.MISMATCH
        return IdentityVerdict.ABSTAIN  # "uncertain" / unknown => SAFE

    def is_veto(self, crop_a: bytes, crop_b: bytes) -> bool:
        """Convenience: True iff the tier vetoes (anything but a confident SAME)."""
        return self.compare(crop_a, crop_b) is not IdentityVerdict.VERIFY

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        """Adapt to the identity ladder's veto-only ``IdentityVLM`` interface
        (``runtime.identity.verify_vlm_identity``), so an instance drops
        straight into ``Replayer(identity_vlm=...)``.

        The tier reads ``"same"`` as fail-to-veto (abstain) and *anything else*
        as a veto (HALT). So only a confident ``VERIFY`` returns ``"same"``;
        ``MISMATCH`` and ``ABSTAIN`` — the latter being the default on any
        uncertainty, timeout, or appliance outage — both return ``"different"``,
        exactly this client's documented fail-safe contract: the tier can only
        veto, never grant a pass, and an outage lowers availability (more halts),
        never safety.
        """
        return (
            "same"
            if self.compare(recorded_png, live_png) is IdentityVerdict.VERIFY
            else "different"
        )


class RemoteGrounder:
    """:class:`~openadapt_flow.runtime.grounder.Grounder` backed by the service.

    Drop-in for ``NullGrounder`` / the ladder's grounder slot. Only ever
    PROPOSES a point; the deterministic identity band still disposes before any
    click. On any failure it returns ``None`` (no proposal) -- exactly the
    ``NullGrounder`` behaviour, so an appliance outage lowers availability, never
    safety.
    """

    # EGRESS (PHI audit REM-3): sends screenshots/crops to the on-prem VLM
    # appliance over HTTP. Off the local box, so the replayer requires an
    # explicit opt-in (allow_model_grounding) to wire it and flags the run.
    MAY_EGRESS = True

    def __init__(self, client: RemoteVLMClient) -> None:
        self._client = client

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
        roi: Optional[Region] = None,
    ) -> Optional[GrounderMatch]:
        if roi is not None:
            gold = gold_crop_for_model(screen_png, roi=roi)
            if gold.used_gold:
                match = self._ground_and_map(
                    gold.local_png,
                    intent,
                    ocr_text,
                    scale=gold.local_scale,
                    origin=gold.local_origin,
                )
                if match is not None:
                    return match
                # Local ROI had no proposal: fall back deterministically to the
                # global downscaled frame so a drifted target can still be found.
                return self._ground_and_map(
                    gold.global_png,
                    intent,
                    ocr_text,
                    scale=gold.global_scale,
                    origin=(0, 0),
                )
        sent, scale = _downscale_for_model(screen_png)
        return self._ground_and_map(sent, intent, ocr_text, scale=scale, origin=(0, 0))

    def _ground_and_map(
        self,
        sent: bytes,
        intent: str,
        ocr_text: Optional[str],
        *,
        scale: float,
        origin: tuple[int, int],
    ) -> Optional[GrounderMatch]:
        data = self._client.ground(sent, intent, ocr_text)
        if data is None:
            return None  # SAFE: service down => no proposal
        point = data.get("point")
        if not point or not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        x, y = point
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not math.isfinite(x)
            or not math.isfinite(y)
        ):
            return None
        # Validate coordinates against the actual image submitted to the model
        # before translating back to full-frame pixel space; an out-of-bounds
        # local coordinate must fall back to global rather than landing outside
        # the observed crop.
        dims = _png_dimensions(sent)
        if dims is not None:
            sent_w, sent_h = dims
            if x < 0 or y < 0 or x >= sent_w or y >= sent_h:
                return None
        elif x < 0 or y < 0:
            return None
        # The model saw a downscaled or ROI-cropped frame; map the proposal back
        # to the original screenshot's pixel space before anything acts on it.
        ox, oy = origin
        px = ox + int(round(x / scale))
        py = oy + int(round(y / scale))
        region: Region = (
            max(0, px - _REGION_HALF_W),
            max(0, py - _REGION_HALF_H),
            2 * _REGION_HALF_W,
            2 * _REGION_HALF_H,
        )
        pt: Point = (px, py)
        conf = data.get("confidence")
        confidence = float(conf) if isinstance(conf, (int, float)) else 0.5
        return GrounderMatch(point=pt, region=region, confidence=confidence)


class RemoteStateVerifier:
    """Drift-oracle postcondition client (semantic 'did the state happen?').

    Returns ``"yes"`` / ``"no"`` / ``"uncertain"``. ``"uncertain"`` is the safe
    direction and is returned on ANY failure, so a down appliance means the
    postcondition is treated as unproven (halt), never as satisfied.
    """

    # EGRESS (PHI audit REM-3): sends screenshots/crops to the on-prem VLM
    # appliance over HTTP. Off the local box, so the replayer requires an
    # explicit opt-in (allow_model_grounding) to wire it and flags the run.
    MAY_EGRESS = True

    def __init__(self, client: RemoteVLMClient) -> None:
        self._client = client

    def verify(
        self,
        screenshot: bytes,
        expected_state: str,
        roi: Optional[Region] = None,
    ) -> str:
        # Downscale below the model's image ceiling; when an ROI is supplied on
        # a >1024px frame, send a GOLD composite (global overview + native-res
        # local crop) within the same 1024px budget.
        if roi is not None:
            gold = gold_crop_for_model(screenshot, roi=roi)
            sent = gold.composite_png if gold.used_gold else gold.global_png
        else:
            sent, _scale = _downscale_for_model(screenshot)
        data = self._client.verify_state(sent, expected_state)
        if data is None:
            return "uncertain"  # SAFE: service down => unproven => halt
        holds = data.get("holds")
        if holds in ("yes", "no"):
            return holds
        return "uncertain"

    def holds(
        self,
        screenshot: bytes,
        expected_state: str,
        roi: Optional[Region] = None,
    ) -> bool:
        """Convenience: True ONLY on a confident 'yes' (uncertain/no => False)."""
        return self.verify(screenshot, expected_state, roi=roi) == "yes"


@dataclass
class RemoteAppliance:
    """Runner-side handles for a configured on-prem VLM appliance.

    ``identity_vlm`` and ``grounder`` drop straight into
    ``Replayer(identity_vlm=..., grounder=...)``; ``state_verifier`` backs the
    drift-oracle postcondition.
    """

    client: RemoteVLMClient
    identity_vlm: RemoteIdentityVLM
    grounder: RemoteGrounder
    state_verifier: RemoteStateVerifier


def appliance_from_env(env: Optional[dict] = None) -> Optional[RemoteAppliance]:
    """Build the runner-side remote-VLM handles from the environment, or return
    ``None`` when no appliance is configured -- the default, so the runtime
    stays fully local and model-free unless a GPU box is pointed to.

    Reads:

    * ``OPENADAPT_FLOW_VLM_URL``     -- appliance base URL; unset => ``None`` (dormant)
    * ``OPENADAPT_FLOW_VLM_TOKEN``   -- bearer token (matches the service's
      ``VLM_SERVICE_TOKEN``); optional
    * ``OPENADAPT_FLOW_VLM_TIMEOUT`` -- per-call timeout in seconds (default 2.0)

    Configuring an appliance never lowers safety: every returned client is
    fail-safe (outage => halt). It only *adds* the grounding rung and the VLM
    identity veto tier when the box is reachable.
    """
    src = os.environ if env is None else env
    url = (src.get("OPENADAPT_FLOW_VLM_URL") or "").strip()
    if not url:
        return None
    token = (src.get("OPENADAPT_FLOW_VLM_TOKEN") or "").strip()
    try:
        timeout = float(src.get("OPENADAPT_FLOW_VLM_TIMEOUT", "2.0"))
    except (TypeError, ValueError):
        timeout = 2.0
    client = RemoteVLMClient(url, token=token, timeout=timeout)
    return RemoteAppliance(
        client=client,
        identity_vlm=RemoteIdentityVLM(client),
        grounder=RemoteGrounder(client),
        state_verifier=RemoteStateVerifier(client),
    )
