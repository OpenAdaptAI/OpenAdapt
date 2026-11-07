"""Agents that drive the compile-agent harness on run 1.

:class:`ClaudeAgent` is a live Claude computer-use loop on the customer's own
key, in one of three observation modes:

``code`` (default)
    Code as action (:mod:`.code_action`): one ``run`` tool that takes a short
    script. The model queries the structure, acts by role and name, reads
    back, keeps notes in a sandboxed working directory, and asks for a
    screenshot only when it wants one. Several actions fit in one call. Every
    save is read back from the system of record before the agent goes on.
    Default model: ``claude-sonnet-5-5`` (:data:`CODE_DEFAULT_MODEL`).
``selective``
    The model reads the page's structure (:mod:`.observe`) and acts on refs
    with ``click``, ``type_text`` and ``press_key``. A screenshot enters its
    context only when it calls ``screenshot``, and after an action that
    committed a write (the verification gate).
``every_turn``
    The baseline: Claude's computer toolset (``computer_toolset_20260801``),
    with a screenshot returned after every action.
All modes perform every action through the same harness and capture computer,
so both are recorded and compiled the same way. Each model call's token usage
and time go into an :class:`AgentLedger`.

:class:`ScriptedAgent` follows a fixed list of steps by control name and makes
no model calls. Tests, demos and CI use it.

Other providers plug in through ``openadapt_flow.jit`` dialects: any object
with a ``dialect`` and ``drive(request, harness)`` works.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Mapping, Optional, Sequence

from openadapt_flow.compile_agent.harness import GateRefused, Harness, StaleRef
from openadapt_flow.jit.adapters import UnsupportedAction
from openadapt_flow.jit.compile_agent import TaskRequest
from openadapt_flow.teach_scope import ScopeViolation

__all__ = [
    "AgentLedger",
    "CODE_DEFAULT_MODEL",
    "BudgetExceeded",
    "ClaudeAgent",
    "DEFAULT_MODEL",
    "ModelCall",
    "ToolInvocation",
    "PRICES_PER_MTOK",
    "ScriptedAgent",
]

DEFAULT_MODEL = "claude-opus-5-5"
#: The model the code-as-action agent was measured with (scoreboard arm 7).
CODE_DEFAULT_MODEL = "claude-sonnet-5-5"
#: Opus 5.5 and later take computer use only as this toolset (no beta header).
COMPUTER_TOOLSET = "computer_toolset_20260801"

#: USD per million tokens: input, output, cache write (5 minute), cache read.
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {
        "input": 4.0,
        "output": 20.0,
        "cache_write": 5.0,
        "cache_read": 0.20,
    },
    "claude-sonnet-5-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_write": 2.5,
        "cache_read": 0.20,
    },
}

VISION_MODES = ("selective", "every_turn", "code")


class BudgetExceeded(RuntimeError):
    """The run's model spend passed its cap. The run ends with no draft."""


@dataclass
class ModelCall:
    """One request to the model."""

    turn: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    seconds: float
    stop_reason: Optional[str]
    images_in_request: int

    @property
    def prompt_tokens(self) -> int:
        """Every input token the model read: uncached, written and read cache."""

        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class ToolInvocation:
    """One actual tool invocation, without its arguments or returned data."""

    name: str
    seconds: float
    outcome: str


