"""Bring-your-own (operator-selected) model grounding.

Covers the four seams the feature adds:

* the typed ``GroundingModelConfig`` (parse / validation / defaults);
* the ``OpenAICompatibleGrounder`` adapter (mocked HTTP: success -> point;
  error / non-200 / malformed / ``{x: null}`` -> None abstain);
* the FAIL-CLOSED PHI allowlist / aggregator-denylist / attestation semantics;
* the egress-gate + PHI wiring in ``build_replayer`` (OFF by default; PHI mode
  refuses a non-attested endpoint and stays fully local).

The SAFETY INVARIANT is asserted throughout: a grounder only ever PROPOSES a
point (returns a ``GrounderMatch`` or None); it never disposes/clicks. An
outage, a confused model, or a blocked endpoint lowers availability (the ladder
halts), never safety.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_flow.deployment import (
    DeploymentConfig,
    GroundingModelConfig,
    RuntimeSection,
    build_model_grounder,
    build_replayer,
    phi_grounding_endpoint_allowed,
)
from openadapt_flow.runtime.grounder import (
    GrounderMatch,
    OpenAICompatibleGrounder,
    component_may_egress,
)

# --------------------------------------------------------------------------
# Mocked HTTP plumbing (no network).
# --------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("malformed body")
        return self._payload


class _FakeClient:
    """Stands in for an ``httpx.Client``; records the request it received."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return self._resp


def _ok(content) -> _FakeResp:
    return _FakeResp(payload={"choices": [{"message": {"content": content}}]})


# --------------------------------------------------------------------------
# 1) Config: parse / validation / defaults.
# --------------------------------------------------------------------------


def test_grounding_model_config_defaults_are_off_and_local():
    cfg = GroundingModelConfig()
    assert cfg.enabled is False
    assert cfg.provider == "anthropic"
    assert cfg.base_url == "" and cfg.model == "" and cfg.api_key_env == ""


def test_runtime_section_defaults_carry_empty_allowlist_and_no_attestation():
    rt = RuntimeSection()
    assert rt.grounding_model.enabled is False
    assert rt.phi_grounding_allowlist == []
    assert rt.phi_egress_attested is False


def test_deployment_parses_grounding_model_section():
    cfg = DeploymentConfig.model_validate(
        {
            "runtime": {
                "allow_model_grounding": True,
                "grounding_model": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "base_url": "http://vllm.internal:8000/v1",
                    "model": "qwen2.5-vl-7b-instruct",
                    "api_key_env": "MY_KEY",
                },
                "phi_grounding_allowlist": ["vllm.internal"],
                "phi_egress_attested": False,
            }
        }
    )
    gm = cfg.runtime.grounding_model
    assert gm.enabled and gm.provider == "openai_compatible"
    assert gm.base_url == "http://vllm.internal:8000/v1"
    assert gm.api_key_env == "MY_KEY"
    assert cfg.runtime.phi_grounding_allowlist == ["vllm.internal"]


def test_invalid_provider_is_rejected():
    with pytest.raises(ValidationError):
        GroundingModelConfig(provider="totally-made-up")


def test_openai_grounder_requires_base_url_and_model():
    with pytest.raises(ValueError):
        OpenAICompatibleGrounder(base_url="", model="m")
    with pytest.raises(ValueError):
        OpenAICompatibleGrounder(base_url="http://x/v1", model="")


# --------------------------------------------------------------------------
# 2) OpenAICompatibleGrounder adapter — mocked HTTP.
# --------------------------------------------------------------------------


def test_grounder_success_returns_a_proposed_point():
    client = _FakeClient(resp=_ok('{"x": 120, "y": 44}'))
    g = OpenAICompatibleGrounder(
        base_url="http://vllm.internal:8000/v1", model="m", client=client
    )
    match = g.locate(b"png", "click Save", "Save")
    assert isinstance(match, GrounderMatch)
    assert match.point == (120, 44)
    # It PROPOSES only; egress-capable and the request hit /chat/completions.
    assert component_may_egress(g) is True
    assert client.calls[0]["url"].endswith("/chat/completions")


