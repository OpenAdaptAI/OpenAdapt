"""Unit tests for hardware-tiered local model routing and PrePostSubmitVisualVeto.

Covers:
1. Hardware capability detection and Tier 1 / Tier 2 / Tier 3 routing.
2. Explicit refusal of interactive 4B+ VLM calls on CPU-only hosts and offline
   opt-in via ``TierPolicy(allow_slow_cpu_vlm=True)``.
3. Strict ``MAY_EGRESS`` propagation into ``Replayer`` opt-in enforcement.
4. GOLD-cropped pre-submit and post-submit strict veto-only truth table on
   ``>1024px`` synthetic frames.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.models import (
    CpuVlmRefusedError,
    HardwareCapabilities,
    HardwareTier,
    PrePostSubmitVisualVeto,
    TieredLocalModelRouter,
    TierPolicy,
    VisualVetoStage,
    detect_hardware_capabilities,
)
from openadapt_flow.runtime.grounder import component_may_egress
from openadapt_flow.runtime.identity_vlm import VetoOnlyVLMGuard, VetoVerdict
from openadapt_flow.runtime.remote_vlm import (
    _MAX_MODEL_IMAGE_DIM,
    RemoteAppliance,
    RemoteGrounder,
    RemoteIdentityVLM,
    RemoteStateVerifier,
    RemoteVLMClient,
)
from openadapt_flow.runtime.replayer import EgressNotPermitted, Replayer


def _dims(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def _make_frame(
    w: int = 2560,
    h: int = 1440,
    roi: tuple[int, int, int, int] = (400, 120, 320, 64),
) -> bytes:
    img = Image.new("RGB", (w, h), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    rx, ry, rw, rh = roi
    draw.rectangle(
        [rx, ry, rx + rw - 1, ry + rh - 1],
        fill=(255, 255, 255),
        outline=(200, 40, 40),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeLocalIdentity:
    MAY_EGRESS = False

    def __init__(self, answer: str = "same") -> None:
        self.answer = answer
        self.calls: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        self.calls.append((_dims(recorded_png), _dims(live_png)))
        return self.answer


class _FakeLocalState:
    MAY_EGRESS = False

    def __init__(self, verdict: str = "yes") -> None:
        self.verdict = verdict
        self.calls: list[tuple[tuple[int, int], str]] = []

    def verify(self, screenshot: bytes, expected_state: str) -> str:
        self.calls.append((_dims(screenshot), expected_state))
        return self.verdict


def _make_lan_appliance() -> RemoteAppliance:
    client = RemoteVLMClient("http://127.0.0.1:9")
    return RemoteAppliance(
        client=client,
        identity_vlm=RemoteIdentityVLM(client),
        grounder=RemoteGrounder(client),
        state_verifier=RemoteStateVerifier(client),
    )


# --------------------------------------------------------------------------
# 1. Capability detection & tier routing
# --------------------------------------------------------------------------


def test_detect_hardware_capabilities_reads_lan_env():
    caps_off = detect_hardware_capabilities(env={}, probe_imports=False)
    assert caps_off.has_lan_appliance is False
    assert caps_off.has_apple_silicon_mlx is False
    assert caps_off.has_cuda_gpu is False

    caps_on = detect_hardware_capabilities(
        env={"OPENADAPT_FLOW_VLM_URL": "http://127.0.0.1:8077"},
        probe_imports=False,
    )
    assert caps_on.has_lan_appliance is True


@pytest.mark.parametrize(
    "caps",
    [
        HardwareCapabilities("Darwin", "arm64", has_apple_silicon_mlx=True),
        HardwareCapabilities("Linux", "x86_64", has_cuda_gpu=True),
        HardwareCapabilities("Linux", "x86_64", has_lan_appliance=True),
        HardwareCapabilities("Linux", "x86_64", has_onnxruntime=True),
    ],
)
def test_tier1_deterministic_always_wins_when_available(caps):
    router = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(prefer_deterministic=True, allow_lan_appliance=True),
    )
    decision = router.route(deterministic_available=True, needs_vlm_veto=True)
    assert decision.tier is HardwareTier.DETERMINISTIC_ONLY
    assert decision.allow_vlm_veto is False
    assert decision.may_egress is False


def test_tier2_local_accelerated_routes_without_egress():
    caps = HardwareCapabilities("Darwin", "arm64", has_apple_silicon_mlx=True)
    router = TieredLocalModelRouter(
        capabilities=caps,
        local_identity_vlm=_FakeLocalIdentity("same"),
        local_state_verifier=_FakeLocalState("yes"),
    )
    decision = router.route(deterministic_available=False, needs_vlm_veto=True)
    assert decision.tier is HardwareTier.LOCAL_ACCELERATED
    assert decision.allow_vlm_veto is True
    assert decision.may_egress is False
    assert router.MAY_EGRESS is False


def test_tier2_lan_appliance_requires_explicit_egress_opt_in():
    caps = HardwareCapabilities(
        "Linux", "x86_64", has_lan_appliance=True, has_onnxruntime=True
    )
    appliance = _make_lan_appliance()

    # Without allow_lan_appliance=True: refuses LAN egress and falls back to CPU tier.
    router_blocked = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(allow_lan_appliance=False),
        lan_appliance=appliance,
    )
    dec_blocked = router_blocked.route(
        deterministic_available=False, needs_vlm_veto=True
    )
    assert dec_blocked.tier is HardwareTier.CPU_LIGHTWEIGHT
    assert dec_blocked.allow_vlm_veto is False
    assert dec_blocked.may_egress is False
    assert router_blocked.MAY_EGRESS is False

    # With allow_lan_appliance=True: routes to LAN_APPLIANCE and propagates MAY_EGRESS.
    router_opted = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(allow_lan_appliance=True),
        lan_appliance=appliance,
    )
    dec_opted = router_opted.route(deterministic_available=False, needs_vlm_veto=True)
    assert dec_opted.tier is HardwareTier.LAN_APPLIANCE
    assert dec_opted.allow_vlm_veto is True
    assert dec_opted.may_egress is True
    assert router_opted.MAY_EGRESS is True


# --------------------------------------------------------------------------
# 2. Tier 3 CPU-only: interactive 4B+ VLM refusal + offline opt-in
# --------------------------------------------------------------------------


def test_tier3_cpu_only_refuses_interactive_vlm_and_allows_onnx_locate():
    caps = HardwareCapabilities("Linux", "x86_64", has_onnxruntime=True)
    local_id = _FakeLocalIdentity("same")
    router = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(allow_slow_cpu_vlm=False, interactive=True),
        local_identity_vlm=local_id,
    )

    dec_locate = router.route(deterministic_available=False, needs_locate=True)
    assert dec_locate.tier is HardwareTier.CPU_LIGHTWEIGHT
    assert dec_locate.allow_lightweight_locate is True
    assert dec_locate.allow_vlm_veto is False

    # Explicit require_vlm=True raises CpuVlmRefusedError.
    with pytest.raises(CpuVlmRefusedError):
        router.build_veto_guard(require_vlm=True)

    # Non-raising build returns a fail-closed abstaining guard that never calls the VLM.
    veto = router.build_submit_veto(require_vlm=False)
    frame = _make_frame()
    res = veto.check_pre_submit(
        recorded_png=frame, live_png=frame, deterministic_passed=True
    )
    assert res.tier is HardwareTier.CPU_LIGHTWEIGHT
    assert res.decision.verdict is VetoVerdict.ABSTAIN
    assert res.allowed is False
    assert local_id.calls == []


def test_tier3_cpu_only_allows_vlm_when_offline_opted_in():
    caps = HardwareCapabilities("Linux", "x86_64", has_onnxruntime=True)
    local_id = _FakeLocalIdentity("same")
    router = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(allow_slow_cpu_vlm=True, interactive=False),
        local_identity_vlm=local_id,
    )

    dec = router.route(deterministic_available=False, needs_vlm_veto=True)
    assert dec.tier is HardwareTier.CPU_LIGHTWEIGHT
    assert dec.allow_vlm_veto is True
    assert dec.may_egress is False

    veto = router.build_submit_veto(require_vlm=True)
    frame = _make_frame()
    res = veto.check_pre_submit(
        recorded_png=frame, live_png=frame, deterministic_passed=True
    )
    assert res.decision.verdict is VetoVerdict.PASS
    assert res.allowed is True
    assert len(local_id.calls) == 1


# --------------------------------------------------------------------------
# 3. MAY_EGRESS propagation into Replayer
# --------------------------------------------------------------------------


def test_router_and_submit_veto_propagate_egress_to_replayer():
    caps = HardwareCapabilities("Linux", "x86_64", has_lan_appliance=True)
    router = TieredLocalModelRouter(
        capabilities=caps,
        policy=TierPolicy(allow_lan_appliance=True),
        lan_appliance=_make_lan_appliance(),
    )
    guard = router.build_veto_guard()
    veto = router.build_submit_veto()

    assert component_may_egress(router) is True
    assert component_may_egress(guard) is True
    assert component_may_egress(veto) is True

    with pytest.raises(EgressNotPermitted):
        Replayer(backend=object(), vision=object(), identity_vlm=guard)

    replayer = Replayer(
        backend=object(),
        vision=object(),
        identity_vlm=guard,
        allow_model_grounding=True,
    )
    assert replayer._screenshots_may_leave_box is True


# --------------------------------------------------------------------------
# 4. PrePostSubmitVisualVeto GOLD-crop & strict veto-only truth table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("det_passed", "raw_id", "expected_verdict", "expected_allowed"),
    [
        (True, "same", VetoVerdict.PASS, True),
        (True, "different", VetoVerdict.VETO, False),
        (True, "uncertain", VetoVerdict.ABSTAIN, False),
        # PASS must NEVER override a failed deterministic check.
        (False, "same", VetoVerdict.PASS, False),
        (False, "different", VetoVerdict.VETO, False),
        (False, "uncertain", VetoVerdict.ABSTAIN, False),
    ],
)
def test_pre_submit_visual_veto_truth_table_and_gold_crop(
    det_passed, raw_id, expected_verdict, expected_allowed
):
    roi = (400, 120, 320, 64)
    frame = _make_frame(2560, 1440, roi=roi)
    id_backend = _FakeLocalIdentity(raw_id)
    guard = VetoOnlyVLMGuard(identity_vlm=id_backend)
    veto = PrePostSubmitVisualVeto(guard=guard, tier=HardwareTier.LOCAL_ACCELERATED)

    res = veto.check_pre_submit(
        recorded_png=frame,
        live_png=frame,
        deterministic_passed=det_passed,
        roi=roi,
    )
    assert res.stage is VisualVetoStage.PRE_SUBMIT
    assert res.decision.verdict is expected_verdict
    assert res.decision.used_gold_crop is True
    assert res.allowed is expected_allowed
    rec_dims, live_dims = id_backend.calls[0]
    assert max(rec_dims) <= _MAX_MODEL_IMAGE_DIM
    assert max(live_dims) <= _MAX_MODEL_IMAGE_DIM
    assert rec_dims[0] < 1024  # native ROI crop, not full 2560px frame


@pytest.mark.parametrize(
    ("det_passed", "raw_state", "expected_verdict", "expected_allowed"),
    [
        (True, "yes", VetoVerdict.PASS, True),
        (True, "no", VetoVerdict.VETO, False),
        (True, "uncertain", VetoVerdict.ABSTAIN, False),
        (False, "yes", VetoVerdict.PASS, False),
        (False, "no", VetoVerdict.VETO, False),
        (False, "uncertain", VetoVerdict.ABSTAIN, False),
    ],
)
def test_post_submit_visual_veto_truth_table_and_gold_crop(
    det_passed, raw_state, expected_verdict, expected_allowed
):
    roi = (400, 120, 320, 64)
    frame = _make_frame(2560, 1440, roi=roi)
    state_backend = _FakeLocalState(raw_state)
    guard = VetoOnlyVLMGuard(state_verifier=state_backend)
    veto = PrePostSubmitVisualVeto(guard=guard, tier=HardwareTier.LOCAL_ACCELERATED)

    res = veto.check_post_submit(
        screenshot=frame,
        expected_state="confirmation banner visible",
        deterministic_passed=det_passed,
        roi=roi,
    )
    assert res.stage is VisualVetoStage.POST_SUBMIT
    assert res.decision.verdict is expected_verdict
    assert res.decision.used_gold_crop is True
    assert res.allowed is expected_allowed
    sent_dims, _ = state_backend.calls[0]
    assert max(sent_dims) <= _MAX_MODEL_IMAGE_DIM


def test_pre_submit_combined_identity_and_state_halts_if_state_vetoes():
    roi = (400, 120, 320, 64)
    frame = _make_frame(2560, 1440, roi=roi)
    guard = VetoOnlyVLMGuard(
        identity_vlm=_FakeLocalIdentity("same"),
        state_verifier=_FakeLocalState("no"),
    )
    veto = PrePostSubmitVisualVeto(guard=guard, tier=HardwareTier.LOCAL_ACCELERATED)
    res = veto.check_pre_submit(
        recorded_png=frame,
        live_png=frame,
        deterministic_passed=True,
        roi=roi,
        expected_state="submit button enabled and no validation error",
    )
    assert res.decision.verdict is VetoVerdict.VETO
    assert res.allowed is False


def test_pre_submit_partly_clipped_roi_abstains_without_calling_backend():
    clipped_roi = (900, 80, 320, 64)
    large_frame = _make_frame(2560, 1440, roi=clipped_roi)
    small_frame = _make_frame(1024, 768, roi=(200, 80, 320, 64))
    id_backend = _FakeLocalIdentity("same")
    veto = PrePostSubmitVisualVeto(
        guard=VetoOnlyVLMGuard(identity_vlm=id_backend),
        tier=HardwareTier.LOCAL_ACCELERATED,
    )
    res = veto.check_pre_submit(
        recorded_png=large_frame,
        live_png=small_frame,
        deterministic_passed=True,
        roi=clipped_roi,
    )
    assert res.decision.verdict is VetoVerdict.ABSTAIN
    assert res.allowed is False
    assert id_backend.calls == []