@dataclass
class AgentLedger:
    """What one agent run cost: model calls, tokens, time, and screenshots."""

    agent: str
    model: Optional[str] = None
    vision: Optional[str] = None
    calls: list[ModelCall] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    wall_seconds: float = 0.0
    model_seconds: float = 0.0
    finished: bool = False
    summary: Optional[str] = None
    tool_errors: int = 0
    prices: Optional[dict[str, float]] = None
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    usage_known: bool = True
    model_requests_started: int = 0
    pricing_model: Optional[str] = None
    pricing_source_url: Optional[str] = None
    pricing_verified_at: Optional[str] = None
    pricing_tier: Optional[str] = None

    @property
    def model_calls(self) -> int:
        # Count observed request starts even when a provider failed before
        # returning usage. Such a prefix is not a zero-call run.
        return max(self.model_requests_started, len(self.calls))

    def total(self, name: str) -> int:
        return sum(int(getattr(call, name)) for call in self.calls)

    def cost_usd(self) -> float:
        prices = self.prices or PRICES_PER_MTOK.get(self.model or "")
        if prices is None:
            return 0.0
        return (
            self.total("input_tokens") * prices["input"]
            + self.total("output_tokens") * prices["output"]
            + self.total("cache_creation_input_tokens") * prices["cache_write"]
            + self.total("cache_read_input_tokens") * prices["cache_read"]
        ) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        price_known = (
            self.prices is not None
            or self.model in PRICES_PER_MTOK
            or self.model_calls == 0
        )
        result = {
            "agent": self.agent,
            "model": self.model,
            "vision": self.vision,
            "model_calls": self.model_calls,
            "input_tokens": self.total("input_tokens"),
            "output_tokens": self.total("output_tokens"),
            "cache_creation_input_tokens": self.total("cache_creation_input_tokens"),
            "cache_read_input_tokens": self.total("cache_read_input_tokens"),
            "prompt_tokens": sum(call.prompt_tokens for call in self.calls),
            "images_in_requests": sum(call.images_in_request for call in self.calls),
            "cost_usd": (
                round(self.cost_usd(), 6) if self.usage_known and price_known else None
            ),
            "wall_seconds": round(self.wall_seconds, 3),
            "model_seconds": round(self.model_seconds, 3),
            "finished": self.finished,
            "summary": self.summary,
            "tool_errors": self.tool_errors,
            "calls": [dict(call.__dict__) for call in self.calls],
            "tool_invocations": [dict(call.__dict__) for call in self.tool_invocations],
            "usage_known": self.usage_known,
            "model_requests_started": self.model_calls,
        }
        if self.pricing_model is not None:
            result.update(
                {
                    "pricing_model": self.pricing_model,
                    "pricing_source_url": self.pricing_source_url,
                    "pricing_verified_at": self.pricing_verified_at,
                    "pricing_tier": self.pricing_tier,
                }
            )
        return result


# -- scripted ----------------------------------------------------------------------


@dataclass
class ScriptedAgent:
    """Follows ``steps`` by control name; no model calls.

    Each step is one of::

        {"click": "Open", "near": "Jane Sample"}   # near: text on the same line
        {"type": "Follow up in two weeks", "into": "Note"}
        {"type": "{note}", "into": "Note"}          # a task input by name
        {"press": "Enter"}

    A control that is missing or ambiguous ends the run with no draft.
    """

    steps: Sequence[Mapping[str, Any]]
    principal_id: Optional[str] = "agent:scripted"
    dialect: ClassVar[str] = "generic"
    requires_model_key: ClassVar[bool] = False

    def drive(self, request: TaskRequest, harness: Harness) -> AgentLedger:
        ledger = AgentLedger(agent="scripted")
        for step in self.steps:
            obs = harness.observe()
            if "click" in step:
                control = obs.find(str(step["click"]), near=step.get("near"))
                if control is None:
                    raise LookupError(f"no single control named {step['click']!r}")
                harness.click(control.ref)
            elif "type" in step:
                text = str(step["type"]).format(**request.inputs)
                ref = None
                if step.get("into"):
                    control = obs.find(str(step["into"]))
                    if control is None:
                        raise LookupError(f"no single field named {step['into']!r}")
                    ref = control.ref
                harness.type_text(text, ref)
            elif "press" in step:
                harness.press(str(step["press"]))
            else:
                raise ValueError(f"unknown step {dict(step)!r}")
        ledger.finished = True
        ledger.wall_seconds = time.monotonic() - ledger.started_at
        return ledger


# -- Claude -----------------------------------------------------------------------

_SELECTIVE_SYSTEM = """\
You operate a web application for the user through a harness. You see the \
page as a structural listing: one line per heading, text, table row or \
control. Controls carry refs such as [e3]; act on them with click, type_text \
and press_key. After every action you get a new listing, and refs change, so \
use the refs in the latest listing. Call screenshot only when the listing is \
not enough to decide what to do. Before any save, the harness checks that the \
right record is on screen; a save on the wrong record is refused and ends the \
run. Do exactly the task, with the values given, and nothing else. When the \
task is done, or cannot be done, call finish with a one-line summary."""

