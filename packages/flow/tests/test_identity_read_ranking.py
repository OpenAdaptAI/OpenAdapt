"""Which identity read wins when the replayer re-reads the identifier.

A misread of a look-alike record (a sibling MRN, a different date of birth)
must never be overruled by a later read of the same screen that happens to
match the target: that is how a wrong-patient click gets through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.runtime.replayer import (
    better_identity_read,
    identity_read_contradicts,
)


def _read(status: str, coverage: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(status=status, coverage=coverage)


@pytest.mark.parametrize("retry", ["verified", "abstain", "unreadable", "mismatch"])
def test_a_first_read_of_a_different_record_is_final(retry: str) -> None:
    first = _read("mismatch")
    assert better_identity_read(first, _read(retry)) is first


@pytest.mark.parametrize("first", ["abstain", "unreadable"])
def test_a_retry_that_reads_a_different_record_wins(first: str) -> None:
    retry = _read("mismatch")
    assert better_identity_read(_read(first), retry) is retry


def test_a_retry_can_still_rescue_an_unsure_first_read() -> None:
    retry = _read("verified")
    assert better_identity_read(_read("abstain"), retry) is retry
    assert better_identity_read(_read("unreadable"), retry) is retry


def test_ties_go_to_the_read_that_covered_more() -> None:
    first, retry = _read("abstain", 0.4), _read("abstain", 0.9)
    assert better_identity_read(first, retry) is retry
    assert better_identity_read(retry, first) is retry


RECORDED = "MG367459 Sorensen, Philip 1957-10-28 M Active"


def _check(observed: str):
    from openadapt_flow.runtime import identity

    check = identity.verify_target_identity(RECORDED, observed)
    assert check.observed == observed
    return check


def test_a_read_of_a_sibling_record_contradicts_and_is_final() -> None:
    sibling = _check("MG619501 Sorenson, Philip 1957-10-28")
    assert sibling.status == "mismatch" and identity_read_contradicts(sibling)
    assert better_identity_read(sibling, _check(RECORDED)) is sibling


def test_an_incomplete_read_of_the_right_record_can_be_rescued() -> None:
    """Columns cut off are not evidence of another record, so the re-read may
    still confirm it; refusing here only added false stops."""
    short = _check("MG367459 Sorensen, Philip 1957-10-28")
    assert short.status == "mismatch" and not identity_read_contradicts(short)
    full = _check(RECORDED)
    assert better_identity_read(short, full) is full


def test_an_incomplete_read_then_a_sibling_read_stays_refused() -> None:
    short = _check("Sorensen, Philip 1957-10-28")
    sibling = _check("MG619501 Sorenson, Philip 1957-10-28")
    assert better_identity_read(short, sibling) is sibling