def test_grounder_parses_content_as_list_of_parts():
    client = _FakeClient(resp=_ok([{"type": "text", "text": '{"x": 10, "y": 20}'}]))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    match = g.locate(b"png", "click", "OK")
    assert match is not None and match.point == (10, 20)


def test_grounder_null_coordinates_abstain():
    client = _FakeClient(resp=_ok('{"x": null, "y": null}'))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    assert g.locate(b"png", "click", "OK") is None


def test_grounder_transport_error_abstains():
    client = _FakeClient(exc=RuntimeError("connection reset"))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    assert g.locate(b"png", "click", "OK") is None


def test_grounder_non_200_abstains():
    client = _FakeClient(resp=_FakeResp(status_code=500, payload={}))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    assert g.locate(b"png", "click", "OK") is None


def test_grounder_malformed_body_abstains():
    client = _FakeClient(resp=_FakeResp(status_code=200, raise_json=True))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    assert g.locate(b"png", "click", "OK") is None


def test_grounder_unparseable_text_abstains():
    client = _FakeClient(resp=_ok("I cannot find the target on this screen."))
    g = OpenAICompatibleGrounder(base_url="http://x/v1", model="m", client=client)
    assert g.locate(b"png", "click", "OK") is None


def test_grounder_sends_api_key_as_bearer():
    client = _FakeClient(resp=_ok('{"x": 1, "y": 2}'))
    g = OpenAICompatibleGrounder(
        base_url="http://x/v1", model="m", api_key="sekret", client=client
    )
    g.locate(b"png", "click", "OK")
    assert client.calls[0]["headers"]["Authorization"] == "Bearer sekret"


# --------------------------------------------------------------------------
# 3) PHI allowlist / denylist / attestation semantics (pure function).
# --------------------------------------------------------------------------


def test_phi_unlisted_host_is_refused():
    ok, reason = phi_grounding_endpoint_allowed(
        "vllm.internal", allowlist=[], attested=False
    )
    assert ok is False and "not on the attested allowlist" in reason


def test_phi_allowlisted_non_aggregator_is_allowed():
    ok, _ = phi_grounding_endpoint_allowed(
        "vllm.internal", allowlist=["vllm.internal"], attested=False
    )
    assert ok is True


def test_phi_allowlist_accepts_full_url_entries():
    ok, _ = phi_grounding_endpoint_allowed(
        "vllm.internal",
        allowlist=["https://vllm.internal:8000/v1"],
        attested=False,
    )
    assert ok is True


def test_phi_aggregator_without_attestation_is_blocked_even_if_allowlisted():
    ok, reason = phi_grounding_endpoint_allowed(
        "openrouter.ai", allowlist=["openrouter.ai"], attested=False
    )
    assert ok is False and "aggregator" in reason


def test_phi_aggregator_with_attestation_is_allowed():
    ok, _ = phi_grounding_endpoint_allowed(
        "openrouter.ai", allowlist=["openrouter.ai"], attested=True
    )
    assert ok is True


def test_phi_empty_host_is_refused():
    ok, _ = phi_grounding_endpoint_allowed("", allowlist=["x"], attested=True)
    assert ok is False


# --------------------------------------------------------------------------
# 4) build_model_grounder — construction / graceful degradation.
# --------------------------------------------------------------------------


def test_build_model_grounder_disabled_returns_none():
    assert build_model_grounder(GroundingModelConfig(enabled=False)) is None


def test_build_model_grounder_builds_openai_compatible(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc")
    g = build_model_grounder(
        GroundingModelConfig(
            enabled=True,
            provider="openai_compatible",
            base_url="http://vllm.internal:8000/v1",
            model="m",
            api_key_env="MY_KEY",
        )
    )
    assert isinstance(g, OpenAICompatibleGrounder)


def test_build_model_grounder_openai_without_base_url_degrades_local(capsys):
    g = build_model_grounder(
        GroundingModelConfig(enabled=True, provider="openai_compatible", model="m")
    )
    assert g is None
    assert "FULLY LOCAL" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 5) build_replayer wiring: egress gate + PHI enforcement (no real Replayer).
