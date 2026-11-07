"""Provider adapters for ``SkillCache`` and compile-agent.

The Claude adapter delegates to compile-agent's existing native computer-use
loop. The OpenAI adapter uses the official Agents SDK with OpenAdapt's
``BrowserTools`` functions; it is browser-tool orchestration, not a claim of
the SDK's native desktop-computer tool. Both leave write checks, capture and
repair decisions to compile-agent's existing pipeline.

No provider call is made by :func:`provider_status`. It reports local SDK and
opaque-key configuration only; it cannot establish account, network or model
availability. Cost caps are required explicitly for every constructed agent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import os
import time
from functools import wraps
from typing import Any, Literal, Mapping, Optional

from openadapt_flow.compile_agent.agents import (
    DEFAULT_MODEL,
    PRICES_PER_MTOK,
    AgentLedger,
    BudgetExceeded,
    ClaudeAgent,
    ModelCall,
    ToolInvocation,
)
from openadapt_flow.integrations.agents.skills import BrowserTools

__all__ = ["agent_usage", "build_agent", "provider_status"]

Provider = Literal["claude", "openai"]
_PRICE_FIELDS = ("input", "output", "cache_write", "cache_read")


def provider_status(
    provider: Provider,
    *,
    max_cost_usd: Optional[float],
    model: Optional[str] = None,
    prices: Optional[Mapping[str, float]] = None,
    opaque_key_configured: bool = False,
) -> dict[str, Any]:
    """Return a no-network configuration check without exposing credentials.

    ``opaque_key_configured`` is supplied by the trusted caller from its
    existing key provider. It is a boolean only; this function never receives
    or reads a key value. For OpenAI's default SDK client, environment-key
    availability is checked by membership only.
    """

    if provider not in ("claude", "openai"):
        raise ValueError(f"unsupported provider: {provider!r}")
    cap = _valid_cap(max_cost_usd)
    configured_cap = float(max_cost_usd) if cap else None
    if not cap:
        return {
            "provider": provider,
            "available": False,
            "blocker": "explicit positive max_cost_usd is required",
            "configured_cap_usd": None,
            "check_scope": "local SDK/key configuration only; no reachability check",
        }

    if provider == "openai":
        pricing_available = _prices_are_valid(prices)
    else:
        pricing_available = (
            model or DEFAULT_MODEL
        ) in PRICES_PER_MTOK or _prices_are_valid(prices)

    module = "anthropic" if provider == "claude" else "agents"
    try:
        sdk_available = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        sdk_available = False

    if provider == "openai":
        # Membership checks presence without returning or logging the value.
        key_available = opaque_key_configured or "OPENAI_API_KEY" in os.environ
    else:
        key_available = opaque_key_configured

    blocker = None
    if not sdk_available:
        blocker = f"optional {module} SDK is not installed"
    elif not key_available:
        blocker = "opaque provider credential is not configured"
    elif not pricing_available:
        blocker = "explicit pricing for this model is not configured"
    return {
        "provider": provider,
        "available": sdk_available and key_available and pricing_available,
        "blocker": blocker,
        "configured_cap_usd": configured_cap,
        "check_scope": "local SDK/key configuration only; no reachability check",
    }


def build_agent(
    provider: Provider,
    *,
    model: Optional[str] = None,
    max_turns: int,
    max_cost_usd: float,
    prices: Optional[Mapping[str, float]] = None,
    client: Any = None,
) -> Any:
    """Build an agent accepted by ``SkillCache.run``/``compile_first_run``.

    Prices are USD per million tokens. OpenAI prices must be supplied by the
    caller for the exact model because pricing changes; this module has no
    implicit price table or default spend cap. Claude uses compile-agent's
    existing reviewed model price table and still requires an explicit cap.
    ``client`` injects Claude's existing client object; for OpenAI it is an
    optional client factory called with the request's customer key and
    ``max_retries=0``. It cannot bypass the request's model-key provenance.
    """

    _require_cap(max_cost_usd)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")
    if provider == "claude":
        kwargs: dict[str, Any] = {
            "vision": "every_turn",
            "max_turns": max_turns,
            "max_cost_usd": max_cost_usd,
        }
        if model is not None:
            kwargs["model"] = model
        if prices is not None:
            kwargs["prices"] = _validated_prices(prices)
        if client is not None:
            kwargs["client"] = client
        return ClaudeAgent(**kwargs)
    if provider == "openai":
        if not model:
            raise ValueError("OpenAI provider requires an explicit model id")
        return OpenAIBrowserAgent(
            model=model,
            max_turns=max_turns,
            max_cost_usd=max_cost_usd,
            prices=_validated_prices(prices),
            client_factory=client,
        )
    raise ValueError(f"unsupported provider: {provider!r}")


def agent_usage(agent: Any) -> dict[str, Any]:
    """Project the latest real provider ledger into cache-safe usage facts.

    The call count is the number of requests started, including failed or
    interrupted requests. Cost is reported only when every started request
    returned complete usage; a partial known subtotal is never presented as a
    complete total.
    """

    ledger = getattr(agent, "last_ledger", None)
    if not isinstance(ledger, AgentLedger):
        return {"usage": None, "usage_known": False}
    calls = ledger.model_calls
    has_price = ledger.prices is not None or ledger.model in PRICES_PER_MTOK
    known = bool(ledger.usage_known and has_price)
    if calls == 0:
        return {
            "usage": [] if ledger.usage_known else None,
            "usage_known": bool(ledger.usage_known),
        }
    cost = ledger.cost_usd() if known else None
    if cost is not None and (not math.isfinite(cost) or cost < 0):
        return {
            "usage": [{"calls": calls, "usd": None}],
            "usage_known": False,
        }
    return {
        "usage": [{"calls": calls, "usd": cost}],
        "usage_known": known,
    }


class OpenAIBrowserAgent:
    """OpenAI Agents SDK agent using the guarded browser tools from Flow."""

    dialect = "generic"
    requires_model_key = True
    model_key_provider = "openai"

    def __init__(
        self,
        *,
        model: str,
        max_turns: int,
        max_cost_usd: float,
        prices: Mapping[str, float],
        max_input_bytes: int = 65536,
        max_output_tokens: int = 1024,
        client_factory: Any = None,
    ) -> None:
        _require_cap(max_cost_usd)
        if not model:
            raise ValueError("model must be non-empty")
        if (
            isinstance(max_turns, bool)
            or not isinstance(max_turns, int)
            or max_turns < 1
        ):
            raise ValueError("max_turns must be a positive integer")
        for name, value in (
            ("max_input_bytes", max_input_bytes),
            ("max_output_tokens", max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.model = model
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        self.prices = dict(prices)
        # The preflight uses UTF-8 bytes, a conservative upper bound for the
        # request's visible-content token count.
        self.max_input_bytes = max_input_bytes
        self.max_output_tokens = max_output_tokens
        worst_input_rate = max(
            self.prices[key] for key in ("input", "cache_write", "cache_read")
        )
        maximum_run_cost = (
            self.max_turns
            * (
                self.max_input_bytes * worst_input_rate
                + self.max_output_tokens * self.prices["output"]
            )
            / 1_000_000
        )
        if maximum_run_cost > self.max_cost_usd:
            raise ValueError(
                "configured run cap is below the worst-case token-budget cost"
            )
        self.principal_id = f"agent:openai-agents:{model}"
        self.last_ledger: Optional[AgentLedger] = None

    def drive(self, request: Any, harness: Any) -> AgentLedger:
        """Run the SDK on a worker while browser actions stay on the owner thread."""

        from agents import (
            Agent,
            ModelSettings,
            OpenAIResponsesModel,
            RunHooks,
            Runner,
            function_tool,
        )
        from openai import AsyncOpenAI

        ledger = AgentLedger(
            agent="openai-agents-browser-tools",
            model=self.model,
            vision="browser-tools",
            prices=self.prices,
        )
        self.last_ledger = ledger
        browser = BrowserTools(harness)
        started_at: list[float] = []

        class UsageHooks(RunHooks):
            async def on_llm_start(
                hook_self,
                context: Any,
                agent: Any,
                system_prompt: Any,
                input_items: Any,
            ) -> None:
                request_size = _request_upper_bound_bytes(
                    system_prompt, input_items, getattr(agent, "tools", ())
                )
                if request_size > self.max_input_bytes:
                    raise BudgetExceeded(
                        "rendered request exceeds the configured input-token ceiling"
                    )
                ledger.model_requests_started += 1
                ledger.usage_known = False
                started_at.append(time.monotonic())

            async def on_llm_end(
                hook_self, context: Any, agent: Any, response: Any
            ) -> None:
                started = started_at.pop() if started_at else time.monotonic()
                usage = getattr(response, "usage", None)
                parsed = _openai_usage(usage)
                if parsed is None:
                    ledger.usage_known = False
                    raise RuntimeError(
                        "OpenAI Agents SDK returned no usable per-call token usage"
                    )
                input_tokens, output_tokens, cached_tokens, cache_write_tokens = parsed
                ledger.calls.append(
                    ModelCall(
                        turn=len(ledger.calls) + 1,
                        input_tokens=input_tokens - cached_tokens - cache_write_tokens,
                        output_tokens=output_tokens,
                        cache_creation_input_tokens=cache_write_tokens,
                        cache_read_input_tokens=cached_tokens,
                        seconds=round(time.monotonic() - started, 6),
                        stop_reason=None,
                        images_in_request=0,
                    )
                )
                ledger.model_seconds += ledger.calls[-1].seconds
                ledger.usage_known = True
                if ledger.cost_usd() > self.max_cost_usd:
                    raise BudgetExceeded(
                        "estimated OpenAI spend passed the configured run cap"
                    )

        def instrument(name: str, fn: Any) -> Any:
            @wraps(fn)
            def called(*args: Any, **kwargs: Any) -> Any:
                started = time.monotonic()
                outcome = "returned"
                try:
                    value = fn(*args, **kwargs)
                    if isinstance(value, str):
                        if value.startswith("Stopped:"):
                            outcome = "refused"
                        elif value.startswith("Not done:"):
                            outcome = "retryable_error"
                    return value
                except BaseException:
                    outcome = "failed"
                    raise
                finally:
                    ledger.tool_invocations.append(
                        ToolInvocation(
                            name=name,
                            seconds=round(time.monotonic() - started, 6),
                            outcome=outcome,
                        )
                    )

            return called

        tools = [
            function_tool(instrument(fn.__name__, fn)) for fn in browser.functions()
        ]
        agent_options: dict[str, Any] = {
            "name": "OpenAdapt browser agent",
            "instructions": (
                "Complete the user's task using only the supplied browser tools. "
                "Use observe to inspect the page, act through the named controls, "
                "and call finish with a concise outcome when done or blocked. "
                "Do not claim a write succeeded unless the tool's readback shows it."
            ),
            "tools": tools,
            "model_settings": ModelSettings(
                max_tokens=self.max_output_tokens,
                preserve_raw_usage=True,
                parallel_tool_calls=False,
            ),
        }
        request_key = getattr(request, "model_key", None)
        if request_key is None or getattr(request_key, "provider", None) != "openai":
            raise RuntimeError(
                "OpenAI browser agent needs the customer's OpenAI model key"
            )
        client = (client_factory or AsyncOpenAI)(
            api_key=request_key.secret(), max_retries=0
        )
        agent_options["model"] = OpenAIResponsesModel(
            model=self.model,
            openai_client=client,
        )
        agent = Agent(**agent_options)

        async def run() -> Any:
            try:
                return await Runner.run(
                    agent,
                    f"Task: {request.instruction}",
                    max_turns=self.max_turns,
                    hooks=UsageHooks(),
                )
            finally:
                await client.close()

        try:
            result = browser.serve_while(lambda: asyncio.run(run()))
        finally:
            ledger.wall_seconds = time.monotonic() - ledger.started_at
            ledger.tool_errors = browser.errors
            if started_at:
                # A request started without an on_llm_end receipt has unknown
                # usage. Never turn that into a zero-call/zero-cost report.
                ledger.usage_known = False
        ledger.summary = browser.summary
        if ledger.summary is None:
            output = getattr(result, "final_output", None)
            ledger.summary = output if isinstance(output, str) else None
        ledger.finished = browser.summary is not None
        return ledger


def _openai_usage(usage: Any) -> Optional[tuple[int, int, int, int]]:
    """Read the Responses API's per-call token facts.

    OpenAI reports cached input but has no cache-write token category. Missing
    cached-token detail remains unknown rather than being silently priced as
    ordinary input.
    """

    if usage is None:
        return None
    input_tokens = _nonnegative_int(getattr(usage, "input_tokens", None))
    output_tokens = _nonnegative_int(getattr(usage, "output_tokens", None))
    if input_tokens is None or output_tokens is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = _nonnegative_int(getattr(details, "cached_tokens", None))
    if cached_tokens is None or cached_tokens > input_tokens:
        return None
    return input_tokens, output_tokens, cached_tokens, 0


def _request_upper_bound_bytes(system_prompt: Any, input_items: Any, tools: Any) -> int:
    """Conservatively bound request tokens by UTF-8 bytes before model call."""

    import json

    def json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return json_value(dump(mode="json"))
        return str(value)

    def serialized_size(value: Any) -> int:
        return len(json.dumps(json_value(value), ensure_ascii=False).encode("utf-8"))

    tool_schemas = [getattr(tool, "params_json_schema", {}) for tool in (tools or ())]
    # UTF-8 byte count upper-bounds BPE tokens for visible content; reserve a
    # fixed allowance for protocol framing and SDK-injected metadata.
    return (
        serialized_size(system_prompt)
        + serialized_size(input_items)
        + serialized_size(tool_schemas)
        + 4096
    )


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _valid_cap(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _require_cap(value: Any) -> None:
    if not _valid_cap(value):
        raise ValueError("max_cost_usd must be a finite positive number")


def _validated_prices(prices: Optional[Mapping[str, float]]) -> dict[str, float]:
    if prices is None or set(prices) != set(_PRICE_FIELDS):
        raise ValueError(
            "OpenAI requires explicit input, output, cache_write and cache_read "
            "prices in USD per million tokens"
        )
    out: dict[str, float] = {}
    for key in _PRICE_FIELDS:
        value = prices[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError(f"price {key!r} must be a finite non-negative number")
        out[key] = float(value)
    return out


def _prices_are_valid(prices: Optional[Mapping[str, float]]) -> bool:
    try:
        _validated_prices(prices)
    except (TypeError, ValueError):
        return False
    return True
