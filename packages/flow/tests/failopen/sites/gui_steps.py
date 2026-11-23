"""gui.steps.judge_step (C3 C5): did a step (a link, a tab, a button) do what
the demonstration showed?

A commit holds only on exactly the demonstrated number of writes, each
returning what the demonstrated one did (a 302 to a sign-in page is not the
302 to the confirmation page the demo saw, and an outcome in the old
path-only ``"3xx <path>"`` form is not checked); a
navigation only when the screen moved and nothing was written. Write counts
that are not whole numbers, and roles nobody listed, are not checked.

No M8: the evidence is gathered in-process by ``take_step`` from the live
backend in the same call, not read back from a store another run could have
written, so there is no stored binding to swap.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.gui.steps import StepEvidence, judge_step
from openadapt_flow.verdict import Status
from tests.failopen.harness import CodeCase, Lens, Seed, SiteSpec, register

#: The fields each role's verdict depends on.
RELEVANT = {
    "commit": (
        "role",
        "expected_writes",
        "writes_before",
        "writes_after",
        "after_complete",
        "expected_outcomes",
        "outcomes",
    ),
    "navigate": ("role", "writes_before", "writes_after", "moved", "after_complete"),
    "in_place": ("role", "writes_before", "writes_after", "moved", "after_complete"),
}


def _ev(role: str, **over: Any) -> dict[str, Any]:
    base = {
        "step_id": "s1",
        "role": role,
        "expected_writes": 1 if role == "commit" else 0,
        "writes_before": 3,
        "writes_after": 4 if role == "commit" else 3,
        "moved": role == "navigate",
        "after_complete": True,
        "after_errors": (),
        "expected_outcomes": ("2xx",) if role == "commit" else None,
        "outcomes": ("2xx",) if role == "commit" else None,
    }
    return {**base, **over}


def _relevant(ev: dict[str, Any]) -> tuple[str, ...]:
    return RELEVANT.get(str(ev.get("role")), ("role",))


def _fields() -> Lens:
    def get(ev: dict[str, Any]) -> dict[str, Any]:
        return {k: ev[k] for k in _relevant(ev) if k in ev}

    def put(ev: dict[str, Any], value: dict[str, Any]) -> None:
        for k in _relevant(ev):
            ev.pop(k, None)
        ev.update(value)

    return Lens(get, put)


def _value(name: str) -> Lens:
    def get(ev: dict[str, Any]) -> Any:
        if name not in _relevant(ev):
            raise KeyError(name)
        return ev[name]

    def put(ev: dict[str, Any], value: Any) -> None:
        from tests.failopen.harness import ABSENT

        if value is ABSENT:
            ev.pop(name, None)
        else:
            ev[name] = value

    return Lens(get, put)


register(
    SiteSpec(
        id="gui.steps.judge_step",
        classes="C3 C5",
        target="openadapt_flow.gui.steps:judge_step",
        run=lambda ev: judge_step(StepEvidence(**ev)),
        is_pass=lambda out: out.status is Status.HELD,
        level=lambda out: out.status,
        seeds=lambda: [
            Seed(_ev("commit"), True, "one write, as demonstrated"),
            Seed(_ev("navigate"), True, "moved, nothing written"),
            Seed(_ev("in_place"), True, "nothing written"),
            Seed(_ev("commit", writes_after=3), False, "a commit that wrote nothing"),
            Seed(_ev("commit", writes_after=5), False, "two writes for one"),
            Seed(
                _ev("commit", outcomes=("redirect /login",)),
                False,
                "a redirect to sign-in where the demo saved",
            ),
            Seed(
                _ev(
                    "commit",
                    expected_outcomes=("redirect /done",),
                    outcomes=("redirect /login",),
                ),
                False,
                "a redirect elsewhere than the demo's",
            ),
            Seed(
                _ev(
                    "commit",
                    expected_outcomes=("redirect /register?status=saved",),
                    outcomes=("redirect /register?status=session-expired",),
                ),
                False,
                "a redirect to the same path with another query",
            ),
            Seed(
                _ev(
                    "commit",
                    expected_outcomes=("redirect /records/{id}",),
                    outcomes=("redirect /records/{id}",),
                ),
                True,
                "a redirect to the record just created",
            ),
            Seed(
                _ev(
                    "commit",
                    expected_outcomes=("3xx /done",),
                    outcomes=("3xx /done",),
                ),
                False,
                "an outcome learned in the old path-only form",
            ),
            Seed(
                _ev("commit", expected_outcomes=None),
                False,
                "a spec with no recorded outcomes",
            ),
            Seed(_ev("navigate", writes_after=4), False, "a navigation that wrote"),
            Seed(_ev("navigate", moved=False), False, "did not move"),
            Seed(_ev("commit", after_errors=("Card declined",)), False, "error shown"),
            Seed(_ev("unknown"), False, "the demo could not count writes"),
            Seed(_ev("in_place", moved=True), False, "an in-place step moved"),
        ],
        fields=_fields(),
        values=tuple(
            _value(n)
            for n in (
                "role",
                "expected_writes",
                "writes_before",
                "writes_after",
                "moved",
                "after_complete",
                "expected_outcomes",
                "outcomes",
            )
        ),
        partial=lambda ev: [("after_complete", {**ev, "after_complete": False})],
        codes=lambda: [
            CodeCase(
                evidence={**_ev("commit"), "role": "submitted"},
                may_have_changed=True,
                forbidden=lambda out: False,
                label="a step role nobody listed",
                unknown=True,
            )
        ],
    )
)