_EVERY_TURN_SYSTEM = """\
You operate a web application for the user with the computer tools. After \
every action you get a screenshot of the screen. Before any save, the \
harness checks that the right record is on screen; a save on the wrong \
record is refused and ends the run. Do exactly the task, with the values \
given, and nothing else. When the task is done, or cannot be done, call \
finish with a one-line summary."""

_CODE_SYSTEM = """\
You operate an application for the user through a terminal-style tool, run. \
Each call takes a short script, one command per line, and returns what each \
line printed. Read the app's structure with tree, find, text and read; act on \
elements by role and name with click, fill, select and press; check results \
by reading again. Put several commands in one script when you know what comes \
next: refs change after every action, so later lines in a script name their \
targets by role and name (click role=button name="Save"). Filter listings \
with grep= to keep them short. Call screenshot only \
when the text is not enough. Before any save, the harness checks that the \
right record is on screen; a save on the wrong record is refused and ends the \
run. Do exactly the task, with the values given, and nothing else. After the \
save, read the screen to confirm it. When the task is done, or cannot be \
done, call finish with a one-line summary."""

_FINISH_TOOL = {
    "name": "finish",
    "description": "End the task. Call it once, when the task is done or cannot be done.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One line: what was done."}
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}

_SELECTIVE_TOOLS = [
    {
        "name": "observe",
        "description": "Read the current page's structural listing again.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Get a screenshot of the page. Costs far more than a listing; use it "
            "only when the listing is not enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "click",
        "description": "Click the control with this ref from the latest listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "A ref such as e3."}
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text. With a ref, click that field first; without one, type "
            "into the focused field. Typing appends to what the field holds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "ref": {"type": "string", "description": "Optional field ref."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press_key",
        "description": "Press one key, such as Enter, Tab or Escape.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    _FINISH_TOOL,
]


def _run_tool_spec() -> dict[str, Any]:
    from openadapt_flow.compile_agent.code_action import COMMANDS

    return {
        "name": "run",
        "description": "Run a short script against the app.\n" + COMMANDS,
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Commands, one per line.",
                }
            },
            "required": ["script"],
            "additionalProperties": False,
        },
    }


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block.model_dump(exclude_none=True)


def _get(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _image(png: bytes) -> dict[str, Any]:
    """An image block for the model. Every screenshot the model sees leaves
    the machine, so it is redacted first (or sent raw under the recorded
    ``OPENADAPT_FLOW_SEND_UNSCRUBBED`` opt-out); without the privacy extra
    this raises and nothing is sent."""
    import base64

    from openadapt_flow.privacy import prepare_outbound_image

    png = prepare_outbound_image(png, "a compile-agent screenshot")

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }


