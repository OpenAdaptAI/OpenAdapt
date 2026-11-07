from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openadapt_flow.compile_agent.agents import AgentLedger, BudgetExceeded, ModelCall
from openadapt_flow.compile_agent import agents as compile_agent_module
from openadapt_flow.integrations.agents.providers import (
    _openai_usage,
    _request_upper_bound_bytes,
    agent_usage,
    build_agent,
    provider_status,
)
from openadapt_flow.integrations.agents.skills import FrameworkAgent, _skill_usage

OPENAI_PRICE_METADATA = {
    "price_model": "gpt-6-luna",
    "pricing_source_url": "https://developers.openai.com/api/docs/pricing",
    "pricing_verified_at": "2026-11-07",
}


def test_openai_usage_uses_responses_cached_input_and_known_zero_cache_writes():
    usage = SimpleNamespace(
        input_tokens=120,
        output_tokens=8,
        input_tokens_details=SimpleNamespace(cached_tokens=40),
    )

    assert _openai_usage(usage) == (120, 8, 40, 0)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        SimpleNamespace(input_tokens=4, output_tokens=1),
        SimpleNamespace(
            input_tokens=True,
            output_tokens=1,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
        SimpleNamespace(
            input_tokens=4,
            output_tokens=1,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
        ),
    ],
)
def test_openai_usage_refuses_incomplete_or_invalid_details(usage):
    assert _openai_usage(usage) is None


def test_request_size_serializes_nested_sdk_objects_without_losing_fields():
    class Nested:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {"payload": "é" * 12}

    actual = _request_upper_bound_bytes(
        "system", [{"nested": Nested()}], [SimpleNamespace(params_json_schema={"x": 1})]
    )
    expected = (
        len(json.dumps("system", ensure_ascii=False).encode("utf-8"))
        + len(
            json.dumps([{"nested": {"payload": "é" * 12}}], ensure_ascii=False).encode(
                "utf-8"
            )
        )
        + len(json.dumps([{"x": 1}], ensure_ascii=False).encode("utf-8"))
        + 4096
    )
    assert actual == expected


def test_openai_factory_requires_finite_cost_bound_and_explicit_prices():
    with pytest.raises(ValueError, match="finite positive"):
        build_agent("openai", model="gpt-6-luna", max_turns=1, max_cost_usd=0)
    with pytest.raises(ValueError, match="explicit input, output"):
        build_agent("openai", model="gpt-6-luna", max_turns=1, max_cost_usd=5)
    with pytest.raises(ValueError, match="worst-case token-budget"):
        build_agent(
            "openai",
            model="gpt-6-luna",
            max_turns=2,
            max_cost_usd=0.01,
            prices={"input": 1, "output": 1, "cache_write": 1, "cache_read": 1},
            **OPENAI_PRICE_METADATA,
        )


