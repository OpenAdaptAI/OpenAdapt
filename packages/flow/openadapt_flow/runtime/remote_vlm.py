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
    ) -> Optional[GrounderMatch]:
        sent, scale = _downscale_for_model(screen_png)
        data = self._client.ground(sent, intent, ocr_text)
        if data is None:
            return None  # SAFE: service down => no proposal
        point = data.get("point")
        if not point or not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        x, y = point
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        # The model saw a downscaled frame; map the proposal back to the
        # original screenshot's pixel space before anything acts on it.
        px, py = int(round(x / scale)), int(round(y / scale))
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

    def verify(self, screenshot: bytes, expected_state: str) -> str:
        # Downscale below the model's image ceiling; there are no coordinates
        # to map back for a yes/no/uncertain verdict.
        sent, _scale = _downscale_for_model(screenshot)
        data = self._client.verify_state(sent, expected_state)
        if data is None:
            return "uncertain"  # SAFE: service down => unproven => halt
        holds = data.get("holds")
        if holds in ("yes", "no"):
            return holds
        return "uncertain"

    def holds(self, screenshot: bytes, expected_state: str) -> bool:
        """Convenience: True ONLY on a confident 'yes' (uncertain/no => False)."""
        return self.verify(screenshot, expected_state) == "yes"


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
