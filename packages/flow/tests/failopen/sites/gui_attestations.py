"""gui.attestations.judge (C1 C2 C5): every planned box shows the planned state
with the statement it had when it was planned.

The obligations are the plan's boxes, fixed before any tick. A box the plan
never saw is unexpected, a statement that hashes differently is refuted, an
unread state is not checked, and an empty plan never passes.
"""

from __future__ import annotations

import copy
from typing import Any

from openadapt_flow.gui.attestations import judge_attestations, statement_digest
from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, index, key, register

RUN = "run-1"


def _row(handle: str, statement: str, decision: str, want: bool) -> dict[str, Any]:
    return {
        "handle": handle,
        "decision": decision,
        "digest": statement_digest(statement),
        "want_checked": want,
    }


def _seen(handle: str, statement: str, checked: Any) -> dict[str, Any]:
    return {"handle": handle, "statement": statement, "checked": checked, "run_id": RUN}


def _good() -> dict[str, Any]:
    return {
        "plan": [
            _row("#a", "I confirm this is true", "tick", True),
            _row("#b", "Send me offers", "leave", False),
        ],
        "after": [
            _seen("#a", "I confirm this is true", True),
            _seen("#b", "Send me offers", False),
        ],
        "complete": True,
    }


def _run(ev: dict[str, Any]) -> Any:
    return judge_attestations(
        ev["plan"], ev["after"], run_id=RUN, complete=ev.get("complete")
    )


def _bad(item: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(item)
    out["checked"] = not out["checked"] if isinstance(out["checked"], bool) else False
    return out


def _inflate(ev: dict[str, Any], n: int) -> dict[str, Any]:
    plan = [_row(f"#{i}", f"s{i}", "tick", True) for i in range(n)]
    after = [_seen(f"#{i}", f"s{i}", i != n - 1) for i in range(n)]
    return {**ev, "plan": plan, "after": after}


register(
    SiteSpec(
        id="gui.attestations.judge",
        classes="C1 C2 C5",
        target="openadapt_flow.gui.attestations:judge_attestations",
        run=_run,
        is_pass=lambda out: out.status is Status.HELD,
        level=lambda out: out.status,
        seeds=lambda: [
            Seed(_good(), True),
            Seed(
                {
                    **_good(),
                    "after": [
                        _seen("#a", "I confirm this is true", False),
                        _seen("#b", "Send me offers", False),
                    ],
                },
                False,
                "the attestation is not ticked",
            ),
            Seed(
                {
                    **_good(),
                    "after": [
                        _seen("#a", "I confirm this and more", True),
                        _seen("#b", "Send me offers", False),
                    ],
                },
                False,
                "the statement changed",
            ),
            Seed(
                {
                    **_good(),
                    "plan": [
                        _row("#a", "I confirm this is true", "stop", True),
                        _row("#b", "Send me offers", "leave", False),
                    ],
                },
                False,
                "the plan stopped on a box",
            ),
            Seed(
                {
                    **_good(),
                    "after": _good()["after"] + [_seen("#c", "New terms", False)],
                },
                False,
                "a box the plan never saw",
            ),
        ],
        items=key("after"),
        plan=key("plan"),
        bad_item=_bad,
        inflate=_inflate,
        fields=index(key("after"), 0),
        values=(
            key("checked", "after", 0),
            key("statement", "after", 0),
            key("digest", "plan", 0),
            key("want_checked", "plan", 0),
            key("decision", "plan", 0),
        ),
        subject=key("run_id", "after", 0),
        partial=lambda ev: [("complete", {**ev, "complete": False})],
    )
)
