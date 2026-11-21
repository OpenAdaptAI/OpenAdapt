"""Properties of openadapt_flow.verdict, the one fold."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.verdict import (
    Errored,
    FailClosed,
    Missing,
    Obligations,
    Observed,
    Result,
    Status,
    Subject,
    TrustSource,
    binding_mismatches,
    from_code,
    observe,
    require_bound,
    settle,
    trust_label,
)

PROFILE = settings(derandomize=True, max_examples=150, deadline=None)
CODES = st.sampled_from([s.value for s in Status])


def _subjects(n: int) -> list[Subject]:
    return [Subject(system="s", table="t", key=str(i), field="f") for i in range(n)]


def _result(subject: Subject, status: str) -> Result:
    if status == "held":
        return Result.held(subject, Observed(1))
    if status == "refuted":
        return Result.refuted(subject, Observed(0), "no")
    return Result.not_checked(subject, "no read")


@st.composite
def cases(draw: st.DrawFn) -> tuple[Obligations, list[Result]]:
    n = draw(st.integers(0, 8))
    subjects = _subjects(n)
    statuses = draw(st.lists(CODES, min_size=n, max_size=n))
    keep = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    results = [_result(s, c) for s, c, k in zip(subjects, statuses, keep) if k]
    return Obligations.fix(subjects, source="plan"), results


def test_an_empty_set_never_passes() -> None:
    settled = settle(Obligations.fix([], source="plan"), [])
    assert not settled.passes
    assert settled.label(precedence=(Status.REFUTED,)) is Status.NOT_CHECKED
    assert settled.rate(Status.HELD) is None


@PROFILE
@given(cases(), st.randoms(use_true_random=False))
def test_order_does_not_matter(
    case: tuple[Obligations, list[Result]], rnd: object
) -> None:
    obligations, results = case
    shuffled = list(results)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert settle(obligations, results) == settle(obligations, shuffled)


@PROFILE
@given(cases(), CODES.filter(lambda c: c != "held"))
def test_a_non_held_result_never_makes_a_fail_pass(
    case: tuple[Obligations, list[Result]], code: str
) -> None:
    obligations, results = case
    before = settle(obligations, results).passes
    for subject in [*obligations.items, Subject(key="stranger")]:
        after = settle(obligations, [*results, _result(subject, code)]).passes
        assert after <= before


@PROFILE
@given(cases())
def test_passes_only_when_every_obligation_held(
    case: tuple[Obligations, list[Result]],
) -> None:
    obligations, results = case
    settled = settle(obligations, results)
    held = {r.subject.key_tuple() for r in results if r.status is Status.HELD}
    every = obligations.items and all(o.key_tuple() in held for o in obligations.items)
    assert settled.passes == (bool(every) and len(results) == len(obligations.items))
    buckets = (
        settled.held,
        settled.refuted,
        settled.not_checked,
        settled.missing,
        settled.mismatched,
        settled.conflicting,
    )
    assert sum(len(b) for b in buckets) == settled.total


@PROFILE
@given(st.integers(1, 40), st.integers(0, 39))
def test_nothing_is_truncated_at_the_limit(n: int, limit: int) -> None:
    subjects = _subjects(n)
    results = [_result(s, "held") for s in subjects[:-1]] + [
        _result(subjects[-1], "refuted")
    ]
    settled = settle(Obligations.fix(subjects, source="plan"), results, limit=limit)
    assert not settled.passes
    assert len(settled.refuted) == 1
    assert settled.over_limit == (n > limit)


def test_a_result_for_another_run_is_mismatched() -> None:
    planned = Subject(run_id="run-1", nonce="n1", key="k")
    other = Subject(run_id="run-2", nonce="n1", key="k")
    settled = settle(
        Obligations.fix([planned], source="plan"), [_result(other, "held")]
    )
    assert settled.mismatched == (planned,) and not settled.passes
    assert binding_mismatches(planned, other) == ("run_id",)
    assert binding_mismatches(planned, Subject(key="k")) == ("run_id", "nonce")
    with pytest.raises(FailClosed):
        require_bound(planned, other)
    require_bound(planned, planned.bound(digest="d"))


def test_a_decoy_table_is_a_different_subject() -> None:
    real = Subject(system="db", table="orders", key="7", field="total")
    decoy = Subject(system="db", table="orders_copy", key="7", field="total")
    settled = settle(Obligations.fix([real], source="plan"), [_result(decoy, "held")])
    assert settled.missing == (real,) and len(settled.unexpected) == 1


def test_disagreeing_duplicates_conflict() -> None:
    (s,) = _subjects(1)
    settled = settle(
        Obligations.fix([s], source="plan"), [_result(s, "held"), _result(s, "refuted")]
    )
    assert settled.conflicting == (s,) and not settled.passes


def test_rates_use_the_planned_total() -> None:
    subjects = _subjects(4)
    settled = settle(
        Obligations.fix(subjects, source="plan"), [_result(subjects[0], "held")]
    )
    assert settled.rate(Status.HELD) == Fraction(1, 4)
    assert settled.rate(Status.NOT_CHECKED) == Fraction(3, 4)


def test_label_follows_the_site_precedence() -> None:
    a, b = _subjects(2)
    settled = settle(
        Obligations.fix([a, b], source="plan"),
        [_result(a, "refuted"), _result(b, "not_checked")],
    )
    assert (
        settled.label(precedence=(Status.NOT_CHECKED, Status.REFUTED))
        is Status.NOT_CHECKED
    )
    assert (
        settled.label(precedence=(Status.REFUTED, Status.NOT_CHECKED)) is Status.REFUTED
    )


@pytest.mark.parametrize(
    "exc", [Exception("x"), TimeoutError(), KeyError("k"), OSError(5, "io")]
)
def test_observe_never_returns_observed_on_a_raise(exc: Exception) -> None:
    def probe() -> object:
        raise exc

    got = observe(probe)
    assert isinstance(got, Errored) and got.exc_type == type(exc).__name__
    assert observe(probe, benign={KeyError: Missing("no row")}) == (
        Missing("no row") if isinstance(exc, KeyError) else got
    )


def test_observe_reraises_fail_closed() -> None:
    def probe() -> object:
        raise FailClosed("stop")

    with pytest.raises(FailClosed):
        observe(probe, benign={Exception: Missing("x")})


def test_held_needs_a_complete_observation() -> None:
    (s,) = _subjects(1)
    for obs in (Missing("x"), Errored("E", "x"), Observed(1, complete=False)):
        with pytest.raises(TypeError):
            Result.held(s, obs)
    with pytest.raises(TypeError):
        Result(s, Status.HELD, "forged")
    with pytest.raises(TypeError):
        Result.refuted(s, Missing("x"), "no")


def test_from_observation_never_judges_a_missing_read() -> None:
    (s,) = _subjects(1)

    def judge(_value: object) -> tuple[Status, str]:
        raise AssertionError("judge called")

    for obs in (Missing("x"), Errored("E", "x"), Observed(1, complete=False)):
        assert Result.from_observation(s, obs, judge).status is Status.NOT_CHECKED
    assert (
        Result.from_observation(s, Observed(1), lambda v: (Status.HELD, "ok")).status
        is Status.HELD
    )


@pytest.mark.parametrize("code", ["unknown", "", None, 1, "CONFIRMED", "confirmed "])
def test_from_code_is_an_allow_list(code: object) -> None:
    status = from_code(
        code, held=frozenset({"confirmed"}), refuted=frozenset({"refuted"})
    )
    assert status is Status.NOT_CHECKED


def test_from_code_refuses_overlapping_lists() -> None:
    with pytest.raises(ValueError):
        from_code("x", held=frozenset({"x"}), refuted=frozenset({"x"}))


def test_obligations_dedupe_by_subject_key_and_binding() -> None:
    a, b = Subject(key="1", run_id="r1"), Subject(key="1", run_id="r2")
    assert Obligations.fix([a, a, b], source="p").items == (a, b)
    settled = settle(Obligations.fix([a, b], source="p"), [_result(a, "held")], limit=1)
    assert settled.mismatched == (b,) and settled.over_limit and not settled.passes


def test_a_stray_result_from_another_run_is_unexpected() -> None:
    a = Subject(key="1", run_id="r1")
    stray = Subject(key="1", run_id="r2")
    settled = settle(
        Obligations.fix([a], source="p"), [_result(a, "held"), _result(stray, "held")]
    )
    assert settled.held == (a,) and len(settled.unexpected) == 1 and not settled.passes


def test_a_held_result_cannot_be_moved_to_another_subject() -> None:
    import dataclasses

    a = Subject(key="1", run_id="r1")
    with pytest.raises(TypeError):
        dataclasses.replace(_result(a, "held"), subject=Subject(key="1", run_id="r2"))


def test_benign_cannot_map_a_raise_to_a_value() -> None:
    def probe() -> object:
        raise TimeoutError

    with pytest.raises(TypeError):
        observe(probe, benign={TimeoutError: Observed(0)})


@pytest.mark.parametrize(
    "source, verified, label",
    [
        (TrustSource.LOCAL_KEY, True, "signed on this computer"),
        (TrustSource.REGISTERED, True, "signed by a registered signer"),
        (TrustSource.EMBEDDED, True, "signature valid, signer not known"),
        (TrustSource.UNVERIFIED, True, "not verified"),
        (TrustSource.LOCAL_KEY, False, "not verified"),
        ("local_key", True, "not verified"),
        (None, True, "not verified"),
    ],
)
def test_trust_label_names_a_signer_only_from_a_verified_source(
    source: object, verified: bool, label: str
) -> None:
    assert trust_label(source, verified=verified) == label