# --------------------------------------------------------------------------


@pytest.fixture()
def wiring(monkeypatch):
    """Capture the grounder fallback and the Replayer kwargs without building a
    real Replayer or a real OCR rung."""
    captured: dict = {}

    import openadapt_flow.runtime as rt
    import openadapt_flow.runtime.grounder as gmod

    class _FakeReplayer:
        def __init__(self, backend, **kwargs):
            captured["replayer_kwargs"] = kwargs

    def _fake_build_grounder(fallback=None):
        captured["fallback"] = fallback
        return fallback

    monkeypatch.setattr(rt, "Replayer", _FakeReplayer)
    monkeypatch.setattr(gmod, "build_grounder", _fake_build_grounder)
    # Ensure no ambient on-prem appliance leaks in from the environment.
    monkeypatch.delenv("OPENADAPT_FLOW_VLM_URL", raising=False)
    return captured


def _runtime(**gm):
    return RuntimeSection(
        grounding_model=GroundingModelConfig(**gm.pop("grounding_model")),
        phi_grounding_allowlist=gm.pop("allowlist", []),
        phi_egress_attested=gm.pop("attested", False),
    )


def _build(captured, *, allow_egress, runtime_config, phi_mode):
    build_replayer(
        object(),
        allow_egress=allow_egress,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=True,
        runtime_config=runtime_config,
        phi_mode=phi_mode,
    )
    return captured["fallback"]


def _oai(**over):
    base = dict(
        grounding_model=dict(
            enabled=True,
            provider="openai_compatible",
            base_url="http://vllm.internal:8000/v1",
            model="m",
        )
    )
    base.update(over)
    return _runtime(**base)


def test_egress_disabled_by_default_wires_no_model_grounder(wiring):
    # A configured model + egress OFF => nothing egresses; no model fallback.
    fallback = _build(wiring, allow_egress=False, runtime_config=_oai(), phi_mode=False)
    assert fallback is None


def test_non_phi_configured_endpoint_is_wired(wiring):
    fallback = _build(wiring, allow_egress=True, runtime_config=_oai(), phi_mode=False)
    assert isinstance(fallback, OpenAICompatibleGrounder)


def test_phi_non_attested_host_stays_local(wiring, capsys):
    # PHI on, host not on allowlist => refused, fully local.
    fallback = _build(wiring, allow_egress=True, runtime_config=_oai(), phi_mode=True)
    assert fallback is None
    assert "FULLY LOCAL" in capsys.readouterr().out


def test_phi_allowlisted_non_aggregator_is_wired(wiring):
    fallback = _build(
        wiring,
        allow_egress=True,
        runtime_config=_oai(allowlist=["vllm.internal"]),
        phi_mode=True,
    )
    assert isinstance(fallback, OpenAICompatibleGrounder)


def test_phi_aggregator_without_attestation_is_blocked(wiring):
    rt_cfg = _runtime(
        grounding_model=dict(
            enabled=True,
            provider="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            model="m",
        ),
        allowlist=["openrouter.ai"],
        attested=False,
    )
    fallback = _build(wiring, allow_egress=True, runtime_config=rt_cfg, phi_mode=True)
    assert fallback is None


def test_phi_aggregator_with_attestation_is_wired(wiring):
    rt_cfg = _runtime(
        grounding_model=dict(
            enabled=True,
            provider="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            model="m",
        ),
        allowlist=["openrouter.ai"],
        attested=True,
    )
    fallback = _build(wiring, allow_egress=True, runtime_config=rt_cfg, phi_mode=True)
    assert isinstance(fallback, OpenAICompatibleGrounder)


def test_no_runtime_config_is_historic_behavior(wiring):
    # No runtime_config => no model grounder, no crash (back-compatible).
    build_replayer(
        object(),
        allow_egress=True,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=True,
        phi_mode=False,
    )
    assert wiring["fallback"] is None
