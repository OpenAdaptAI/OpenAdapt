"""What every v2 run shares: the instruction, the claim rule, the limits, the ledger.

These texts and numbers are part of the pre-registered protocol
(PROTOCOL.md); its hash covers this file.

The agents are version 1's adapters, unchanged
(``benchmark/fhir_medplum/leaderboard/agents``). Version 1's adapters put
their Medplum preamble in front of the instruction; here the runner sets
that preamble to the app's own (``preamble``) before each run, so every app
gets the same wording with only the app's description changed. For Medplum
it is version 1's text, word for word.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from benchmark.fhir_medplum.leaderboard.common import PREAMBLE as MEDPLUM_PREAMBLE
from benchmark.fhir_medplum.leaderboard.common import RunLimit

_GENERIC = """\
You are a staff member using {app}, in a web browser. The screen is \
1280x800 pixels and you are already signed in. Use only this app, and change \
nothing the task doesn't ask for.

When you have finished, or if you can't finish, stop acting and end your \
reply with one final line in exactly one of these two forms:
RESULT: DONE <the record's id, or the URL of its page>
RESULT: NOT DONE <the reason, in one line>
Write RESULT: DONE only if the record now exists exactly as the task asks \
(created by you, or already there).

The task:
"""


def preamble(app: str, description: str) -> str:
    """The text put before every task: Medplum's from version 1, the rest generic."""

    if app == "medplum":
        return MEDPLUM_PREAMBLE
    return _GENERIC.format(app=description)


# -- run limits (pre-registered) --------------------------------------------------

STEP_CAP = 80  # model calls per run
RUN_USD_CAP = 2.50  # list-price model spend per run
WALL_S_CAP = 1800  # seconds per run

# -- spend (pre-registered) -------------------------------------------------------

#: Total for everything in version 2, practice runs included.
TOTAL_CAP_USD = 150.0
#: Each provider's share. The rest (15) is held for practice and retries.
AGENT_CAP_USD = {"claude": 95.0, "openai": 40.0, "gemini": 0.0}
LEDGER = Path(
    os.environ.get(
        "LEADERBOARD_V2_LEDGER",
        Path.home() / ".openadapt" / "leaderboard-v2" / "spend.json",
    )
)


class SpendCap(Exception):
    """A share or the total would be passed: stop this agent."""


@contextmanager
def _locked() -> Iterator[dict[str, Any]]:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER.with_suffix(".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = json.loads(LEDGER.read_text()) if LEDGER.is_file() else {"total_usd": 0.0, "by_agent": {}, "by_phase": {}}
        yield data
        LEDGER.write_text(json.dumps(data, indent=1) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


def ledger() -> dict[str, Any]:
    return json.loads(LEDGER.read_text()) if LEDGER.is_file() else {"total_usd": 0.0, "by_agent": {}, "by_phase": {}}


def add_spend(agent: str, phase: str, usd: float) -> None:
    with _locked() as d:
        d["by_agent"][agent] = round(d["by_agent"].get(agent, 0.0) + usd, 6)
        key = f"{agent}:{phase}"
        d["by_phase"][key] = round(d["by_phase"].get(key, 0.0) + usd, 6)
        d["total_usd"] = round(sum(d["by_agent"].values()), 6)


def check_spend(agent: str, margin: float = RUN_USD_CAP) -> None:
    """Refuse a run that could pass the agent's share or the total."""

    d = ledger()
    mine = d["by_agent"].get(agent, 0.0)
    share = AGENT_CAP_USD.get(agent, 0.0)
    if mine + margin > share:
        raise SpendCap(f"{agent} has spent ${mine:.2f} of its ${share:.0f} share")
    if d["total_usd"] + margin > TOTAL_CAP_USD:
        raise SpendCap(f"${d['total_usd']:.2f} spent of the ${TOTAL_CAP_USD:.0f} cap")


@dataclass
class Meter:
    """One run's model calls and dollars (the adapters' meter, with v2's limits)."""

    label: str
    agent: str
    phase: str = "scored"
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
        check_spend(self.agent, margin=0.25)

    def record(self, usd: float, tokens_in: int, tokens_out: int, **extra: Any) -> None:
        self.calls += 1
        self.usd += usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usage.append({"in": tokens_in, "out": tokens_out, "usd": round(usd, 6), **extra})
        add_spend(self.agent, self.phase, usd)


# -- the claim (pre-registered; version 1's rule) ----------------------------------

_RESULT = re.compile(r"^\W*RESULT\W*:?\s*(NOT\s+DONE|DONE)\b\s*(.*)$", re.I)


def parse_claim(final_text: str) -> dict[str, Any]:
    """The last line starting ``RESULT:`` decides; anything else is not a claim of done."""

    lines = [ln.strip() for ln in (final_text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _RESULT.match(ln)
        if m:
            return {
                "status": "done" if m.group(1).upper() == "DONE" else "not_done",
                "id": m.group(2).strip()[:200],
                "note": ln[:300],
                "claim_line": True,
            }
    return {"status": "not_done", "id": "", "note": (lines[-1] if lines else "(no reply)")[:300], "claim_line": False}


# -- intervals (pre-registered) ----------------------------------------------------


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for k of n, as fractions; (0, 0) when n is 0."""

    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))
