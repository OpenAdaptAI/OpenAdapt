"""receipt_record's line judge and tally on the kernel: a differential
against the copies vendored from main before the move. ``_line`` is
unchanged on every input; the tally changes only for a line check nobody
listed, which now keeps a run from VERIFIED (C3)."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import receipt_record
from tests.failopen.selftest import _v_receipt_lines_pre as before

PROFILE = settings(derandomize=True, max_examples=1500, deadline=None)
HASHES = st.sampled_from(["h1", "h2"])
READ = st.fixed_dictionaries(
    {
        "effect_contract_hash": HASHES,
        "final_verdict": st.sampled_from(
            ["confirmed", "refuted", "indeterminate", "x"]
        ),
        "verification_tier": st.sampled_from([None, 1, 2, 3, "1", True]),
        "observed_effect": st.sampled_from(["present", "absent", None]),
    }
)


@st.composite
def _write(draw: Any) -> tuple[dict[str, Any], Any]:
    hashes = draw(st.lists(HASHES, max_size=4))
    result = {
        "effect_contract_hashes": hashes,
        "effect_evidence": draw(st.lists(READ, max_size=4)),
        "effect_verified": draw(st.sampled_from([True, False, None])),
        "postconditions_ok": draw(st.sampled_from([True, False, None])),
        "effect_field_checks": draw(st.sampled_from([[], ["f"]])),
        "actuation": draw(st.sampled_from(["api", "gui"])),
    }
    ledger = draw(
        st.one_of(
            st.none(),
            st.fixed_dictionaries(
                {
                    "effects": st.one_of(
                        st.just([{"contract_hash": h} for h in hashes]),
                        st.lists(
                            st.fixed_dictionaries({"contract_hash": HASHES}), max_size=3
                        ),
                    ),
                    "status": st.sampled_from(["confirmed", "refuted", None]),
                }
            ),
        )
    )
    return result, ledger


def _outcome(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - both must raise alike
        return type(exc).__name__


@PROFILE
@given(_write())
def test_line_is_unchanged_on_any_write(case: tuple[dict[str, Any], Any]) -> None:
    result, ledger = case
    assert _outcome(receipt_record._line, 3, result, ledger) == _outcome(
        before._line, 3, result, ledger
    )


CHECKS = st.sampled_from(["confirmed", "refuted", "not_checked"])


@PROFILE
@given(st.lists(CHECKS, max_size=6))
def test_tally_is_unchanged_on_the_three_checks(checks: list[str]) -> None:
    lines = [{"check": c} for c in checks]
    tally, settled = receipt_record._tally_lines(lines)
    assert tally == before.tally(lines)
    assert (bool(settled.total) and not settled.passes) == before.unverified(lines)


def test_a_check_nobody_listed_keeps_the_run_from_verified() -> None:
    lines = [{"check": "confirmed"}, {"check": "maybe"}]
    tally, settled = receipt_record._tally_lines(lines)
    assert not before.unverified(lines)  # the old tally dropped the line
    assert not settled.passes and tally == {
        "confirmed": 1,
        "refuted": 0,
        "not_checked": 1,
    }
