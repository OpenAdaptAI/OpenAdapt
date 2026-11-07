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
from datetime import date
from decimal import Decimal
from functools import wraps
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlparse

from openadapt_flow.compile_agent.agents import (
    DEFAULT_MODEL,
    PRICES_PER_MTOK,
    AgentLedger,
    BudgetExceeded,
    ClaudeAgent,
    ModelCall,
    ToolInvocation,
    _exact_ledger_cost,
)
from openadapt_flow.integrations.agents.skills import BrowserTools

__all__ = ["agent_usage", "build_agent", "provider_status"]

Provider = Literal["claude", "openai"]
_PRICE_FIELDS = ("input", "output", "cache_write", "cache_read")
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_REVIEWED_AT = "2026-11-07"
MAX_AGENT_RUN_SECONDS = 120.0


def provider_status(
    provider: Provider,
    *,
    max_cost_usd: Optional[float],
    model: Optional[str] = None,
    prices: Optional[Mapping[str, float]] = None,
    price_model: Optional[str] = None,
    pricing_source_url: Optional[str] = None,
    pricing_verified_at: Optional[str] = None,
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
        pricing_available = _reviewed_openai_prices(
            model=model,
            prices=prices,
            price_model=price_model,
            source_url=pricing_source_url,
            verified_at=pricing_verified_at,
        )
    else:
        selected = model or DEFAULT_MODEL
        pricing_available = (
            prices is None and selected in PRICES_PER_MTOK
        ) or _reviewed_prices(
            model=selected,
            prices=prices,
            price_model=price_model,
            source_url=pricing_source_url,
            verified_at=pricing_verified_at,
            expected_host="www.anthropic.com",
        )

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
    price_model: Optional[str] = None,
    pricing_source_url: Optional[str] = None,
    pricing_verified_at: Optional[str] = None,
    max_input_bytes: int = 65536,
    max_output_tokens: int = 1024,
    max_run_seconds: float = 110.0,
    request_timeout_seconds: float = 25.0,
    client: Any = None,
) -> Any:
    """Build an agent accepted by ``SkillCache.run``/``compile_first_run``.

    Prices are USD per million tokens. OpenAI prices must be supplied for the
    exact model with an official pricing URL and review date; this module has
    no implicit OpenAI price table or default spend cap. Claude uses
    compile-agent's reviewed prices for its exact listed model. Both adapters
    reserve worst-case request cost before generation and use one bounded run
    deadline.
    ``client`` injects Claude's existing client object; for OpenAI it is an
    optional client factory called with the request's customer key and
    ``max_retries=0``. It cannot bypass the request's model-key provenance.
    """

    _require_cap(max_cost_usd)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")
    _validate_deadlines(max_run_seconds, request_timeout_seconds)
    for name, value in (
        ("max_input_bytes", max_input_bytes),
        ("max_output_tokens", max_output_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if provider == "claude":
        selected_model = model or "claude-opus-5-5"
        built_in_reviewed = prices is None and selected_model in PRICES_PER_MTOK
        supplied_reviewed = _reviewed_prices(
            model=selected_model,
            prices=prices,
            price_model=price_model,
            source_url=pricing_source_url,
            verified_at=pricing_verified_at,
            expected_host="www.anthropic.com",
        )
        if not (built_in_reviewed or supplied_reviewed):
            raise ValueError("Claude model requires exact-model reviewed pricing")
        kwargs: dict[str, Any] = {
            "vision": "every_turn",
            "max_turns": max_turns,
            "max_cost_usd": max_cost_usd,
            "preflight_required": True,
            "max_input_bytes": max_input_bytes,
            "max_tokens": max_output_tokens,
            "max_run_seconds": max_run_seconds,
            "request_timeout_seconds": request_timeout_seconds,
            "pricing_source_url": (
                pricing_source_url or _anthropic_pricing_url(selected_model)
            ),
            "pricing_verified_at": pricing_verified_at or PRICING_REVIEWED_AT,
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
            price_model=price_model,
            pricing_source_url=pricing_source_url,
            pricing_verified_at=pricing_verified_at,
            max_input_bytes=max_input_bytes,
            max_output_tokens=max_output_tokens,
            max_run_seconds=max_run_seconds,
            request_timeout_seconds=request_timeout_seconds,
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
        price_model: Optional[str],
        pricing_source_url: Optional[str],
        pricing_verified_at: Optional[str],
        max_input_bytes: int = 65536,
        max_output_tokens: int = 1024,
        max_run_seconds: float = 110.0,
        request_timeout_seconds: float = 25.0,
        client_factory: Any = None,
    ) -> None:
        _require_cap(max_cost_usd)
        if not model:
            raise ValueError("model must be non-empty")
        if not _reviewed_openai_prices(
            model=model,
            prices=prices,
            price_model=price_model,
            source_url=pricing_source_url,
            verified_at=pricing_verified_at,
        ):
            raise ValueError("OpenAI requires exact-model reviewed pricing metadata")
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
        self.pricing_source_url = pricing_source_url
        self.pricing_verified_at = pricing_verified_at
        # The preflight uses UTF-8 bytes, a conservative upper bound for the
        # request's visible-content token count.
        self.max_input_bytes = max_input_bytes
        self.max_output_tokens = max_output_tokens
        _validate_deadlines(max_run_seconds, request_timeout_seconds)
        self.max_run_seconds = max_run_seconds
        self.request_timeout_seconds = request_timeout_seconds
        worst_input_rate = max(
            Decimal(str(self.prices[key]))
            for key in ("input", "cache_write", "cache_read")
        )
        maximum_run_cost = (
            Decimal(self.max_turns)
            * (
                Decimal(self.max_input_bytes) * worst_input_rate
                + Decimal(self.max_output_tokens) * Decimal(str(self.prices["output"]))
            )
            / Decimal(1_000_000)
        )
        if maximum_run_cost > Decimal(str(self.max_cost_usd)):
            raise ValueError(
                "configured run cap is below the worst-case token-budget cost"
            )
        self.principal_id = f"agent:openai-agents:{model}"
        self.client_factory = client_factory
        self.last_ledger: Optional[AgentLedger] = None

    def drive(self, request: Any, harness: Any) -> AgentLedger:
        """Run the SDK on a worker while browser actions stay on the owner thread."""

        from agents import (
            Agent,
            ModelSettings,
            OpenAIResponsesModel,
            RunConfig,
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
            pricing_model=self.model,
            pricing_source_url=self.pricing_source_url,
            pricing_verified_at=self.pricing_verified_at,
            pricing_tier="default",
        )
        self.last_ledger = ledger
        browser = BrowserTools(harness)
        started_at: list[tuple[float, int]] = []
        deadline = time.monotonic() + self.max_run_seconds

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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("agent run deadline expired before request")
                reserve = _request_reservation_usd(
                    request_size, self.max_output_tokens, self.prices
                )
                if _exact_ledger_cost(ledger, self.prices) + reserve > Decimal(
                    str(self.max_cost_usd)
                ):
                    raise BudgetExceeded(
                        "worst-case request reservation exceeds remaining run budget"
                    )
                ledger.model_requests_started += 1
                ledger.usage_known = False
                started_at.append((time.monotonic(), request_size))

            async def on_llm_end(
                hook_self, context: Any, agent: Any, response: Any
            ) -> None:
                started, input_bound = (
                    started_at.pop() if started_at else (time.monotonic(), 0)
                )
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
                if output_tokens > self.max_output_tokens:
                    raise BudgetExceeded(
                        "OpenAI response exceeded the configured output-token ceiling"
                    )
                if input_tokens > input_bound:
                    raise BudgetExceeded(
                        "OpenAI usage exceeded the reserved request-size bound"
                    )
                if time.monotonic() >= deadline:
                    ledger.usage_known = False
                    raise TimeoutError("agent run deadline expired during request")
                if _exact_ledger_cost(ledger, self.prices) > Decimal(
                    str(self.max_cost_usd)
                ):
                    raise BudgetExceeded(
                        "estimated OpenAI spend passed the configured run cap"
                    )

        def instrument(name: str, fn: Any) -> Any:
            @wraps(fn)
            def called(*args: Any, **kwargs: Any) -> Any:
                if time.monotonic() >= deadline:
                    raise TimeoutError("agent run deadline expired before browser tool")
                started = time.monotonic()
                outcome = "returned"
                try:
                    value = fn(*args, **kwargs)
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "agent run deadline expired during browser tool"
                        )
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
                timeout=self.request_timeout_seconds,
                extra_args={"service_tier": "default"},
            ),
        }
        request_key = getattr(request, "model_key", None)
        if request_key is None or getattr(request_key, "provider", None) != "openai":
            raise RuntimeError(
                "OpenAI browser agent needs the customer's OpenAI model key"
            )
        client = (self.client_factory or AsyncOpenAI)(
            api_key=request_key.secret(),
            max_retries=0,
            timeout=self.request_timeout_seconds,
        )
        agent_options["model"] = OpenAIResponsesModel(
            model=self.model,
            openai_client=client,
        )
        agent = Agent(**agent_options)

        async def run() -> Any:
            try:
                return await asyncio.wait_for(
                    Runner.run(
                        agent,
                        f"Task: {request.instruction}",
                        max_turns=self.max_turns,
                        hooks=UsageHooks(),
                        run_config=RunConfig(tracing_disabled=True),
                    ),
                    timeout=self.max_run_seconds,
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


def _reviewed_openai_prices(
    *,
    model: Optional[str],
    prices: Optional[Mapping[str, float]],
    price_model: Optional[str],
    source_url: Optional[str],
    verified_at: Optional[str],
) -> bool:
    return (
        model is not None
        and price_model == model
        and _reviewed_prices(
            model=model,
            prices=prices,
            price_model=price_model,
            source_url=source_url,
            verified_at=verified_at,
            expected_host="developers.openai.com",
            expected_url=OPENAI_PRICING_URL,
        )
    )


def _anthropic_pricing_url(model: str) -> Optional[str]:
    if "opus" in model:
        return "https://www.anthropic.com/claude/opus"
    if "sonnet" in model:
        return "https://www.anthropic.com/claude/sonnet"
    return None


def _reviewed_prices(
    *,
    model: str,
    prices: Optional[Mapping[str, float]],
    price_model: Optional[str],
    source_url: Optional[str],
    verified_at: Optional[str],
    expected_host: str,
    expected_url: Optional[str] = None,
) -> bool:
    if (
        not model
        or price_model != model
        or not _prices_are_valid(prices)
        or not isinstance(source_url, str)
    ):
        return False
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (expected_url is not None and source_url != expected_url)
    ):
        return False
    if expected_host == "www.anthropic.com":
        expected_anthropic_url = (
            "https://www.anthropic.com/claude/opus"
            if "opus" in model
            else "https://www.anthropic.com/claude/sonnet"
            if "sonnet" in model
            else None
        )
        if source_url != expected_anthropic_url:
            return False
    if not isinstance(verified_at, str):
        return False
    try:
        reviewed = date.fromisoformat(verified_at)
    except ValueError:
        return False
    return reviewed <= date.today()


def _validate_deadlines(max_run_seconds: float, request_timeout_seconds: float) -> None:
    for name, value in (
        ("max_run_seconds", max_run_seconds),
        ("request_timeout_seconds", request_timeout_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    if max_run_seconds > MAX_AGENT_RUN_SECONDS:
        raise ValueError("max_run_seconds exceeds the approved 120-second ceiling")
    if request_timeout_seconds > max_run_seconds:
        raise ValueError("request timeout cannot exceed the total run deadline")


def _request_reservation_usd(
    input_token_upper_bound: int,
    max_output_tokens: int,
    prices: Mapping[str, float],
) -> Decimal:
    input_rate = max(
        Decimal(str(prices[key])) for key in ("input", "cache_write", "cache_read")
    )
    amount = (
        Decimal(input_token_upper_bound) * input_rate
        + Decimal(max_output_tokens) * Decimal(str(prices["output"]))
    ) / Decimal(1_000_000)
    return amount
