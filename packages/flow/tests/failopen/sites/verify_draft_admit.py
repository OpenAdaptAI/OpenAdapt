"""verify_draft.judge_case (C3 C5): admission cases settle on the verdict kernel.

An untestable case, an empty/error/no-baseline outcome, or an unlisted
outcome or expectation code must settle as NOT_CHECKED and never pass.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.verdict import Status
from openadapt_flow.verify_draft.admit import Case, judge_case
from tests.failopen.harness import CodeCase, Seed, SiteSpec, register


def _run(ev: dict[str, Any]) -> Any:
    case = Case(
        group=ev.get("group", "clean"),
        name=ev.get("name", "c1"),
        detail="d",
        expect=ev["expect"],
        outcome=ev["outcome"],
        recheck=ev.get("recheck", ""),
        applicable=ev.get("applicable", True),
        untestable=ev.get("untestable", False),
    )
    return judge_case(case)


def _level(out: Any) -> Status:
    if out.passes:
        return Status.HELD
    if out.refuted:
        return Status.REFUTED
    return Status.NOT_CHECKED


def _codes() -> list[CodeCase]:
    return [
        CodeCase(
            evidence={"expect": "confirmed", "outcome": "MYSTERY_CODE", "recheck": ""},
            may_have_changed=True,
            forbidden=lambda out: out.passes,
            label="unknown outcome on confirmed case",
            unknown=True,
        ),
        CodeCase(
            evidence={
                "expect": "confirmed",
                "outcome": "CONFIRMED",
                "recheck": "MYSTERY_RECHECK",
            },
            may_have_changed=True,
            forbidden=lambda out: out.passes,
            label="unknown recheck on confirmed case",
            unknown=True,
        ),
        CodeCase(
            evidence={"expect": "refuted", "outcome": "MYSTERY_CODE", "recheck": ""},
            may_have_changed=True,
            forbidden=lambda out: out.passes,
            label="unknown outcome on refuted case",
            unknown=True,
        ),
        CodeCase(
            evidence={
                "expect": "unknown_expect",
                "outcome": "CONFIRMED",
                "recheck": "",
            },
            may_have_changed=True,
            forbidden=lambda out: out.passes,
            label="unknown expect mode",
            unknown=True,
        ),
        CodeCase(
            evidence={
                "expect": "confirmed",
                "outcome": "CONFIRMED",
                "recheck": "CONFIRMED",
                "untestable": True,
            },
            may_have_changed=True,
            forbidden=lambda out: out.passes,
            label="untestable case never holds",
        ),
    ]


register(
    SiteSpec(
        id="verify_draft.judge_case",
        classes="C3 C5",
        target="openadapt_flow.verify_draft.admit:judge_case",
        run=_run,
        is_pass=lambda out: out.passes,
        level=_level,
        seeds=lambda: [
            Seed(
                {"expect": "confirmed", "outcome": "CONFIRMED", "recheck": "CONFIRMED"},
                True,
            ),
            Seed(
                {"expect": "refuted", "outcome": "REFUTED", "recheck": ""},
                True,
            ),
            Seed(
                {"expect": "refuted", "outcome": "CONFIRMED", "recheck": "REFUTED"},
                True,
            ),
            Seed(
                {"expect": "confirmed", "outcome": "REFUTED", "recheck": ""},
                False,
            ),
            Seed(
                {"expect": "refuted", "outcome": "CONFIRMED", "recheck": "NONE"},
                False,
            ),
            Seed(
                {"expect": "refuted", "outcome": "ERROR", "recheck": ""},
                False,
            ),
        ],
        codes=_codes,
    )
)
