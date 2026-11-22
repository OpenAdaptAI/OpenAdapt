"""receipt_record's line judge and line tally (C1, C2, C3).

``_line`` settles a write's reads against the effects it declared: a declared
effect with no read, a read nobody declared, a read below UNCHECKED or a
verdict nobody listed keeps the line from ``confirmed``. ``_tally_lines``
counts the lines on the kernel, so a check nobody listed is counted as not
checked and keeps the run from VERIFIED. The pre-kernel copies are in
``selftest/_v_receipt_lines_pre.py``; test_receipt_lines_migration.py
compares them.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow import receipt_record
from openadapt_flow.verdict import Status
from tests.failopen.harness import CodeCase, Seed, SiteSpec, key, register, root_list

_CHECK_LEVEL = {
    "confirmed": Status.HELD,
    "refuted": Status.REFUTED,
    "not_checked": Status.NOT_CHECKED,
}


def _read(h: str, verdict: str = "confirmed") -> dict[str, Any]:
    return {
        "effect_contract_hash": h,
        "final_verdict": verdict,
        "verification_tier": 1,
        "observed_effect": "present",
    }


def _write(*reads: dict[str, Any]) -> dict[str, Any]:
    hashes = [r["effect_contract_hash"] for r in reads]
    return {
        "result": {
            "effect_contract_hashes": hashes,
            "effect_evidence": list(reads),
            "effect_verified": True,
            "postconditions_ok": True,
            "effect_field_checks": ["f"],
        },
        "ledger": {
            "effects": [{"contract_hash": h} for h in hashes],
            "status": "confirmed",
        },
    }


def _inflate(_ev: Any, n: int) -> dict[str, Any]:
    reads = [_read(f"h{i}") for i in range(n - 1)] + [_read(f"h{n - 1}", "refuted")]
    return _write(*reads)


def _evidence() -> Any:
    return key("effect_evidence", "result")


register(
    SiteSpec(
        id="receipt_record.line",
        classes="C1 C3",
        target="openadapt_flow.receipt_record:_line",
        run=lambda ev: receipt_record._line(0, ev["result"], ev["ledger"]),
        is_pass=lambda out: out["check"] == "confirmed",
        level=lambda out: _CHECK_LEVEL.get(out["check"], Status.NOT_CHECKED),
        precedence=(Status.NOT_CHECKED, Status.REFUTED),
        notes={
            "precedence": "a line is refuted only when every declared effect "
            "was read; an unread one may have landed, so it outranks a refutation"
        },
        seeds=lambda: [
            Seed(_write(_read("h1"), _read("h2")), True, "two writes, both confirmed"),
            Seed(_write(_read("h1"), _read("h1")), True, "one contract written twice"),
            Seed(_write(_read("h1"), _read("h2", "refuted")), False, "one refuted"),
            Seed(
                _write(_read("h1"), {**_read("h2"), "verification_tier": None}),
                False,
                "one read unchecked",
            ),
        ],
        items=_evidence(),
        plan=key("effect_contract_hashes", "result"),
        bad_item=lambda r: {**r, "final_verdict": "refuted"},
        inflate=_inflate,
        values=(
            key("final_verdict", "result", "effect_evidence", 0),
            key("observed_effect", "result", "effect_evidence", 0),
            key("verification_tier", "result", "effect_evidence", 0),
            key("effect_verified", "result"),
            key("status", "ledger"),
        ),
    )
)


def _lines(checks: list[str]) -> list[dict[str, Any]]:
    return [{"check": c} for c in checks]


def _keeps_verified(out: Any) -> bool:
    _tally, settled = out
    return settled.total == 0 or settled.passes


def _codes() -> list[CodeCase]:
    return [
        CodeCase(
            evidence=_lines(["confirmed", "maybe"]),
            may_have_changed=True,
            forbidden=lambda out: False,
            label="a line check nobody listed",
            unknown=True,
        )
    ]


register(
    SiteSpec(
        id="receipt_record.lines",
        classes="C2 C3",
        target="openadapt_flow.receipt_record:_tally_lines",
        run=receipt_record._tally_lines,
        is_pass=_keeps_verified,
        seeds=lambda: [
            Seed(_lines(["confirmed"]), True),
            Seed(_lines(["confirmed", "confirmed"]), True),
            Seed(_lines(["confirmed", "refuted"]), False),
            Seed(_lines(["not_checked"]), False),
        ],
        items=root_list(),
        bad_item=lambda _line: {"check": "refuted"},
        inflate=lambda _ev, n: _lines(["confirmed"] * (n - 1) + ["not_checked"]),
        values=(key("check", 0),),
        codes=_codes,
        vacuous=(
            "a run that wrote nothing has no line to check; the receipt keeps the "
            "run's own outcome and never upgrades it"
        ),
    )
)
