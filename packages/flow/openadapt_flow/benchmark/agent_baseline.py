"""Claude computer-use agent baseline for the benchmark.

A minimal but fair agent loop: it drives the SAME ``PlaywrightBackend`` the
compiled replayer uses, vision-only — screenshots go to the model; clicks,
typed text, and key presses come back. The task is stated in user-intent
terms (no coordinates, no step-by-step script), the phrasing a real user
would give a computer-use agent.

Model and API facts (checked 2026-07-08 against the Anthropic docs):

- Model: ``claude-sonnet-5`` (the current Sonnet).
- Computer-use tool ``computer_20251124`` with beta header
  ``computer-use-2025-11-24``.
- List pricing: $3.00 / MTok input, $15.00 / MTok output. Prompt-cache
  writes (5-minute TTL) bill at 1.25x input and cache reads at 0.1x input.
  An introductory $2.00 / $10.00 rate applies through 2026-08-31; costs
  here are computed at the durable list price so the numbers stay
  comparable after the promo (billed cost today is therefore BELOW every
  cap computed here — the safe direction).

Loop mechanics:

- screenshot -> model -> execute tool actions -> repeat, until the model
  stops requesting actions, the ``max_actions`` budget (default 25) is hit,
  or the run's list-price cost exceeds ``max_cost_usd`` (default $1.50 —
  a hard per-run cost cap, checked after every API call, so a run can
  overshoot the cap by at most one call's marginal cost).
- Every executed action returns a settled screenshot in its ``tool_result``
  (the same settle logic the replayer uses), so the model always sees the
  post-action state without spending an extra action on it.
- Conversation history is bounded: only the last ``keep_screenshots``
  (default 3) screenshot image blocks are kept; older ones are replaced with
  a small text stub.

Prompt caching:

- ``cache_control: {"type": "ephemeral"}`` breakpoints are placed on the
  computer-use tool definition (stable across the whole run) and on the
  last content block of the newest user message each turn; stale per-turn
  markers are stripped so the request never exceeds the 4-breakpoint limit.
- Interaction with screenshot truncation: ``_truncate_screenshots``
  rewrites the screenshot block ~``keep_screenshots`` turns back into a
  text stub each turn, mutating the prefix at that point. Cache matching
  falls back to the longest still-valid earlier prefix (the API checks
  ~20 content blocks behind each breakpoint), so everything before the
  newly stubbed block — the ever-growing stable prefix of stubs and
  assistant turns — is still served from cache; only the last few turns
  (the intact screenshots) are re-processed. Per-call usage
  (``input_tokens``, ``cache_creation_input_tokens``,
  ``cache_read_input_tokens``) is logged so the realized hit rate is
  visible in the run log.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MODEL = "claude-sonnet-5"
COMPUTER_USE_BETA = "computer-use-2025-11-24"
COMPUTER_TOOL_TYPE = "computer_20251124"
#: List price, USD per million tokens (see module docstring re: intro rate).
INPUT_USD_PER_MTOK = 3.00
OUTPUT_USD_PER_MTOK = 15.00
#: 5-minute-TTL cache writes bill at 1.25x input list price. Written as an
#: exact decimal literal (not ``INPUT * 1.25``) so serialized pricing has no
#: binary-float noise.
CACHE_WRITE_USD_PER_MTOK = 3.75
#: Cache reads bill at 0.1x input list price (exact decimal literal, same
#: reasoning as above).
CACHE_READ_USD_PER_MTOK = 0.30
#: Hard per-run cost cap at list price. Checked after every API call, so a
#: run can overshoot the cap by at most one call's marginal cost before it
#: stops.
MAX_COST_USD = 1.50
MAX_ACTIONS = 25
KEEP_SCREENSHOTS = 3
MAX_TOKENS = 4096

_SUPPORTED_ACTIONS = (
    "screenshot, left_click, double_click, triple_click, type, key, "
    "scroll, wait, mouse_move"
)
#: Pixels dispatched per computer-use ``scroll_amount`` unit (wheel click).
SCROLL_PX_PER_UNIT = 120


#: Auth/billing/credit failure fingerprints: HTTP 400-403 as standalone
#: numbers, or credit/billing wording anywhere in the error string.
_BILLING_ERROR_RE = re.compile(
    r"\b40[0-3]\b|credit|billing|authentication|permission", re.IGNORECASE
)


def _looks_like_billing_error(message: str) -> bool:
    """True when an error string looks like an auth/billing/credit failure.

    Args:
        message: An error string (``"<ExceptionType>: <message>"``).

    Returns:
        Whether the error suggests the API key cannot spend (as opposed to
        a transient network/server failure worth retrying or continuing
        past).
    """
    return bool(_BILLING_ERROR_RE.search(message))


def load_api_key() -> str:
    """Resolve the Anthropic API key.

    Reads ``ANTHROPIC_API_KEY`` from the environment, falling back to the
    file ``~/.anthropic/api_key``.

    Returns:
        The API key string.

    Raises:
        RuntimeError: If neither source provides a key.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".anthropic" / "api_key"
    if path.is_file():
        key = path.read_text().strip()
        if key:
            return key
    raise RuntimeError(
        "No Anthropic API key: set ANTHROPIC_API_KEY or write the key to "
        "~/.anthropic/api_key"
    )


