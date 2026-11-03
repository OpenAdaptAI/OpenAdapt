"""What every agent shares: the instruction, the claim rule, the budgets.

These texts and numbers are part of the pre-registered protocol
(PROTOCOL.md); its hash covers this file. Change them and the protocol's
hash no longer matches.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

#: Prepended to the case's own instruction (model.task_text), for every agent.
PREAMBLE = """\
You are a clinic staff member using Medplum, the clinic's electronic health \
record, in a web browser. The screen is 1280x800 pixels and you are already \
signed in. Medplum stores its records on a FHIR R4 server; this web app is \
how you read and change them. Use only this app, and change nothing the task \
doesn't ask for.

When you have finished, or if you can't finish, stop acting and end your \
reply with one final line in exactly one of these two forms:
RESULT: DONE <the record's id, such as Patient/0193..., or the URL of its page>
RESULT: NOT DONE <the reason, in one line>
Write RESULT: DONE only if the record now exists exactly as the task asks \
(created by you, or already there).

The task:
"""

#: A run's limits (pre-registered). A run that reaches one ends with no claim.
STEP_CAP = int(os.environ.get("LEADERBOARD_STEP_CAP", 100))  # model calls per run
RUN_USD_CAP = float(os.environ.get("LEADERBOARD_RUN_USD_CAP", 4.00))  # model spend per run
WALL_S_CAP = int(os.environ.get("LEADERBOARD_WALL_S_CAP", 2400))  # seconds per run

_RESULT = re.compile(r"^\W*RESULT\W*:?\s*(NOT\s+DONE|DONE)\b\s*(.*)$", re.I)
_REF = re.compile(
    r"\b(Patient|AllergyIntolerance|Appointment)/([A-Za-z0-9\-\.]{1,64})"
)


def parse_claim(final_text: str) -> dict[str, Any]:
    """The claim from the agent's last reply (pre-registered rule).

    The last line that starts with ``RESULT:`` decides. ``DONE`` is a claim
    of success; ``NOT DONE``, no such line, a refusal, a crash or a limit
    reached is ``not_done``. The id is the first ``Type/id`` on that line.
    """

    lines = [ln.strip() for ln in (final_text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _RESULT.match(ln)
        if not m:
            continue
        done = m.group(1).upper() == "DONE"
        ref = _REF.search(m.group(2))
        return {
            "status": "done" if done else "not_done",
            "id": f"{ref.group(1)}/{ref.group(2)}" if ref else "",
            "note": ln[:300],
            "claim_line": True,
        }
    return {
        "status": "not_done",
        "id": "",
        "note": (lines[-1] if lines else "(no reply)")[:300],
        "claim_line": False,
    }


class RunLimit(Exception):
    """A run reached its step, dollar or time cap."""


class SpendCap(Exception):
    """The leaderboard's total spend cap would be passed: stop everything."""


@dataclass
class Meter:
    """One run's model calls and dollars, against the run's caps."""

    label: str
    calls: int = 0
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    started: float = field(default_factory=time.monotonic)
    usage: list[dict[str, Any]] = field(default_factory=list)

    def before_call(self) -> None:
        if self.calls >= STEP_CAP:
            raise RunLimit(f"step cap: {STEP_CAP} model calls")
        if self.usd >= RUN_USD_CAP:
            raise RunLimit(f"dollar cap: ${self.usd:.2f} of ${RUN_USD_CAP:.2f}")
        if time.monotonic() - self.started > WALL_S_CAP:
            raise RunLimit(f"time cap: {WALL_S_CAP} s")
        check_total(agent=self.label.split(" ", 1)[0])

    def record(self, usd: float, tokens_in: int, tokens_out: int, **extra: Any) -> None:
        self.calls += 1
        self.usd += usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usage.append(
            {"in": tokens_in, "out": tokens_out, "usd": round(usd, 6), **extra}
        )
        add_spend(self.label, usd)


# -- the total spend ledger (all agents, all runs) ----------------------------------

TOTAL_CAP_USD = float(os.environ.get("LEADERBOARD_SPEND_CAP_USD", "300"))
#: Each agent's share of the total (pre-registered); the rest is reserve.
AGENT_CAP_USD = {"claude": 180.0, "openai": 90.0, "gemini": 0.0, "fara": 0.0}
LEDGER = Path(
    os.environ.get(
        "LEADERBOARD_LEDGER", Path.home() / ".openadapt" / "leaderboard" / "spend.json"
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
            else {"total_usd": 0.0, "by_label": {}}
        )
        yield data
        LEDGER.write_text(json.dumps(data, indent=1) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


def spent() -> float:
    if not LEDGER.is_file():
        return 0.0
    return float(json.loads(LEDGER.read_text()).get("total_usd", 0.0))


def check_total(margin: float = RUN_USD_CAP / 8, agent: str = "") -> None:
    if agent in AGENT_CAP_USD and AGENT_CAP_USD[agent] > 0:
        mine = spent_by(agent)
        if mine + margin > AGENT_CAP_USD[agent]:
            raise SpendCap(f"{agent} has spent ${mine:.2f} of its ${AGENT_CAP_USD[agent]:.0f} share")
    if spent() + margin > TOTAL_CAP_USD:
        raise SpendCap(
            f"${spent():.2f} spent of the ${TOTAL_CAP_USD:.0f} cap; "
            "raise LEADERBOARD_SPEND_CAP_USD only with the coordinator's go"
        )


def spent_by(agent: str) -> float:
    if not LEDGER.is_file():
        return 0.0
    return float(json.loads(LEDGER.read_text()).get("by_label", {}).get(agent, 0.0))


def add_spend(label: str, usd: float) -> None:
    agent = label.split(" ", 1)[0]
    with _locked() as data:
        data["by_label"][agent] = round(data["by_label"].get(agent, 0.0) + usd, 6)
        data["total_usd"] = round(sum(data["by_label"].values()), 6)


def price_usd(prices: dict[str, float], tin: int, tout: int, cached: int = 0) -> float:
    """List price of one call; cached input at the provider's cached rate."""

    fresh = max(tin - cached, 0)
    return (
        fresh * prices["input"]
        + cached * prices.get("cached_input", prices["input"])
        + tout * prices["output"]
    ) / 1e6


@dataclass
class AgentRun:
    """What an adapter returns for one run."""

    final_text: str
    meter: Meter
    stopped: Optional[str] = None  # why the loop ended early, if it did
    transcript: list[dict[str, Any]] = field(default_factory=list)