def test_provider_status_is_local_configuration_only(monkeypatch):
    monkeypatch.setattr(
        "openadapt_flow.integrations.agents.providers.importlib.util.find_spec",
        lambda name: object() if name == "agents" else None,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prices = {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1}

    unavailable = provider_status(
        "openai", model="gpt-6-luna", max_cost_usd=5, prices=prices
    )
    configured = provider_status(
        "openai",
        model="gpt-6-luna",
        max_cost_usd=5,
        prices=prices,
        **OPENAI_PRICE_METADATA,
        opaque_key_configured=True,
    )

    assert unavailable["available"] is False
    assert configured["available"] is True
    assert "no reachability check" in configured["check_scope"]
    assert "api_key" not in repr(configured).lower()


def test_agent_usage_preserves_started_count_and_unknown_cost():
    agent = SimpleNamespace(
        last_ledger=AgentLedger(
            agent="openai-agents-browser-tools",
            model="gpt-6-luna",
            prices={"input": 1, "output": 1, "cache_write": 1, "cache_read": 1},
            model_requests_started=2,
            usage_known=False,
            calls=[
                ModelCall(
                    turn=1,
                    input_tokens=10,
                    output_tokens=2,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    seconds=0.1,
                    stop_reason=None,
                    images_in_request=0,
                )
            ],
        )
    )

    assert agent_usage(agent) == {
        "usage": [{"calls": 2, "usd": None}],
        "usage_known": False,
    }
    assert agent.last_ledger.to_dict()["cost_usd"] is None


def test_agent_usage_marks_actual_zero_request_path():
    agent = SimpleNamespace(last_ledger=AgentLedger(agent="scripted"))
    assert agent_usage(agent) == {"usage": [], "usage_known": True}


def test_framework_agent_missing_usage_is_not_reported_as_zero_cost():
    agent = FrameworkAgent(lambda _task, _tools: None, model="unknown-model")
    ledger = agent.drive(SimpleNamespace(instruction="inspect"), harness=object())

    assert ledger.model_calls == 0
    assert ledger.usage_known is False
    assert agent_usage(agent) == {"usage": None, "usage_known": False}


def test_framework_agent_requires_complete_call_usage_fields():
    agent = FrameworkAgent(
        lambda _task, _tools: {"calls": [{"input_tokens": 3}]},
        model="unknown-model",
    )
    with pytest.raises(ValueError, match="output_tokens"):
        agent.drive(SimpleNamespace(instruction="inspect"), harness=object())
    assert agent.last_ledger is not None
    assert agent.last_ledger.model_calls == 1
    assert agent.last_ledger.usage_known is False


def test_skill_replay_zero_calls_requires_explicit_usage_receipt():
    assert _skill_usage({"model_calls": 0}) == (0, None, None, False)
    assert _skill_usage(
        {"model_calls": 0, "usage": [], "usage_known": True}
    ) == (0, 0.0, [], True)


def test_skill_path_preserves_fallback_usage_or_unknown_cost():
    assert _skill_usage(
        {
            "model_calls": 2,
            "usage": [{"calls": 2, "usd": 0.04}],
            "usage_known": True,
        }
    ) == (2, 0.04, [{"calls": 2, "usd": 0.04}], True)
    assert _skill_usage(
        {
            "model_calls": 2,
            "usage": [{"calls": 2, "usd": None}],
            "usage_known": False,
        }
    ) == (2, None, [{"calls": 2, "usd": None}], False)


def test_openai_browser_adapter_runs_the_sdk_with_a_scripted_local_model(monkeypatch):
    agents = pytest.importorskip("agents")
    openai = pytest.importorskip("openai")
    from agents import Model, ModelResponse, Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    from openadapt_flow.integrations.agents.providers import OpenAIBrowserAgent

    usage = Usage()
    usage.input_tokens = 12
    usage.output_tokens = 3
    if usage.input_tokens_details is None:
        usage.input_tokens_details = SimpleNamespace(cached_tokens=0)
    else:
        usage.input_tokens_details.cached_tokens = 0

    class ScriptedModel(Model):
        async def get_response(self, *_args, **_kwargs):
            message = ResponseOutputMessage(
                type="message",
                id="m_local",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text", text="done", annotations=[]
                    )
                ],
            )
            return ModelResponse(output=[message], usage=usage, response_id="r_local")

        def stream_response(self, *_args, **_kwargs):  # pragma: no cover - unused
            raise NotImplementedError

    class NoNetworkClient:
        def __init__(self, *, api_key, max_retries, timeout):
            assert api_key == "test-only-opaque-key"
            assert max_retries == 0
            assert timeout == 25.0
            self.closed = False

        async def close(self):
            self.closed = True

    model = ScriptedModel()
    clients = []

    def client_factory(**kwargs):
        client = NoNetworkClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(agents, "OpenAIResponsesModel", lambda **_kwargs: model)
    monkeypatch.setattr(openai, "AsyncOpenAI", client_factory)
    agent = OpenAIBrowserAgent(
        model="scripted-test-model",
        max_turns=1,
        max_cost_usd=1,
        prices={"input": 1, "output": 1, "cache_write": 1, "cache_read": 1},
        price_model="scripted-test-model",
        pricing_source_url="https://developers.openai.com/api/docs/pricing",
        pricing_verified_at="2026-11-07",
    )
    request = SimpleNamespace(
        instruction="inspect the current page",
        model_key=SimpleNamespace(
            provider="openai", secret=lambda: "test-only-opaque-key"
        ),
    )

    ledger = agent.drive(request, harness=object())

    assert ledger.model_calls == 1
    assert ledger.usage_known is True
    assert ledger.total("input_tokens") == 12
    assert ledger.total("output_tokens") == 3
    assert agent_usage(agent)["usage_known"] is True
    assert len(clients) == 1 and clients[0].closed is True


