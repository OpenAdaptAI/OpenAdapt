"""The two positive controls moved onto the kernel keep their verdicts.

``hard_invariants.judge_hard_invariants`` and ``versions.judge_versions``
now build their verdicts with ``Result`` and ``settle``. A differential
against the copies vendored from main before the move shows the only change
is the one fail-open fix named below.
"""

from __future__ import annotations

import itertools
from typing import Any, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.hard_invariants import (
    HardInvariantVerdict,
    InvariantResult,
    judge_hard_invariants,
)
from openadapt_flow.runtime.effects.versions import (
    FrameVersions,
    judge_versions,
    seal_versions,
)
from tests.failopen.selftest import _v_hard_invariants_pre as hard_before
from tests.failopen.selftest import _v_versions_pre as versions_before

COUNTS = (None, 0, 1, 2, True)
FLAGS = (True, False)


def test_hard_invariants_are_unchanged_on_every_small_input() -> None:
    names = (
        "outside_scope_changes",
        "undeclared_deletes",
        "before_matches",
        "after_matches",
        "history_matches",
    )
    flags = (
        "scope_complete",
        "deletes_complete",
        "history_complete",
        "search_complete",
    )
    checked = 0
    for counts in itertools.product(COUNTS, repeat=len(names)):
        for bits in itertools.product(FLAGS, repeat=len(flags)):
            kwargs: dict[str, Any] = {
                **dict(zip(names, counts)),
                **dict(zip(flags, bits)),
            }
            new = judge_hard_invariants(**kwargs)
            old = hard_before.judge_hard_invariants(**kwargs)
            assert new.evidence() == old.evidence(), kwargs
            checked += 1
    assert checked == len(COUNTS) ** 5 * len(FLAGS) ** 4


def test_a_hand_written_held_never_holds() -> None:
    """Only a check the judge built carries a kernel result: three hand-made
    "held" checks are not checked, where the old fold said held."""

    checks = tuple(
        InvariantResult(name, "held", "ok")
        for name in ("declared_scope", "at_most_one", "no_undeclared_deletes")
    )
    assert HardInvariantVerdict(checks).status == "not_checked"
    assert hard_before.HardInvariantVerdict(checks).status == "held"
    refuted = (
        InvariantResult("declared_scope", "refuted", "outside_scope_changes", 1),
    )
    assert HardInvariantVerdict(refuted).status == "refuted"


KEY = b"k" * 32
RECORDS = st.sampled_from(["r1", "r2"])
VERSIONS = st.sampled_from([None, "", "1", "2", "3", "x"])
AUTHORS = st.sampled_from([None, "me", "them"])


def _rows(draw: Any) -> Any:
    return draw(
        st.lists(
            st.fixed_dictionaries({"rid": RECORDS, "v": VERSIONS, "by": AUTHORS}),
            max_size=4,
        )
    )


@st.composite
def _case(draw: Any) -> dict[str, Any]:
    return {
        "authors": draw(st.booleans()),
        "before": _rows(draw),
        "after": _rows(draw),
        "unread_after": draw(st.booleans()) and draw(st.booleans()),
        "targets": draw(
            st.lists(
                st.tuples(
                    st.sampled_from([None, "r1", "r2", "r3"]),
                    st.booleans(),
                    st.booleans(),
                ),
                max_size=3,
            )
        ),
    }


#: Problems that used to end a target's check before a refutation was
#: looked for. A refutation found after one of them now stands (the fix).
_EXPLAINED_LATER = {"duplicate_version", "no_new_version", "history_changed"}


def _spec(authors: bool) -> FrameVersions:
    if authors:
        return FrameVersions(
            read={},
            record_field="rid",
            version_field="v",
            author_field="by",
            self=["me"],
        )
    return FrameVersions(read={}, record_field="rid", version_field="v")


@settings(derandomize=True, max_examples=2000, deadline=None)
@given(_case())
def test_versions_change_only_where_a_refutation_was_hidden(
    case: dict[str, Any],
) -> None:
    spec = _spec(case["authors"])
    stored = seal_versions(spec, case["before"], KEY)
    after: Optional[list[dict[str, Any]]] = (
        None if case["unread_after"] else case["after"]
    )
    new = judge_versions(spec, stored, after, case["targets"], KEY)
    old = versions_before.judge_versions(spec, stored, after, case["targets"], KEY)
    # The targets the old check stopped on before looking for a refutation.
    stopped_early = [
        t
        for t in case["targets"]
        if versions_before.judge_versions(spec, stored, after, [t], KEY).problem
        in _EXPLAINED_LATER
    ]
    if new.status == old.status:
        assert new.problem == old.problem, case
        assert new.targets == old.targets, case
        if not stopped_early:
            assert (new.new_versions, new.own_versions) == (
                old.new_versions,
                old.own_versions,
            ), case
        return
    # The one change: a target the old check stopped on as unexplained, whose
    # versions also show another writer, is refuted now.
    assert (old.status, new.status) == ("not_checked", "refuted"), case
    assert any(
        judge_versions(spec, stored, after, [t], KEY).status == "refuted"
        for t in stopped_early
    ), case
