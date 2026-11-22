"""The verify_run folds moved onto the kernel change nothing but the empty
case (kill criterion 6): differential against the copies vendored from main."""

from __future__ import annotations

from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import verify_run
from openadapt_flow.runtime.effects.effect import Verdict
from tests.failopen.selftest import _v_s9_verify_run_pre as before


def _plan(items: list[object]) -> list[str]:
    return [str(i) for i in range(len(items))]


PROFILE = settings(derandomize=True, max_examples=300, deadline=None)
# The three members the vendored fold knew; a member added later is not
# compared, since the old fold would have called it CONFIRMED.
VERDICTS = st.sampled_from([Verdict.CONFIRMED, Verdict.REFUTED, Verdict.INDETERMINATE])
OBSERVED = st.sampled_from(["present", "absent", "unknown", "conflicting"])


@PROFILE
@given(st.lists(VERDICTS, min_size=1, max_size=12))
def test_aggregate_is_unchanged_on_any_effects(verdicts: list[Verdict]) -> None:
    items = [SimpleNamespace(verdict=v) for v in verdicts]
    assert verify_run._aggregate(_plan(items), items) == before._aggregate(items)


@PROFILE
@given(st.lists(OBSERVED, min_size=1, max_size=12))
def test_observed_is_unchanged_on_any_effects(seen: list[str]) -> None:
    items = [SimpleNamespace(observed_effect=s) for s in seen]
    assert verify_run._observed(_plan(items), items) == before._observed(items)


class _Tier:
    def __init__(self, sor: bool) -> None:
        self.sor = sor

    def reads_system_of_record(self) -> bool:
        return self.sor


@PROFILE
@given(
    st.lists(
        st.tuples(st.booleans(), st.booleans(), st.sampled_from([None, True, False])),
        min_size=1,
        max_size=8,
    )
)
def test_readback_class_is_unchanged_when_tiers_pair_with_effects(
    rows: list[tuple[bool, bool, object]],
) -> None:
    verdicts = [SimpleNamespace(unavailable=u, stale=s) for u, s, _ in rows]
    tiers = [None if t is None else _Tier(bool(t)) for _, _, t in rows]
    assert verify_run.readback_class(verdicts, tiers) == before.readback_class(
        verdicts, tiers
    )


def test_only_the_empty_and_unpaired_cases_change() -> None:
    assert (before._aggregate([]), verify_run._aggregate([], [])) == (
        "CONFIRMED",
        "INDETERMINATE",
    )
    assert (before._observed([]), verify_run._observed([], [])) == (
        "present",
        "unknown",
    )
    assert before.readback_class([], []) == "independent_readback"
    assert verify_run.readback_class([], []) == "unchecked"
    one = [SimpleNamespace(unavailable=False, stale=False)] * 2
    assert before.readback_class(one, [_Tier(True)]) == "independent_readback"
    assert verify_run.readback_class(one, [_Tier(True)]) == "unchecked"


def test_a_planned_effect_with_no_verdict_never_passes() -> None:
    """The fail-open fix: the folds settle against the plan, so a verdict
    that went missing, or one nobody planned, is not a pass."""

    one = [SimpleNamespace(verdict=Verdict.CONFIRMED, observed_effect="present")]
    assert verify_run._aggregate(["0", "1"], one) == "INDETERMINATE"
    assert verify_run._observed(["0", "1"], one) == "unknown"
    assert verify_run._aggregate([], one) == "INDETERMINATE"
    assert verify_run._observed([], one) == "unknown"
    assert verify_run._aggregate(["0"], one) == "CONFIRMED"


def test_an_unlisted_observed_code_is_unknown() -> None:
    """The old fold read a code nobody listed as present (C3)."""

    odd = [
        SimpleNamespace(observed_effect="present"),
        SimpleNamespace(observed_effect="maybe"),
    ]
    assert before._observed(odd) == "present"
    assert verify_run._observed(_plan(odd), odd) == "unknown"


def test_an_unplanned_verdict_beside_a_refuted_one_is_unknown() -> None:
    got = [
        SimpleNamespace(observed_effect="absent"),
        SimpleNamespace(observed_effect="present"),
    ]
    assert verify_run._observed(["0"], got) == "unknown"