def test_openai_provider_requires_reviewed_prices_for_exact_model():
    prices = {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1}
    with pytest.raises(ValueError, match="reviewed pricing metadata"):
        build_agent(
            "openai", model="gpt-6-luna", max_turns=1, max_cost_usd=1, prices=prices
        )
    with pytest.raises(ValueError, match="reviewed pricing metadata"):
        build_agent(
            "openai",
            model="gpt-6-luna",
            max_turns=1,
            max_cost_usd=1,
            prices=prices,
            **{**OPENAI_PRICE_METADATA, "price_model": "gpt-6-other"},
        )


def test_claude_reserves_before_generation_and_disables_retries(monkeypatch):
    class Messages:
        def __init__(self):
            self.created = 0

        def count_tokens(self, **kwargs):
            assert kwargs["model"] == "claude-opus-5-5"
            return SimpleNamespace(input_tokens=100_000)

        def create(self, **_kwargs):
            self.created += 1
            raise AssertionError("generation must not start after reservation refusal")

    class FakeClient:
        def __init__(self):
            self.messages = Messages()
            self.options = []

        def with_options(self, *, timeout, max_retries):
            self.options.append((timeout, max_retries))
            return self

    client = FakeClient()
    monkeypatch.setattr(
        compile_agent_module,
        "_image",
        lambda _png: {"type": "image", "source": {"data": "test"}},
    )
    agent = build_agent(
        "claude",
        model="claude-opus-5-5",
        max_turns=1,
        max_cost_usd=0.001,
        max_output_tokens=10,
        client=client,
    )
    harness = SimpleNamespace(screenshot=lambda _reason: b"png")
    request = SimpleNamespace(instruction="inspect", model_key=None)

    with pytest.raises(BudgetExceeded, match="reservation"):
        agent.drive(request, harness)

    assert client.messages.created == 0
    assert client.options == [(25.0, 0)]
    assert agent.last_ledger.model_calls == 0
    assert agent_usage(agent) == {"usage": [], "usage_known": True}


def test_claude_token_count_precedes_bounded_generation(monkeypatch):
    class Messages:
        def __init__(self):
            self.events = []

        def count_tokens(self, **_kwargs):
            self.events.append("count")
            return SimpleNamespace(input_tokens=10)

        def create(self, **kwargs):
            self.events.append("generate")
            assert kwargs["max_tokens"] == 10
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                content=[{"type": "text", "text": "done"}],
                stop_reason="end_turn",
            )

    class FakeClient:
        def __init__(self):
            self.messages = Messages()
            self.options = []

        def with_options(self, *, timeout, max_retries):
            self.options.append((timeout, max_retries))
            return self

    client = FakeClient()
    monkeypatch.setattr(
        compile_agent_module,
        "_image",
        lambda _png: {"type": "image", "source": {"data": "test"}},
    )
    agent = build_agent(
        "claude",
        model="claude-opus-5-5",
        max_turns=1,
        max_cost_usd=0.001,
        max_output_tokens=10,
        client=client,
    )
    ledger = agent.drive(
        SimpleNamespace(instruction="inspect", model_key=None),
        SimpleNamespace(screenshot=lambda _reason: b"png"),
    )

    assert client.messages.events == ["count", "generate"]
    assert client.options == [(25.0, 0), (25.0, 0)]
    assert ledger.model_calls == 1 and ledger.usage_known is True


def test_agent_deadline_and_request_timeout_are_bounded():
    with pytest.raises(ValueError, match="120-second ceiling"):
        build_agent(
            "claude",
            model="claude-opus-5-5",
            max_turns=1,
            max_cost_usd=1,
            max_run_seconds=121,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        build_agent(
            "claude",
            model="claude-opus-5-5",
            max_turns=1,
            max_cost_usd=1,
            max_run_seconds=10,
            request_timeout_seconds=11,
        )