def triage_task_prompt(note_text: str) -> str:
    """Build the natural-language task prompt for the MockMed triage task.

    The prompt describes the same task the demo recording performs, written
    from user intent — what a real user would tell an agent — not as
    coordinates or a click script.

    Args:
        note_text: The note the agent must enter in the encounter.

    Returns:
        The task prompt string.
    """
    return (
        "You are looking at MockMed, a demo clinic web app (fake data "
        "only). Complete this task:\n\n"
        '1. Sign in with username "nurse.demo" and password '
        '"mockmed-demo-pass".\n'
        "2. Open the first referral task in the list.\n"
        "3. From the patient's page, create a New Encounter and choose the "
        'type "Triage".\n'
        f'4. Enter exactly this note in the Note field: "{note_text}"\n'
        "5. Save the encounter.\n\n"
        "You are done when you are back on the patient's page and see the "
        "'Encounter saved' confirmation. Then stop and reply with a one-line "
        "summary. Start by taking a screenshot to see the current state."
    )


def openemr_task_prompt(note_text: str) -> str:
    """Build the natural-language task prompt for the OpenEMR demo task.

    Same intent-level phrasing rules as :func:`triage_task_prompt`: what a
    real user would tell an agent — credentials as the user would state
    them, the target patient, the exact note text — no coordinates and no
    click script.

    Args:
        note_text: The note the agent must add to the patient's messages.

    Returns:
        The task prompt string.
    """
    return (
        "You are looking at the OpenEMR public demo (a real EMR web app "
        "with fake demo patients only). Complete this task:\n\n"
        '1. Sign in with username "admin" and password "pass".\n'
        "2. Use the patient search box in the top bar to search for "
        '"Phil" and open the chart of the patient "Belford, Phil".\n'
        "3. On the patient's dashboard, open the Patient Messages section "
        "(the Messages card — you will likely need to scroll down to "
        "find it).\n"
        "4. Add a new note and enter exactly this text as the note: "
        f'"{note_text}"\n'
        "5. Save it as a new message.\n\n"
        "You are done when you are back on the patient-message list and "
        "can see the new note. Then stop and reply with a one-line "
        "summary. The app is slow and heavily framed; wait for pages to "
        "load after navigation. Start by taking a screenshot to see the "
        "current state."
    )


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Cost in USD for API usage at list pricing, all four buckets.

    Args:
        input_tokens: Uncached input tokens across all API calls.
        output_tokens: Output tokens across all API calls.
        cache_creation_input_tokens: Tokens written to the prompt cache
            (billed at 1.25x input list price for the 5-minute TTL).
        cache_read_input_tokens: Tokens served from the prompt cache
            (billed at 0.1x input list price).

    Returns:
        The cost in USD.
    """
    return (
        input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
        + cache_creation_input_tokens / 1_000_000 * CACHE_WRITE_USD_PER_MTOK
        + cache_read_input_tokens / 1_000_000 * CACHE_READ_USD_PER_MTOK
    )


def preflight_check(
    client: Any = None,
    model: str = MODEL,
    *,
    retry_wait_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str | None]:
    """Make one minimal API call to verify the key has usable credit.

    A one-word prompt with ``max_tokens=1`` — the cheapest possible probe
    (fractions of a cent). Used by the benchmark orchestrator before
    starting any paid runs, so a dead key skips the agent arm cleanly
    instead of burning pace time on doomed runs.

    A failure that does NOT look like an auth/billing error (a transient
    429/5xx or network blip) is retried once after ``retry_wait_s`` before
    the key is declared dead; auth/billing-looking failures are not
    retried (a second identical call cannot succeed).

    Args:
        client: Anthropic client; when None, one is constructed with the
            key from ``ANTHROPIC_API_KEY`` or ``~/.anthropic/api_key``.
        model: Model ID to probe.
        retry_wait_s: Pause before the single transient-error retry.
        sleep: Sleep function (injectable for tests).

    Returns:
        ``(True, None)`` when a call succeeds, else
        ``(False, "<ExceptionType>: <message>")`` with the last error.
    """
    try:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=load_api_key())
    except Exception as exc:  # noqa: BLE001 - any failure means "don't spend"
        return False, f"{type(exc).__name__}: {exc}"
    error: str | None = None
    for attempt in range(2):
        try:
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "don't spend"
            error = f"{type(exc).__name__}: {exc}"
            if attempt == 0 and not _looks_like_billing_error(error):
                sleep(retry_wait_s)
                continue
            return False, error
        return True, None
    return False, error


@dataclass
class UsageLedger:
    """Running API usage totals, updated after every paid call.

    :func:`run_agent` records into this object the moment each API response
    arrives, BEFORE any further work that could fail. A caller that passes
    its own ledger therefore still holds the run's paid usage when the loop
    dies mid-run (an API error after N paid calls, a screenshot failure,
    ...) — crashed runs' real spend must reach the recorded row and any
    arm-level cost ceiling, never be dropped with the exception.

    Attributes:
        api_calls: Messages API calls whose usage was recorded.
        input_tokens: Total uncached input tokens.
        output_tokens: Total output tokens.
        cache_creation_input_tokens: Total prompt-cache write tokens.
        cache_read_input_tokens: Total prompt-cache read tokens.
    """

    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        """Cost of the recorded usage at list pricing (all four buckets)."""
        return compute_cost(
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )

    def record(self, usage: Any) -> None:
        """Accumulate one API response's ``usage`` block.

        Args:
            usage: The ``usage`` object of a Messages API response.
        """
        self.api_calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        self.cache_read_input_tokens += (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )


@dataclass
class AgentRunResult:
    """Outcome of one agent run (success is judged separately by verify).

    Attributes:
        actions: Number of computer actions executed.
        api_calls: Number of Messages API calls made.
        input_tokens: Total uncached input tokens (from API usage fields).
        output_tokens: Total output tokens (from API usage fields).
        cache_creation_input_tokens: Total tokens written to the prompt
            cache across all API calls.
        cache_read_input_tokens: Total tokens served from the prompt cache
            across all API calls.
        cost_usd: Cost at list pricing, all four buckets (see
            :func:`compute_cost`).
        wall_s: Wall-clock seconds for the whole loop.
        stopped: Why the loop ended: ``"model_done"`` (the model stopped
            requesting actions), ``"budget_exhausted"`` (action budget),
            or ``"cost_cap"`` (the run's list-price cost exceeded
            ``max_cost_usd``).
        model_stop_reason: The final API response's ``stop_reason``.
        final_screenshot: PNG bytes of the final state (for verification).
        action_log: One short line per executed action (for debugging).
    """

    actions: int
    api_calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float
    wall_s: float
    stopped: str
    model_stop_reason: str | None
    final_screenshot: bytes
    action_log: list[str] = field(default_factory=list)


def _capture(backend: Any) -> bytes:
    """Settled screenshot — the same settle logic the replayer uses."""
    from openadapt_flow.vision import wait_settled

    return wait_settled(backend, timeout_s=2.0)


def _screenshot_block(png: bytes) -> dict[str, Any]:
    """Wrap PNG bytes as a base64 image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }


