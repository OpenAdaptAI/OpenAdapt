"""What every agent shares: the instruction, the claim rule, the budgets."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

#: Put before every scenario's task, for every agent. ``{app}`` is the pack's description.
PREAMBLE = """\
You are a staff member using {app}, in a web browser. The screen is \
1280x800 pixels and you are already signed in. Use only this app, and change \
nothing the task doesn't ask for.

When you have finished, or if you can't finish, stop acting and end your \
reply with one final line in exactly one of these two forms:
RESULT: DONE <the record's id, or the URL of its page>
RESULT: NOT DONE <the reason, in one line>
Write RESULT: DONE only if the record now exists exactly as the task asks \
(created by you, or already there).
{extra}
The task:
"""


def preamble(description: str, extra: str = "") -> str:
    extra = extra.strip()
    return PREAMBLE.format(
        app=description, extra=("\n" + extra + "\n") if extra else ""
    )


#: List prices in USD per million tokens. A model not listed needs --price.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {
        "input": 2.0,
        "output": 10.0,
        "cached_input": 0.2,
        "cache_write": 2.5,
    },
    "claude-opus-5-5": {
        "input": 4.0,
        "output": 20.0,
        "cached_input": 0.2,
        "cache_write": 5.0,
    },
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cached_input": 0.2},
}


def price_for(model: str, override: Optional[str] = None) -> dict[str, float]:
    """The model's prices; refuse before any call if they are unknown."""

    if override:
        inp, out = (float(x) for x in override.split(","))
        return {
            "input": inp,
            "output": out,
            "cached_input": inp / 10,
            "cache_write": inp * 1.25,
        }
    if model not in PRICES:
        raise SystemExit(
            f"no list price for model {model!r}: pass --price <input>,<output> (USD per million tokens)"
        )
    return PRICES[model]


def call_usd(
    p: dict[str, float], tin: int, tout: int, cached: int = 0, written: int = 0
) -> float:
    fresh = max(tin - cached - written, 0)
    return (
        fresh * p["input"]
        + cached * p.get("cached_input", p["input"])
        + written * p.get("cache_write", p["input"])
        + tout * p["output"]
    ) / 1e6


class RunLimit(Exception):
    """A scenario reached its step, dollar or time cap."""


class SpendCap(Exception):
    """The test run's spend cap would be passed: stop everything."""


LEDGER = Path(
    os.environ.get(
        "OPENADAPT_TEST_LEDGER",
        str(Path.home() / ".openadapt" / "agent-test" / "spend.json"),
    )
)


@contextmanager
def _locked() -> Iterator[dict[str, Any]]:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER.with_suffix(".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = (
            json.loads(LEDGER.read_text())
            if LEDGER.is_file()
            else {"total_usd": 0.0, "runs": {}}
        )
        yield data
        LEDGER.write_text(json.dumps(data, indent=1) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class Budget:
    """The whole test run's model spend, against ``cap_usd``; also kept in a ledger file."""

    cap_usd: float
    run_id: str
    spent: float = 0.0
    #: The ledger's total across runs may not pass this (OPENADAPT_TEST_TOTAL_CAP_USD).
    total_cap_usd: Optional[float] = None

    def check(self, margin: float) -> None:
        if self.spent + margin > self.cap_usd:
            raise SpendCap(
                f"${self.spent:.2f} of this run's ${self.cap_usd:.2f} cap spent"
            )
        if self.total_cap_usd is not None:
            total = (
                json.loads(LEDGER.read_text()).get("total_usd", 0.0)
                if LEDGER.is_file()
                else 0.0
            )
            if total + margin > self.total_cap_usd:
                raise SpendCap(
                    f"${total:.2f} of the ledger's ${self.total_cap_usd:.2f} cap spent"
                )

    def add(self, usd: float) -> None:
        self.spent += usd
        with _locked() as data:
            data["runs"][self.run_id] = round(
                data["runs"].get(self.run_id, 0.0) + usd, 6
            )
            data["total_usd"] = round(sum(data["runs"].values()), 6)


@dataclass
class Meter:
    """One scenario's model calls and dollars, against the scenario's caps."""

    budget: Optional[Budget] = None
    step_cap: int = 60
    usd_cap: float = 1.50
    wall_s_cap: float = 900.0
    calls: int = 0
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    started: float = field(default_factory=time.monotonic)

    def before_call(self) -> None:
        if self.calls >= self.step_cap:
            raise RunLimit(f"step cap: {self.step_cap} model calls")
        if self.usd >= self.usd_cap:
            raise RunLimit(f"dollar cap: ${self.usd:.2f} of ${self.usd_cap:.2f}")
        if time.monotonic() - self.started > self.wall_s_cap:
            raise RunLimit(f"time cap: {self.wall_s_cap:.0f} s")
        if self.budget is not None:
            self.budget.check(margin=0.05)

    def record(self, usd: float, tokens_in: int, tokens_out: int) -> None:
        self.calls += 1
        self.usd += usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        if self.budget is not None:
            self.budget.add(usd)


@dataclass
class AgentContext:
    """What the harness hands an agent for one scenario."""

    instruction: str  # the preamble plus the task
    task_text: str  # the task alone
    values: dict[str, Any]
    screen: Any  # browser.Screen, the signed-in page
    base_url: str  # the app's front door
    cdp_url: str  # the same browser over the DevTools Protocol
    app: Any  # packs.AppPack
    scenario_id: str
    evidence: Path
    meter: Meter


@dataclass
class AgentResult:
    final_text: str
    claimed: Optional[bool] = (
        None  # an explicit claim from a report file; None: parse final_text
    )
    stopped: Optional[str] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: Optional[float] = None  # reported by an external agent
    model_calls: Optional[int] = None
