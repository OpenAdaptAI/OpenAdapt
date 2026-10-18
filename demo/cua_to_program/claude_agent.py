"""A Claude computer-use agent loop that the JIT wrapper records.

The loop calls the Claude API with the computer use toolset
(``computer_toolset_20260801``) and sends every action through
``CaptureComputer.execute``, so Flow records the agent's run as a
demonstration. It keeps a ledger of every model call: wall time, token usage
and list-price cost. Nothing here estimates; every number comes from the API
response or the clock.

The API key is read from the environment (``ANTHROPIC_API_KEY``) by the SDK
and is never written anywhere.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

MODEL = "claude-opus-5-5"
EFFORT = "medium"

# List prices for claude-opus-5-5, USD per million tokens (Anthropic API,
# checked 2026-10-18). Cache writes are 1.25x input for the 5-minute TTL.
PRICE_PER_MTOK = {
    "input": 4.00,
    "output": 20.00,
    "cache_read": 0.20,
    "cache_write": 5.00,
}

# Members the recording format can't hold, and zoom (a zoom image would need
# its own capture). Turning them off keeps the model from asking for them.
DISABLED_MEMBERS = (
    "zoom",
    "triple_click",
    "middle_click",
    "left_mouse_down",
    "left_mouse_up",
    "hold_key",
)

SYSTEM = """\
You operate a web browser that shows one page of a health-insurance claims \
system (openIMIS). There is no address bar and no other application. The \
screen is 1280x800 pixels. Work only on the page you are shown. Do not \
navigate away, open menus you do not need, or change any field the task does \
not mention. All data is synthetic test data.

Click a field before you type into it. To scroll, scroll at the point you \
last clicked. When the claim is saved, stop and reply with one short \
sentence; do not take further actions."""


class BudgetExceeded(RuntimeError):
    """The run reached its spend or step cap."""


@dataclass
class CallRecord:
    turn: int
    started_s: float
    ended_s: float
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float
    actions: list[dict[str, Any]]
    request_id: Optional[str]
    stop_reason: Optional[str]


def call_cost(usage: Any) -> float:
    def n(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    return (
        n("input_tokens") * PRICE_PER_MTOK["input"]
        + n("output_tokens") * PRICE_PER_MTOK["output"]
        + n("cache_read_input_tokens") * PRICE_PER_MTOK["cache_read"]
        + n("cache_creation_input_tokens") * PRICE_PER_MTOK["cache_write"]
    ) / 1_000_000


@dataclass
class ClaudeComputerUseAgent:
    """A CUA loop for ``compile_agent``: dialect ``anthropic``.

    Toolset calls arrive as ``tool_use`` blocks named for the member
    (``left_click``) with the member's parameters as ``input``. The JIT
    ``anthropic`` dialect takes the earlier ``computer`` tool's input shape,
    ``{"action": "left_click", "coordinate": [x, y]}``, which carries the same
    parameters; ``to_native`` builds it.
    """

    ledger_path: Path
    frames_dir: Optional[Path] = None
    max_turns: int = 40
    max_cost_usd: float = 4.00
    model: str = MODEL
    effort: str = EFFORT
    dialect: str = "anthropic"
    principal_id: str = f"agent:anthropic:{MODEL}"
    calls: list[CallRecord] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    final_text: str = ""
    refused_actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @staticmethod
    def to_native(name: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "key" and tool_input.get("repeat"):
            repeat = max(1, min(int(tool_input["repeat"]), 100))
            text = " ".join([str(tool_input.get("text", ""))] * repeat)
            return [{"action": "key", "text": text}]
        native = {"action": name, **{k: v for k, v in tool_input.items()}}
        return [native]

    def run(self, request: Any, computer: Any) -> None:
        import anthropic
        from openadapt_flow.jit.adapters import UnsupportedAction

        key = request.model_key.secret() if request.model_key is not None else None
        client = anthropic.Anthropic(api_key=key, max_retries=4)
        tools = [
            {
                "type": "computer_toolset_20260801",
                "configs": {m: {"enabled": False} for m in DISABLED_MEMBERS},
            }
        ]
        first = computer.execute({"action": "screenshot"})
        self._save_frame(0, first.png)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": request.instruction},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": first.png_base64(),
                        },
                    },
                ],
            }
        ]
        self.started_at = time.time()
        frame_no = 1
        for turn in range(1, self.max_turns + 1):
            if self.cost_usd >= self.max_cost_usd:
                raise BudgetExceeded(f"spend cap ${self.max_cost_usd:.2f} reached")
            t0 = time.time()
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            t1 = time.time()
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            record = CallRecord(
                turn=turn,
                started_s=t0,
                ended_s=t1,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=(
                    response.usage.cache_creation_input_tokens or 0
                ),
                cost_usd=call_cost(response.usage),
                actions=[{"name": b.name, "input": b.input} for b in calls],
                request_id=getattr(response, "_request_id", None),
                stop_reason=response.stop_reason,
            )
            self.calls.append(record)
            self._write_ledger()
            if response.stop_reason == "refusal":
                raise RuntimeError("the model declined the request")
            if not calls:
                self.final_text = " ".join(
                    b.text for b in response.content if b.type == "text"
                )
                self.finished_at = time.time()
                self._write_ledger()
                return
            results: list[dict[str, Any]] = []
            failed = False
            for block in calls:
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "toolset_name": "computer",
                }
                if failed:
                    result["content"] = (
                        "Not executed: an earlier computer action in this turn failed."
                    )
                    result["is_error"] = True
                    results.append(result)
                    continue
                try:
                    obs = None
                    for native in self.to_native(block.name, dict(block.input)):
                        obs = computer.execute(native)
                    assert obs is not None
                except UnsupportedAction as exc:
                    # Refused before anything was performed: the recording is
                    # unchanged. Tell the model and let it choose another way.
                    self.refused_actions.append(
                        {"turn": turn, "name": block.name, "reason": str(exc)}
                    )
                    result["content"] = f"Error: {exc}"
                    result["is_error"] = True
                    failed = True
                    results.append(result)
                    continue
                self._save_frame(frame_no, obs.png)
                frame_no += 1
                if block.name == "screenshot" or block is calls[-1]:
                    content: list[dict[str, Any]] = []
                    if obs.notes:
                        content.append({"type": "text", "text": "; ".join(obs.notes)})
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": obs.png_base64(),
                            },
                        }
                    )
                    result["content"] = content
                else:
                    result["content"] = [{"type": "text", "text": "OK"}]
                results.append(result)
            messages.append({"role": "user", "content": results})
        raise BudgetExceeded(f"no finish within {self.max_turns} model calls")

    def _save_frame(self, n: int, png: bytes) -> None:
        if self.frames_dir is None:
            return
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        (self.frames_dir / f"{n:04d}_{time.time():.3f}.png").write_bytes(png)

    def _write_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "model": self.model,
            "effort": self.effort,
            "tool": "computer_toolset_20260801",
            "price_per_mtok_usd": PRICE_PER_MTOK,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model_calls": len(self.calls),
            "actions": sum(len(c.actions) for c in self.calls),
            "cost_usd": round(self.cost_usd, 6),
            "final_text": self.final_text,
            "refused_actions": self.refused_actions,
            "calls": [c.__dict__ for c in self.calls],
        }
        self.ledger_path.write_text(json.dumps(data, indent=2) + "\n")