def _execute_action(backend: Any, block: Any) -> dict[str, Any]:
    """Execute one computer-use ``tool_use`` block against the backend.

    Args:
        backend: A ``Backend`` (screenshot/click/type_text/press).
        block: The ``tool_use`` content block from the API response.

    Returns:
        A ``tool_result`` dict. Successful actions carry a short text ack
        plus a settled screenshot; unsupported or failing actions carry
        ``is_error: true`` with an explanation.
    """
    inp = dict(block.input) if block.input else {}
    action = inp.get("action")

    def error(message: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": message,
            "is_error": True,
        }

    try:
        if action == "screenshot":
            pass  # screenshot is attached below for every action
        elif action in ("left_click", "double_click"):
            x, y = inp["coordinate"]
            backend.click(int(x), int(y), double=action == "double_click")
        elif action == "triple_click":
            x, y = inp["coordinate"]
            for _ in range(3):
                backend.click(int(x), int(y))
        elif action == "type":
            backend.type_text(str(inp["text"]))
        elif action == "key":
            backend.press(str(inp["text"]))
        elif action == "scroll":
            # The wheel dispatches at the current pointer position (the
            # coordinate field is accepted but not used to move the
            # pointer, matching mouse_move semantics on this backend).
            amount = int(inp.get("scroll_amount", 3)) * SCROLL_PX_PER_UNIT
            direction = str(inp.get("scroll_direction", "down"))
            dx, dy = {
                "down": (0, amount),
                "up": (0, -amount),
                "right": (amount, 0),
                "left": (-amount, 0),
            }.get(direction, (0, amount))
            backend.scroll(dx, dy)
        elif action == "wait":
            time.sleep(min(float(inp.get("duration", 1.0)), 2.0))
        elif action == "mouse_move":
            pass  # cursor position has no effect on this backend
        else:
            return error(
                f"Action {action!r} is not supported in this environment. "
                f"Supported actions: {_SUPPORTED_ACTIONS}."
            )
    except Exception as exc:  # noqa: BLE001 - report to the model, not crash
        return error(f"Action {action!r} failed: {exc}")

    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": [
            {"type": "text", "text": f"Executed {action}."},
            _screenshot_block(_capture(backend)),
        ],
    }


