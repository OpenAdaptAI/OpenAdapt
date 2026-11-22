"""Wiring the remote-VLM clients into the production ladder.

Covers the one safety-critical seam (the ``IdentityVerdict`` -> veto-only
``same_or_different`` mapping the identity tier consumes) and the env factory
that decides, per run, whether the grounding rung and VLM veto tier come online
at all. No network: a stub client stands in for the HTTP layer, so these assert
the *mapping* and *fail-safe direction*, which is where a wiring bug would hide.
"""

from __future__ import annotations

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.remote_vlm import (
    RemoteAppliance,
    RemoteGrounder,
    RemoteIdentityVLM,
    RemoteStateVerifier,
    appliance_from_env,
)


class _StubClient:
    """Stands in for RemoteVLMClient: returns a canned body (or None = outage)
    for whichever endpoint a wrapper calls."""

    def __init__(self, *, identity=None, ground=None, state=None):
        self._identity = identity
        self._ground = ground
        self._state = state

    def compare_identity(self, a, b):
        return self._identity

    def ground(self, png, intent, ocr_text=None):
        return self._ground

    def verify_state(self, png, expected):
        return self._state


# --------------------------------------------------------------------------
# The safety-critical adapter: IdentityVerdict -> veto-only same_or_different.
# Only a confident SAME may fail-to-veto; everything else HALTs.
# --------------------------------------------------------------------------


def test_same_verdict_is_only_fail_to_veto():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "same"}))
    assert vlm.same_or_different(b"a", b"b") == "same"


def test_different_verdict_vetoes():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "different"}))
    assert vlm.same_or_different(b"a", b"b") == "different"


def test_uncertain_verdict_vetoes():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "uncertain"}))
    assert vlm.same_or_different(b"a", b"b") == "different"


def test_appliance_outage_vetoes():
    # client returns None (unreachable/timeout/5xx/malformed) -> ABSTAIN -> halt
    vlm = RemoteIdentityVLM(_StubClient(identity=None))
    assert vlm.same_or_different(b"a", b"b") == "different"


# --------------------------------------------------------------------------
# The wired tier, exercised through the REAL verify_vlm_identity it feeds.
# --------------------------------------------------------------------------


def test_wired_tier_halts_on_outage_for_confusable_identifier():
    vlm = RemoteIdentityVLM(_StubClient(identity=None))  # appliance down
    check = I.verify_vlm_identity(b"rec", b"live", verifier=vlm, glyph_confusable=True)
    assert check is not None
    assert check.status == "mismatch" and check.mode == "vlm"


def test_wired_tier_halts_on_different_for_confusable_identifier():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "different"}))
    check = I.verify_vlm_identity(b"rec", b"live", verifier=vlm, glyph_confusable=True)
    assert check is not None and check.status == "mismatch"


def test_wired_tier_same_abstains_never_grants_pass():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "same"}))
    # veto-only: a "same" answer folds to abstain (None), never a verified pass
    assert (
        I.verify_vlm_identity(b"rec", b"live", verifier=vlm, glyph_confusable=True)
        is None
    )


def test_wired_tier_gated_off_for_non_confusable_identifier():
    vlm = RemoteIdentityVLM(_StubClient(identity={"verdict": "different"}))
    # the tier only runs on glyph-confusable identifiers; else it abstains
    assert (
        I.verify_vlm_identity(b"rec", b"live", verifier=vlm, glyph_confusable=False)
        is None
    )


# --------------------------------------------------------------------------
# Grounder: only ever proposes; an outage lowers availability, not safety.
# --------------------------------------------------------------------------


def test_grounder_outage_returns_no_proposal():
    g = RemoteGrounder(_StubClient(ground=None))
    assert g.locate(b"png", "click Save", None) is None


def test_grounder_proposes_a_point_when_reachable():
    g = RemoteGrounder(_StubClient(ground={"point": [120, 44], "confidence": 0.9}))
    m = g.locate(b"png", "click Save", None)
    assert m is not None and m.point == (120, 44)


# --------------------------------------------------------------------------
# The env factory decides, per run, whether the appliance is used at all.
# Unset => fully local & model-free (the default).
# --------------------------------------------------------------------------


def test_factory_returns_none_when_unconfigured():
    assert appliance_from_env(env={}) is None
    assert appliance_from_env(env={"OPENADAPT_FLOW_VLM_URL": ""}) is None
    assert appliance_from_env(env={"OPENADAPT_FLOW_VLM_URL": "   "}) is None


def test_factory_builds_all_handles_when_configured():
    a = appliance_from_env(
        env={
            "OPENADAPT_FLOW_VLM_URL": "http://gpu-box.lan:8077",
            "OPENADAPT_FLOW_VLM_TOKEN": "secret",
            "OPENADAPT_FLOW_VLM_TIMEOUT": "1.5",
        }
    )
    assert isinstance(a, RemoteAppliance)
    assert isinstance(a.identity_vlm, RemoteIdentityVLM)
    assert isinstance(a.grounder, RemoteGrounder)
    assert isinstance(a.state_verifier, RemoteStateVerifier)
    # identity_vlm is a drop-in for Replayer(identity_vlm=...)
    assert hasattr(a.identity_vlm, "same_or_different")


def test_factory_tolerates_a_bad_timeout():
    a = appliance_from_env(
        env={
            "OPENADAPT_FLOW_VLM_URL": "http://x",
            "OPENADAPT_FLOW_VLM_TIMEOUT": "not-a-number",
        }
    )
    assert a is not None  # falls back to the default timeout, no crash
