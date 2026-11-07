"""A small chat client with a hard spending cap, for the GUI agent and proposers.

Only the measured runs use it. Tests use scripted agents and proposers and
never make a paid call. The client reads its key from the environment
(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY`` or ``GOOGLE_API_KEY``) and never
logs it.

Every request passes the outbound privacy gate first
(:func:`openadapt_flow.privacy.prepare_outbound`): scrubbed, or sent under an
opt-out recorded in the local ledger. Every call is charged to a
:class:`CostLedger` at the list price the caller declares. The ledger refuses a call once the cap is reached, so a run can
overshoot the cap by at most one call's cost.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import httpx

from openadapt_flow import privacy


class BudgetExceeded(RuntimeError):
    """The ledger's cap is reached; no further paid call is made."""


class CostLedger:
    """Thread-safe running total of model spend, with a hard cap."""

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = float(cap_usd)
        self.spent_usd = 0.0
        self.calls = 0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.spent_usd >= self.cap_usd:
                raise BudgetExceeded(
                    f"spent ${self.spent_usd:.4f} of the ${self.cap_usd:.2f} cap"
                )

    def charge(self, usd: float) -> None:
        with self._lock:
            self.spent_usd += float(usd)
            self.calls += 1


@dataclass(frozen=True)
class Completion:
    text: str
    usd: float
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class ChatModel(Protocol):
    """One system prompt and one user message in, text out."""

    name: str

    def complete(
        self, system: str, user: str, *, max_tokens: int = 800
    ) -> Completion: ...


@dataclass(frozen=True)
class Price:
    """List price in USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / 1_000_000


_PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


class HttpChatModel:
    """OpenAI, Anthropic or Gemini chat over plain HTTPS, charged to a ledger."""

    def __init__(
        self,
        provider: str,
        model: str,
        price: Price,
        ledger: CostLedger,
        *,
        timeout_s: float = 90.0,
        attempts: int = 3,
    ) -> None:
        if provider not in _PROVIDER_KEYS:
            raise ValueError(f"unknown provider {provider!r}")
        self.provider = provider
        self.model = model
        self.name = f"{provider}/{model}"
        self.price = price
        self.ledger = ledger
        self.timeout_s = timeout_s
        self.attempts = attempts

    def _key(self) -> str:
        key = os.environ.get(_PROVIDER_KEYS[self.provider], "")
        if not key:
            raise RuntimeError(f"{_PROVIDER_KEYS[self.provider]} is not set")
        return key

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> Completion:
        self.ledger.check()
        # Screens and trajectories can carry patient data, so the request goes
        # through the outbound privacy gate: scrubbed, or a recorded opt-out.
        # Scrubbing names out of a GUI task makes it unsolvable (the agent must
        # match the patient), so a run on synthetic data records an opt-out.
        sendable = privacy.prepare_outbound(
            {"system": system, "user": user}, f"harness2 request to {self.name}"
        )
        assert isinstance(sendable, dict)
        system, user = str(sendable["system"]), str(sendable["user"])
        last: Optional[Exception] = None
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(2.0 * attempt)
            try:
                text, tin, tout = self._call(system, user, max_tokens)
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = exc
                continue
            usd = self.price.cost(tin, tout)
            self.ledger.charge(usd)
            return Completion(text=text, usd=usd, input_tokens=tin, output_tokens=tout)
        raise RuntimeError(
            f"{self.name}: no reply after {self.attempts} attempts: {last}"
        )

    def _call(self, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
        key = self._key()
        with httpx.Client(timeout=self.timeout_s) as client:
            if self.provider == "openai":
                r = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "max_completion_tokens": max_tokens,
                    },
                )
                r.raise_for_status()
                body = r.json()
                usage = body.get("usage") or {}
                return (
                    str(body["choices"][0]["message"]["content"] or ""),
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                )
            if self.provider == "anthropic":
                r = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={
                        "model": self.model,
                        "system": system,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": user}],
                    },
                )
                r.raise_for_status()
                body = r.json()
                usage = body.get("usage") or {}
                text = "".join(
                    b.get("text", "")
                    for b in body["content"]
                    if b.get("type") == "text"
                )
                return (
                    text,
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                )
            r = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
            )
            r.raise_for_status()
            body = r.json()
            usage = body.get("usageMetadata") or {}
            parts = body["candidates"][0].get("content", {}).get("parts", [])
            return (
                "".join(p.get("text", "") for p in parts),
                int(usage.get("promptTokenCount", 0)),
                int(usage.get("candidatesTokenCount", 0))
                + int(usage.get("thoughtsTokenCount", 0)),
            )


__all__ = [
    "BudgetExceeded",
    "ChatModel",
    "Completion",
    "CostLedger",
    "HttpChatModel",
    "Price",
]