def _truncate_screenshots(messages: list[dict[str, Any]], keep: int) -> None:
    """Replace all but the last ``keep`` screenshot blocks with text stubs.

    Walks the ``tool_result`` blocks of user messages (the only place this
    agent puts images) in order and rewrites every image block except the
    final ``keep`` of them. Assistant messages are never touched.

    Args:
        messages: The conversation history (mutated in place).
        keep: Number of most-recent screenshot image blocks to keep.
    """
    slots: list[tuple[list[Any], int]] = []
    for msg in messages:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            content = block.get("content")
            if not isinstance(content, list):
                continue
            for i, item in enumerate(content):
                if isinstance(item, dict) and item.get("type") == "image":
                    slots.append((content, i))
    excess = slots[:-keep] if keep > 0 else slots
    for content, i in excess:
        content[i] = {
            "type": "text",
            "text": "[screenshot removed to bound context]",
        }


def _apply_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Strip stale per-turn cache markers and mark the newest user message.

    Called immediately before every API call. Removes ``cache_control``
    from every dict content block in the history (assistant messages carry
    SDK block objects, never dicts we added markers to, so they are left
    alone), then places a single ephemeral marker on the last content
    block of the last message. Together with the marker on the tool
    definition this keeps the request at 2 of the allowed 4 breakpoints,
    with the per-turn marker always at the newest position.

    Args:
        messages: The conversation history (mutated in place). The last
            entry must be a user message; string content is converted to
            a text block so it can carry the marker.
    """
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    last["content"][-1]["cache_control"] = {"type": "ephemeral"}


def run_agent(
    backend: Any,
    task: str,
    *,
    client: Any = None,
    model: str = MODEL,
    max_actions: int = MAX_ACTIONS,
    keep_screenshots: int = KEEP_SCREENSHOTS,
    max_tokens: int = MAX_TOKENS,
    max_cost_usd: float = MAX_COST_USD,
    log: Callable[[str], None] | None = None,
    ledger: UsageLedger | None = None,
) -> AgentRunResult:
    """Run the computer-use agent loop against a backend until done.

    Args:
        backend: A ``Backend`` (the same PlaywrightBackend the replayer
            uses) already navigated to the target app.
        task: Natural-language task prompt (see :func:`triage_task_prompt`).
        client: Anthropic client; when None, one is constructed with the key
            from ``ANTHROPIC_API_KEY`` or ``~/.anthropic/api_key``.
        model: Model ID.
        max_actions: Hard budget on executed computer actions.
        keep_screenshots: How many recent screenshots to keep in history.
        max_tokens: Per-response output token cap.
        max_cost_usd: Hard per-run cost cap at list price. Checked after
            every API call — so a run can overshoot the cap by at most one
            call's marginal cost; when exceeded, the loop stops with
            ``stopped="cost_cap"`` and returns normally (a capped run is
            a recorded data point, not an exception).
        log: Per-API-call usage logger (cache hit rate visibility); None
            disables per-call logging.
        ledger: Optional :class:`UsageLedger` to record usage into. Pass
            one to keep the run's paid usage observable even when this
            function raises mid-run (see :class:`UsageLedger`); when None
            a private ledger is used.

    Returns:
        An :class:`AgentRunResult` with counters, cost, and the final
        screenshot. Task success is judged separately (``verify``).
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=load_api_key())

    width, height = backend.viewport
    tools = [
        {
            "type": COMPUTER_TOOL_TYPE,
            "name": "computer",
            "display_width_px": width,
            "display_height_px": height,
            # Stable for the whole run: caches the tools portion of the
            # prefix once, then reads it on every subsequent call.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]

    if ledger is None:
        ledger = UsageLedger()
    actions = 0
    stopped = "model_done"
    model_stop_reason: str | None = None
    action_log: list[str] = []
    start = time.monotonic()

    while True:
        _apply_cache_breakpoint(messages)
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            betas=[COMPUTER_USE_BETA],
            messages=messages,
        )
        # Recorded FIRST, before anything below can fail: a mid-run crash
        # must never lose paid usage (the caller's ledger keeps it).
        usage = response.usage
        ledger.record(usage)
        cost_usd = ledger.cost_usd
        model_stop_reason = response.stop_reason
        if log is not None:
            call_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            call_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            log(
                f"  api call {ledger.api_calls}: in={usage.input_tokens} "
                f"cache_write={call_write} cache_read={call_read} "
                f"out={usage.output_tokens} run_cost=${cost_usd:.4f}"
            )

        if cost_usd > max_cost_usd:
            stopped = "cost_cap"
            if log is not None:
                log(
                    f"  cost cap tripped: ${cost_usd:.4f} > "
                    f"${max_cost_usd:.2f} after {ledger.api_calls} calls"
                )
            break

        tool_uses = [
            b for b in response.content if getattr(b, "type", None) == "tool_use"
        ]
        if response.stop_reason != "tool_use" or not tool_uses:
            break

        messages.append({"role": "assistant", "content": response.content})
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            if actions >= max_actions:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Action budget exhausted.",
                        "is_error": True,
                    }
                )
                continue
            actions += 1
            action_log.append(f"{actions}: {dict(block.input) if block.input else {}}")
            results.append(_execute_action(backend, block))
        messages.append({"role": "user", "content": results})
        _truncate_screenshots(messages, keep_screenshots)

        if actions >= max_actions:
            stopped = "budget_exhausted"
            break

    wall_s = time.monotonic() - start
    return AgentRunResult(
        actions=actions,
        api_calls=ledger.api_calls,
        input_tokens=ledger.input_tokens,
        output_tokens=ledger.output_tokens,
        cache_creation_input_tokens=ledger.cache_creation_input_tokens,
        cache_read_input_tokens=ledger.cache_read_input_tokens,
        cost_usd=ledger.cost_usd,
        wall_s=wall_s,
        stopped=stopped,
        model_stop_reason=model_stop_reason,
        final_screenshot=_capture(backend),
        action_log=action_log,
    )
