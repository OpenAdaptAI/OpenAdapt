"""Model prices, usage accounting and the shared spend cap for the comparison arms.

- Prices come only from ``prices.json`` (dated). Nothing here hard-codes a price.
- Every model call made by any arm is added to one spend ledger
  (``benchmark/lifecycle/results/spend.json``) under a file lock, so the cap
  holds across arms and across resumed runs.
- Before a call, an arm asks :func:`reserve` for the most that call could cost.
  If that could pass the cap, :class:`SpendCapReached` is raised and nothing is
  sent. The cap is $30 unless ``LIFECYCLE_SPEND_CAP_USD`` raises it.
- The API key is read from the environment (``ANTHROPIC_API_KEY``) or, when
  ``LIFECYCLE_KEYCHAIN_SERVICE`` names one, from the macOS keychain. It is held
  in this process only and never printed, logged or written.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

HERE = Path(__file__).resolve().parent
PRICES_PATH = HERE / "prices.json"
RESULTS = HERE.parent / "results"
SPEND_PATH = Path(os.environ.get("LIFECYCLE_SPEND_LEDGER", str(RESULTS / "spend.json")))
DEFAULT_CAP_USD = 30.0


class SpendCapReached(BaseException):
    """The next model call could take total spend past the cap.

    A BaseException on purpose: no `except Exception` in an arm or a harness
    can swallow it, so reaching the cap always stops the run (resume later).
    """


class ItemBudgetExceeded(RuntimeError):
    """One item (or one phase) used up its own fixed budget."""


def prices() -> dict[str, Any]:
    return json.loads(PRICES_PATH.read_text())


def price(model: str) -> dict[str, float]:
    table = prices()["models"]
    if model not in table:
        raise KeyError(f"{model} is not in {PRICES_PATH.name} (as of {prices()['as_of']})")
    return table[model]


def cap_usd() -> float:
    return float(os.environ.get("LIFECYCLE_SPEND_CAP_USD", DEFAULT_CAP_USD))


@dataclass
class Usage:
    """Token usage of one model on one item or phase, priced from the table."""

    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    @property
    def usd(self) -> float:
        p = price(self.model)
        return (
            self.input * p["input"]
            + self.output * p["output"]
            + self.cache_read * p["cache_read"]
            + self.cache_write * p["cache_write"]
        ) / 1_000_000

    def add_response(self, usage: Any) -> float:
        """Add one API response's ``usage``; returns that call's cost."""

        def n(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        one = Usage(
            self.model,
            input=n("input_tokens"),
            output=n("output_tokens"),
            cache_read=n("cache_read_input_tokens"),
            cache_write=n("cache_creation_input_tokens"),
            calls=1,
        )
        self.input += one.input
        self.output += one.output
        self.cache_read += one.cache_read
        self.cache_write += one.cache_write
        self.calls += 1
        return one.usd

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "usd": round(self.usd, 6)}


@dataclass
class Meter:
    """Usage for one item or phase, with that unit's own fixed budget."""

    label: str
    budget_usd: float
    usage: dict[str, Usage] = field(default_factory=dict)

    def of(self, model: str) -> Usage:
        return self.usage.setdefault(model, Usage(model))

    @property
    def usd(self) -> float:
        return sum(u.usd for u in self.usage.values())

    @property
    def calls(self) -> int:
        return sum(u.calls for u in self.usage.values())

    def before_call(self, model: str, max_call_usd: float) -> None:
        """Refuse a call that could pass this unit's budget or the global cap."""

        if self.usd + max_call_usd > self.budget_usd:
            raise ItemBudgetExceeded(
                f"{self.label}: ${self.usd:.3f} used of ${self.budget_usd:.2f}; "
                f"the next call could cost ${max_call_usd:.3f}"
            )
        check_cap(max_call_usd)

    def record(self, model: str, response_usage: Any) -> float:
        usd = self.of(model).add_response(response_usage)
        add_spend(self.label, model, usd)
        return usd

    def to_list(self) -> list[dict[str, Any]]:
        return [u.to_dict() for u in self.usage.values()]


def max_call_usd(model: str, input_tokens: int, max_tokens: int) -> float:
    """An upper bound for one call: every input token uncached, full output."""

    p = price(model)
    return (input_tokens * p["cache_write"] + max_tokens * p["output"]) / 1_000_000


@contextmanager
def _locked() -> Iterator[dict[str, Any]]:
    SPEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = SPEND_PATH.with_suffix(".lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = (
            json.loads(SPEND_PATH.read_text())
            if SPEND_PATH.is_file()
            else {"total_usd": 0.0, "entries": []}
        )
        yield data
        fcntl.flock(fh, fcntl.LOCK_UN)


def spent() -> float:
    if not SPEND_PATH.is_file():
        return 0.0
    return float(json.loads(SPEND_PATH.read_text()).get("total_usd", 0.0))


def check_cap(next_call_usd: float) -> None:
    total, cap = spent(), cap_usd()
    if total + next_call_usd > cap:
        raise SpendCapReached(
            f"${total:.2f} spent; a call of up to ${next_call_usd:.2f} could pass the "
            f"${cap:.2f} cap (raise LIFECYCLE_SPEND_CAP_USD only with the coordinator's go)"
        )


def add_spend(label: str, model: str, usd: float) -> float:
    with _locked() as data:
        data["entries"].append(
            {"label": label, "model": model, "usd": round(usd, 6), "at": round(time.time(), 3)}
        )
        data["total_usd"] = round(sum(e["usd"] for e in data["entries"]), 6)
        data["cap_usd"] = cap_usd()
        data["prices_as_of"] = prices()["as_of"]
        SPEND_PATH.write_text(json.dumps(data, indent=1) + "\n")
        return data["total_usd"]


def ensure_key() -> None:
    """Make the key available to the SDK without ever printing it."""

    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    service = os.environ.get("LIFECYCLE_KEYCHAIN_SERVICE")
    if service:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            os.environ["ANTHROPIC_API_KEY"] = out.stdout.strip()
            return
        raise SystemExit(f"keychain service {service!r} has no readable password")
    raise SystemExit(
        "no Anthropic key: set ANTHROPIC_API_KEY, or LIFECYCLE_KEYCHAIN_SERVICE to a "
        "keychain item that holds it"
    )


def client() -> Any:
    ensure_key()
    import anthropic

    return anthropic.Anthropic(max_retries=4)


def compute_usd(wall_s: float) -> float:
    """Nominal local compute for ``wall_s`` seconds of one worker."""

    return prices()["compute"]["host_usd_per_hour"] * wall_s / 3600.0


def summarize(meter: Optional[Meter]) -> dict[str, Any]:
    if meter is None:
        return {"model_calls": 0, "cost_usd": 0.0, "usage": []}
    return {"model_calls": meter.calls, "cost_usd": round(meter.usd, 6), "usage": meter.to_list()}