def _count_images(messages: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                count += 1
            inner = block.get("content")
            if isinstance(inner, list):
                count += sum(
                    1 for b in inner if isinstance(b, dict) and b.get("type") == "image"
                )
    return count


def _request_size_bytes(system: Any, tools: Any, messages: Any, extra: Any) -> int:
    """Return a conservative serialized request size before provider I/O."""

    def json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return json_value(dump(exclude_none=True))
        raise TypeError("request contains an unsupported value")

    body = json.dumps(
        json_value(
            {
                "system": system,
                "tools": tools,
                "messages": messages,
                "extra": extra,
            }
        ),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(body.encode("utf-8"))


@dataclass
class ClaudeAgent:
    """A live Claude computer-use loop on the customer's own key.

    Args:
        vision: ``code`` (code as action, the default), ``selective``
            (structure first) or ``every_turn`` (the screenshot baseline).
        model: The model id. Default ``claude-sonnet-5-5`` for ``code``,
            ``claude-opus-5-5`` for the others.
        effort: ``output_config.effort``. Opus 5.5 defaults to ``medium``.
        max_turns: Model calls before the run is ended.
        max_cost_usd: Estimated spend before the run is ended
            (:class:`BudgetExceeded`), priced from :data:`PRICES_PER_MTOK`.
        client: An Anthropic client. Default: built from the request's
            customer key. Tests pass a fake.
        principal_id: The registered agent identity this loop records as.
    """

    vision: str = "code"
    model: str = ""
    effort: Optional[str] = "medium"
    max_turns: int = 40
    max_cost_usd: float = 2.0
    max_tokens: int = 8000
    client: Any = None
    principal_id: Optional[str] = None
    #: USD per million tokens (input, output, cache_write, cache_read) for a
    #: model not in :data:`PRICES_PER_MTOK`. The cost cap needs a price.
    prices: Optional[dict[str, float]] = None
    pricing_source_url: Optional[str] = None
    pricing_verified_at: Optional[str] = None
    #: The provider adapter enables token-count and per-request reservation
    #: before generation. Older direct callers retain their legacy behavior.
    preflight_required: bool = False
    max_input_bytes: int = 65536
    request_timeout_seconds: float = 25.0
    max_run_seconds: float = 110.0
    requires_model_key: ClassVar[bool] = True
    model_key_provider: ClassVar[str] = "anthropic"

    def __post_init__(self) -> None:
        if self.vision not in VISION_MODES:
            raise ValueError(f"vision must be one of {VISION_MODES}")
        if not self.model:
            self.model = CODE_DEFAULT_MODEL if self.vision == "code" else DEFAULT_MODEL
        if self.prices is None and self.model not in PRICES_PER_MTOK:
            raise ValueError(
                f"no price for {self.model!r}, so the cost cap can't hold; pass "
                "prices={'input': ..., 'output': ..., 'cache_write': ..., "
                "'cache_read': ...} in USD per million tokens"
            )
        if self.principal_id is None:
            self.principal_id = f"agent:{self.model}"

    @property
    def dialect(self) -> str:
        return "anthropic" if self.vision == "every_turn" else "generic"

    def _client(self, request: TaskRequest) -> Any:
        if self.client is not None:
            return self.client
        if request.model_key is None:
            raise RuntimeError("ClaudeAgent needs the customer's model key")
        import anthropic

        return anthropic.Anthropic(
            api_key=request.model_key.secret(),
            max_retries=0 if self.preflight_required else 4,
            timeout=self.request_timeout_seconds if self.preflight_required else 600.0,
        )

    def drive(self, request: TaskRequest, harness: Harness) -> AgentLedger:
        deadline = time.monotonic() + self.max_run_seconds
        client = self._client(request)
        ledger = AgentLedger(
            agent="claude",
            model=self.model,
            vision=self.vision,
            prices=self.prices,
            pricing_model=self.model if self.preflight_required else None,
            pricing_source_url=(
                self.pricing_source_url if self.preflight_required else None
            ),
            pricing_verified_at=(
                self.pricing_verified_at if self.preflight_required else None
            ),
        )
        #: Kept so a run that raises still reports what it spent.
        self.last_ledger = ledger
        selective = self.vision == "selective"
        self._tool: Any = None
        tools: list[dict[str, Any]]
        first: list[dict[str, Any]]
        if self.vision == "code":
            from openadapt_flow.compile_agent.code_action import (
                CodeActionTool,
                _watch,
            )

            self._tool = CodeActionTool(harness, watch=_watch(request, harness))
            self.last_trace = self._tool.trace
            system = _CODE_SYSTEM
            tools = [_run_tool_spec(), _FINISH_TOOL]
            first = [
                {"type": "text", "text": f"Task: {request.instruction}"},
                {
                    "type": "text",
                    "text": "The harness ran `tree` for you. Its output:\n\n"
                    + self._tool.run("tree").text,
                },
            ]
        elif selective:
            system = _SELECTIVE_SYSTEM
            tools = list(_SELECTIVE_TOOLS)
            first = [
                {"type": "text", "text": f"Task: {request.instruction}"},
                {"type": "text", "text": "Current page:\n" + harness.observe().text},
            ]
        else:
            system = _EVERY_TURN_SYSTEM
            tools = [
                {"type": COMPUTER_TOOLSET, "configs": {"zoom": {"enabled": False}}},
                _FINISH_TOOL,
            ]
            first = [
                {"type": "text", "text": f"Task: {request.instruction}"},
                _image(harness.screenshot("every_turn")),
            ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
        extra: dict[str, Any] = {"cache_control": {"type": "ephemeral"}}
        if self.effort:
            extra["output_config"] = {"effort": self.effort}
        try:
            for turn in range(1, self.max_turns + 1):
                self._check_run_deadline(deadline)
                call_client = client
                input_token_bound: Optional[int] = None
                if self.preflight_required:
                    request_size = _request_size_bytes(system, tools, messages, extra)
                    if request_size > self.max_input_bytes:
                        raise BudgetExceeded(
                            "rendered request exceeds the configured input-byte ceiling"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "agent run deadline expired before token count"
                        )
                    call_timeout = min(self.request_timeout_seconds, remaining)
                    with_options = getattr(client, "with_options", None)
                    if not callable(with_options):
                        raise RuntimeError(
                            "preflight client must support bounded no-retry request options"
                        )
                    call_client = with_options(timeout=call_timeout, max_retries=0)
                    count_response = call_client.messages.count_tokens(
                        model=self.model,
                        system=system,
                        tools=tools,
                        messages=messages,
                    )
                    counted_tokens = _get(count_response, "input_tokens")
                    if (
                        isinstance(counted_tokens, bool)
                        or not isinstance(counted_tokens, int)
                        or counted_tokens < 0
                    ):
                        raise RuntimeError(
                            "Claude token count was missing or invalid; generation refused"
                        )
                    input_token_bound = counted_tokens
                    self._reserve_claude_call(ledger, counted_tokens)
                    self._check_run_deadline(deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "agent run deadline expired before generation"
                        )
                    call_timeout = min(self.request_timeout_seconds, remaining)
                    call_client = with_options(timeout=call_timeout, max_retries=0)
                started = time.monotonic()
                ledger.model_requests_started += 1
                ledger.usage_known = False
                try:
                    response = call_client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system,
                        tools=tools,
                        messages=messages,
                        extra_body=extra,
                    )
                except BaseException:
                    # A failed request may have reached the provider, so its
                    # usage cannot be reported as a measured zero.
                    raise
                elapsed = time.monotonic() - started
                self._check_run_deadline(deadline)
                usage = _get(response, "usage")
                usage_fields = (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
                token_values = [_get(usage, field) for field in usage_fields]
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in token_values
                ):
                    raise RuntimeError(
                        "Claude returned incomplete per-call token usage"
                    )
                ledger.calls.append(
                    ModelCall(
                        turn=turn,
                        input_tokens=token_values[0],
                        output_tokens=token_values[1],
                        cache_creation_input_tokens=token_values[2],
                        cache_read_input_tokens=token_values[3],
                        seconds=round(elapsed, 3),
                        stop_reason=_get(response, "stop_reason"),
                        images_in_request=_count_images(messages),
                    )
                )
                ledger.usage_known = True
                ledger.model_seconds += elapsed
                observed_input_tokens = (
                    token_values[0] + token_values[2] + token_values[3]
                )
                if (
                    input_token_bound is not None
                    and observed_input_tokens > input_token_bound
                ):
                    raise BudgetExceeded(
                        "Claude usage exceeded the reserved token-count bound"
                    )
                if token_values[1] > self.max_tokens:
                    raise BudgetExceeded(
                        "Claude response exceeded the configured output-token ceiling"
                    )
                active_prices = self.prices or PRICES_PER_MTOK[self.model]
                if _exact_ledger_cost(ledger, active_prices) > Decimal(
                    str(self.max_cost_usd)
                ):
                    raise BudgetExceeded(
                        f"estimated spend ${ledger.cost_usd():.2f} passed the cap "
                        f"${self.max_cost_usd:.2f}"
                    )
                content = [_block_dict(b) for b in _get(response, "content", [])]
                messages.append({"role": "assistant", "content": content})
                stop = _get(response, "stop_reason")
                if stop == "refusal":
                    raise RuntimeError(
                        "the model declined the task (stop_reason refusal)"
                    )
                calls = [b for b in content if b.get("type") == "tool_use"]
                if not calls:
                    ledger.finished = stop == "end_turn"
                    ledger.summary = (
                        " ".join(
                            str(b.get("text", ""))
                            for b in content
                            if b.get("type") == "text"
                        ).strip()
                        or None
                    )
                    return ledger
                results: list[dict[str, Any]] = []
                for call in calls:
                    self._check_run_deadline(deadline)
                    if call.get("name") == "finish":
                        ledger.finished = True
                        ledger.summary = str(
                            (call.get("input") or {}).get("summary", "")
                        )
                        return ledger
                    results.append(
                        self._run_tool(call, harness, ledger, selective=selective)
                    )
                    self._check_run_deadline(deadline)
                messages.append({"role": "user", "content": results})
            raise RuntimeError(f"the agent did not finish in {self.max_turns} turns")
        finally:
            ledger.wall_seconds = time.monotonic() - ledger.started_at

    def _check_run_deadline(self, deadline: float) -> None:
        if self.preflight_required and time.monotonic() >= deadline:
            raise TimeoutError("agent run exceeded its configured deadline")

    def _reserve_claude_call(self, ledger: AgentLedger, input_tokens: int) -> None:
        prices = self.prices or PRICES_PER_MTOK.get(self.model)
        if prices is None:
            raise BudgetExceeded("reviewed pricing is unavailable; generation refused")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
            raise BudgetExceeded(
                "maximum output tokens are invalid; generation refused"
            )
        input_rate = max(
            Decimal(str(prices["input"])),
            Decimal(str(prices["cache_write"])),
            Decimal(str(prices["cache_read"])),
        )
        reserve = (
            Decimal(input_tokens) * input_rate
            + Decimal(self.max_tokens) * Decimal(str(prices["output"]))
        ) / Decimal(1_000_000)
        spent = _exact_ledger_cost(ledger, prices)
        if spent + reserve > Decimal(str(self.max_cost_usd)):
            raise BudgetExceeded(
                "worst-case request reservation exceeds remaining run budget"
            )

    def _run_tool(
        self,
        call: Mapping[str, Any],
        harness: Harness,
        ledger: AgentLedger,
        *,
        selective: bool,
    ) -> dict[str, Any]:
        name = str(call.get("name"))
        args = dict(call.get("input") or {})
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.get("id")}
        toolset = call.get("toolset_name")
        if toolset:
            result["toolset_name"] = toolset
        started = time.monotonic()
        outcome = "returned"
        try:
            if self._tool is not None:
                if name != "run":
                    raise LookupError(f"unknown tool {name!r}; use run or finish")
                result["content"] = self._tool.run(str(args.get("script", ""))).blocks()
            elif selective:
                result["content"] = self._selective(name, args, harness)
            else:
                result["content"] = self._computer(name, args, harness)
        except (GateRefused, ScopeViolation):
            # A refused save, or an action outside the task's declared scope:
            # run 1 ends here with no draft.
            outcome = "refused"
            raise
        except (StaleRef, UnsupportedAction, LookupError, ValueError) as exc:
            # Nothing in the action was performed; the model can try another.
            ledger.tool_errors += 1
            outcome = "retryable_error"
            result["content"] = [{"type": "text", "text": f"Not done: {exc}"}]
            result["is_error"] = True
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
        return result

    def _selective(
        self, name: str, args: Mapping[str, Any], harness: Harness
    ) -> list[dict[str, Any]]:
        gates_before = harness.stats.gate_checks
        if name == "observe":
            return [{"type": "text", "text": harness.observe().text}]
        if name == "screenshot":
            return [_image(harness.screenshot("requested"))]
        if name == "click":
            harness.click(str(args["ref"]))
        elif name == "type_text":
            ref = args.get("ref")
            harness.type_text(str(args["text"]), str(ref) if ref else None)
        elif name == "press_key":
            harness.press(str(args["key"]))
        else:
            raise LookupError(f"unknown tool {name!r}")
        out: list[dict[str, Any]] = [{"type": "text", "text": harness.observe().text}]
        if harness.stats.gate_checks > gates_before:
            # A write was committed: show the screen it left (verification gate).
            out.append(_image(harness.screenshot("gate")))
        return out

    def _computer(
        self, name: str, args: Mapping[str, Any], harness: Harness
    ) -> list[dict[str, Any]]:
        obs = harness.act({"action": name, **args})
        harness.stats.screenshots_every_turn += 1
        out: list[dict[str, Any]] = [_image(obs.png)]
        if obs.notes:
            out.insert(0, {"type": "text", "text": "; ".join(obs.notes)})
        return out


def _exact_ledger_cost(ledger: AgentLedger, prices: Mapping[str, float]) -> Decimal:
    return sum(
        Decimal(ledger.total(field)) * Decimal(str(prices[price_key]))
        for field, price_key in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_creation_input_tokens", "cache_write"),
            ("cache_read_input_tokens", "cache_read"),
        )
    ) / Decimal(1_000_000)
