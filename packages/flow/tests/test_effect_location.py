"""The effect-location record and its evaluator (synthetic readings only)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from openadapt_types.check_level import CheckLevel

from openadapt_flow.effect_location import (
    Codec,
    EffectLocation,
    Locator,
    Reading,
    _compat,
    decode,
    evaluate,
    evaluate_all,
)
from openadapt_flow.effect_location.record import Part

RUN = "run-1"
DMY_MONTHS = tuple(
    (name,)
    for name in (
        "m01", "m02", "m03", "m04", "m05", "m06",
        "m07", "m08", "m09", "m10", "m11", "m12",
    )
)  # fmt: skip


def loc(
    keys: tuple[str, ...] = ("name",),
    codec: Codec | None = None,
    expected: dict[str, Any] | None = None,
    start: str = "same_screen",
    **kw: Any,
) -> EffectLocation:
    return EffectLocation(
        id=kw.pop("id", "L1"),
        keys=keys,
        path=("step-1", "step-2"),
        start=start,
        locator=kw.pop("locator", Locator("label", "^Name$")),
        codec=codec or Codec("text"),
        expected={"name": "Ada Byron"} if expected is None else expected,
        **kw,
    )


def reading(*texts: Any, **kw: Any) -> Reading:
    matches = kw.pop("matches", None)
    return Reading(
        location_id=kw.pop("location_id", "L1"),
        run_id=kw.pop("run_id", RUN),
        start=kw.pop("start", "same_screen"),
        matches=tuple(matches)
        if matches is not None
        else tuple({"text": t} for t in texts),
        complete=kw.pop("complete", True),
        error=kw.pop("error", None),
        basis=kw.pop("basis", None),
    )


def status(location: EffectLocation, r: Reading | None) -> str:
    return evaluate(location, r, run_id=RUN).status


# -- one scalar location ---------------------------------------------------------


def test_shown_value_holds_at_the_start_level() -> None:
    report = evaluate(loc(), reading("Ada  Byron"), run_id=RUN)
    assert report.status == "held"
    assert report.level is CheckLevel.SAME_SCREEN
    assert report.results[0].locations == ("L1",)


@pytest.mark.parametrize(
    "shown", ["Ada", "Ada Byron 0", "Ada Byron (old)", "", "ada byron"]
)
def test_close_text_refutes(shown: str) -> None:
    assert status(loc(), reading(shown)) == "refuted"


def test_fold_ignores_case_only() -> None:
    folded = loc(codec=Codec("fold"))
    assert status(folded, reading("ADA byron")) == "held"
    assert status(folded, reading("ADA byro")) == "refuted"


@pytest.mark.parametrize(
    ("r", "why"),
    [
        (None, "the location was not read"),
        (reading(), "the location was not found"),
        (reading("Ada Byron", "Ada Byron"), "found 2 times, expected 1"),
        (reading("Ada Byron", error="timeout"), "the read failed"),
        (reading("Ada Byron", run_id="other"), "the reading belongs to another run"),
        (reading("Ada Byron", complete=False), "the read was partial"),
        (reading(matches=[{"text": None}]), "the displayed text could not be read"),
        (
            reading(matches=[{"text": "Ada Byron", "x": "1"}]),
            "the displayed text could not be read",
        ),
    ],
)
def test_what_cannot_be_read_is_not_checked(r: Reading | None, why: str) -> None:
    report = evaluate(loc(), r, run_id=RUN)
    assert report.status == "not_checked"
    assert report.results[0].reason == why
    assert report.level is CheckLevel.UNCHECKED


def test_a_partial_read_still_refutes() -> None:
    assert status(loc(), reading("Grace", complete=False)) == "refuted"


def test_expected_count_two() -> None:
    twice = loc(expected_count=2)
    assert status(twice, reading("Ada Byron", "Ada Byron")) == "held"
    assert status(twice, reading("Ada Byron")) == "not_checked"
    assert status(twice, reading("Ada Byron", "Grace")) == "refuted"


@pytest.mark.parametrize(
    "bad",
    [
        {"locator": Locator("pixel", "x")},
        {"codec": Codec("guess")},
        {"start": "telepathy"},
        {"expected_count": 0},
        {"keys": ("name", "name")},
    ],
)
def test_unknown_or_malformed_records_are_not_checked(bad: dict[str, Any]) -> None:
    assert status(loc(**bad), reading("Ada Byron")) == "not_checked"


def test_the_start_condition_must_be_met() -> None:
    wanted = loc(start="new_session")
    assert status(wanted, reading("Ada Byron")) == "not_checked"
    assert (
        status(wanted, reading("Ada Byron", start="fresh_navigation")) == "not_checked"
    )
    assert status(wanted, reading("Ada Byron", start="new_session")) == "held"
    stronger = evaluate(loc(), reading("Ada Byron", start="new_session"), run_id=RUN)
    assert stronger.level is CheckLevel.SEPARATE_SESSION


def test_start_conditions_map_to_levels() -> None:
    levels = {
        s: evaluate(loc(start=s), reading("Ada Byron", start=s), run_id=RUN).level
        for s in ("same_screen", "fresh_navigation", "new_session", "other_channel")
    }
    assert levels == {
        "same_screen": CheckLevel.SAME_SCREEN,
        "fresh_navigation": CheckLevel.REOPENED,
        "new_session": CheckLevel.SEPARATE_SESSION,
        "other_channel": CheckLevel.SYSTEM_OF_RECORD,
    }


# -- baseline --------------------------------------------------------------------


def test_a_value_that_was_already_there_proves_nothing() -> None:
    same = loc(baseline={"name": "Ada Byron"})
    report = evaluate(same, reading("Ada Byron"), run_id=RUN)
    assert report.status == "not_checked"
    assert "already shown before" in report.results[0].reason


def test_a_location_still_showing_the_baseline_refutes() -> None:
    changed = loc(baseline={"name": "Grace"})
    assert status(changed, reading("Grace")) == "refuted"
    assert status(changed, reading("Ada Byron")) == "held"


# -- codecs ----------------------------------------------------------------------


def test_money_compares_whole_amounts_in_minor_units() -> None:
    money = Codec("money", affixes=("$",))
    m = loc(keys=("cost",), codec=money, expected={"cost": 125000})
    assert status(m, reading("$1,250.00")) == "held"
    assert status(m, reading("1,250")) == "held"
    assert status(m, reading("$250.00")) == "refuted"
    assert status(m, reading("$1,250.005")) == "not_checked"
    assert status(m, reading("$12,50.00")) == "not_checked"
    no_affix = loc(keys=("cost",), codec=Codec("money"), expected={"cost": 125000})
    assert status(no_affix, reading("$1,250.00")) == "not_checked"
    refund = loc(keys=("cost",), codec=money, expected={"cost": -25000})
    assert status(refund, reading("-$250.00")) == "held"
    assert status(refund, reading("$-250.00")) == "held"
    assert status(refund, reading("--$250.00")) == "not_checked"
    as_text = loc(keys=("cost",), codec=money, expected={"cost": "125000"})
    assert status(as_text, reading("$1,250.00")) == "not_checked"


def test_units_and_numbers() -> None:
    unit = loc(keys=("w",), codec=Codec("unit", units=("kg",)), expected={"w": "12.5"})
    assert status(unit, reading("12.50 kg")) == "held"
    assert status(unit, reading("12.5kg")) == "held"
    assert status(unit, reading("12.5 lb")) == "not_checked"
    assert status(unit, reading("13 kg")) == "refuted"
    dec = loc(keys=("n",), codec=Codec("number"), expected={"n": Decimal("2.50")})
    assert status(dec, reading("2.5")) == "held"
    nan = loc(keys=("n",), codec=Codec("number"), expected={"n": Decimal("NaN")})
    assert status(nan, reading("2.5")) == "not_checked"
    num = loc(keys=("n",), codec=Codec("number"), expected={"n": True})
    assert status(num, reading("1")) == "not_checked"


def test_dates_need_an_order_unless_iso() -> None:
    iso = loc(keys=("d",), codec=Codec("date"), expected={"d": "2024-03-07"})
    assert status(iso, reading("2024-03-07")) == "held"
    assert status(iso, reading("03/07/2024")) == "not_checked"
    mdy = loc(
        keys=("d",), codec=Codec("date", date_order="MDY"), expected={"d": "2024-03-07"}
    )
    assert status(mdy, reading("03/07/2024")) == "held"
    assert status(mdy, reading("07/03/2024")) == "refuted"
    assert status(mdy, reading("02/30/2024")) == "not_checked"
    spelled = loc(
        keys=("d",),
        codec=Codec("date", date_order="DMY", month_names=DMY_MONTHS),
        expected={"d": "2024-03-07"},
    )
    assert status(spelled, reading("7 M03 2024")) == "held"
    assert status(spelled, reading("7 m13 2024")) == "not_checked"


def test_split_date_reads_three_parts_as_one_value() -> None:
    split = loc(
        keys=("dob",), codec=Codec("split_date"), expected={"dob": "1990-01-31"}
    )
    good = {"year": "1990", "month": "1", "day": "31"}
    assert status(split, reading(matches=[good])) == "held"
    assert status(split, reading(matches=[{**good, "day": "30"}])) == "refuted"
    assert status(split, reading(matches=[{**good, "day": "32"}])) == "not_checked"
    assert (
        status(split, reading(matches=[{"year": "1990", "month": "1"}]))
        == "not_checked"
    )


def test_bool_and_enum_need_their_tables() -> None:
    b = loc(keys=("ok",), codec=Codec("bool"), expected={"ok": True})
    assert status(b, reading("yes")) == "not_checked"
    tokens = Codec("bool", true_tokens=("ja",), false_tokens=("nein",))
    b2 = loc(keys=("ok",), codec=tokens, expected={"ok": True})
    assert status(b2, reading("JA")) == "held"
    assert status(b2, reading("nein")) == "refuted"
    e = loc(
        keys=("s",),
        codec=Codec("enum", table=(("Ontario", "ON"),)),
        expected={"s": "ON"},
    )
    assert status(e, reading("ontario")) == "held"
    assert status(e, reading("Onta")) == "not_checked"


# -- composite: one summary row covering two values -----------------------------

ROW = Codec(
    "template",
    pattern=r"(?P<n>.+) \((?P<d>[^)]*)\)",
    parts=(
        Part("n", "traveller_name", Codec("text")),
        Part("d", "traveller_dob", Codec("date", date_order="MDY")),
    ),
)
ROW_EXPECTED = {"traveller_name": "Ada Byron", "traveller_dob": "1990-01-31"}


def row_loc(**kw: Any) -> EffectLocation:
    return loc(
        keys=("traveller_name", "traveller_dob"), codec=ROW, expected=ROW_EXPECTED, **kw
    )


def test_a_summary_row_checks_both_values() -> None:
    report = evaluate(row_loc(), reading("Ada Byron (01/31/1990)"), run_id=RUN)
    assert report.status == "held"
    assert {r.key: r.status for r in report.results} == {
        "traveller_name": "held",
        "traveller_dob": "held",
    }


def test_one_wrong_part_refutes_only_that_value() -> None:
    report = evaluate(row_loc(), reading("Ada Byron (01/30/1990)"), run_id=RUN)
    assert report.by_key()["traveller_name"].status == "held"
    assert report.by_key()["traveller_dob"].status == "refuted"
    assert report.status == "refuted"


def test_a_row_that_does_not_fit_leaves_both_not_checked() -> None:
    report = evaluate(row_loc(), reading("Ada Byron, 01/31/1990"), run_id=RUN)
    assert {r.status for r in report.results} == {"not_checked"}


def test_a_template_must_name_each_key() -> None:
    bad = loc(keys=("traveller_name",), codec=ROW, expected=ROW_EXPECTED)
    assert status(bad, reading("Ada Byron (01/31/1990)")) == "not_checked"


def test_decode_reports_errors_per_key() -> None:
    out = decode(
        ROW, {"text": "Ada Byron (13/31/1990)"}, ("traveller_name", "traveller_dob")
    )
    assert out["traveller_name"] == "Ada Byron"
    assert isinstance(out["traveller_dob"], ValueError)


# -- a run's plan ----------------------------------------------------------------


def test_every_planned_value_needs_a_location() -> None:
    report = evaluate_all(
        {"name": "Ada Byron", "email": "a@example.test"},
        [loc()],
        [reading("Ada Byron")],
        run_id=RUN,
    )
    assert report.status == "not_checked"
    assert report.by_key()["email"].reason == "no effect location shows this value"


def test_an_empty_plan_never_passes() -> None:
    assert (
        evaluate_all({}, [loc()], [reading("Ada Byron")], run_id=RUN).status
        == "not_checked"
    )
    assert evaluate_all({}, [], [], run_id=RUN).status == "not_checked"


def test_the_plan_decides_the_expected_value() -> None:
    template = loc(expected={})
    assert evaluate_all(
        {"name": "Ada Byron"}, [template], [reading("Ada Byron")], run_id=RUN
    ).passes
    stale = loc(expected={"name": "Grace"})
    report = evaluate_all(
        {"name": "Ada Byron"}, [stale], [reading("Ada Byron")], run_id=RUN
    )
    assert report.status == "not_checked"
    assert "different value than the plan" in report.results[0].reason


def test_left_over_readings_and_duplicates_do_not_pass() -> None:
    extra = evaluate_all(
        {"name": "Ada Byron"},
        [loc()],
        [reading("Ada Byron"), reading("x", location_id="L9")],
        run_id=RUN,
    )
    assert extra.status == "not_checked" and extra.unexpected == ("L9",)
    twice = evaluate_all(
        {"name": "Ada Byron"}, [loc(), loc()], [reading("Ada Byron")], run_id=RUN
    )
    assert twice.status == "not_checked"
    plan_twice = evaluate_all(
        [("name", "Ada Byron"), ("name", "Grace")],
        [loc()],
        [reading("Ada Byron")],
        run_id=RUN,
    )
    assert plan_twice.status == "not_checked"


def test_two_readings_of_one_location_must_agree() -> None:
    both = evaluate_all(
        {"name": "Ada Byron"},
        [loc()],
        [reading("Ada Byron"), reading("Grace")],
        run_id=RUN,
    )
    assert both.status == "refuted"
    same = evaluate_all(
        {"name": "Ada Byron"},
        [loc()],
        [reading("Ada Byron"), reading("Ada Byron")],
        run_id=RUN,
    )
    assert same.status == "held"


def test_the_weakest_location_sets_the_level() -> None:
    review = loc(id="review")
    fresh = loc(id="session", start="new_session")
    report = evaluate_all(
        {"name": "Ada Byron"},
        [review, fresh],
        [
            reading("Ada Byron", location_id="review"),
            reading("Ada Byron", location_id="session", start="new_session"),
        ],
        run_id=RUN,
    )
    assert report.status == "held"
    assert report.level is CheckLevel.SAME_SCREEN


def test_a_missing_run_id_never_passes() -> None:
    assert (
        evaluate(loc(), reading("Ada Byron", run_id=""), run_id="").status
        == "not_checked"
    )


# -- receipt words (the ladder from #2495) ---------------------------------------

needs_words = pytest.mark.skipif(
    not _compat.has_ladder_words(),
    reason="check_level.outcome_word is not on this branch yet",
)


@needs_words
@pytest.mark.parametrize(
    ("start", "basis", "word"),
    [
        ("same_screen", None, "SHOWN"),
        ("fresh_navigation", None, "SHOWN"),
        ("new_session", None, "VERIFIED"),
        ("other_channel", None, "RECORDED"),
        ("other_channel", "db_frame", "VERIFIED"),
        ("other_channel", "separate_session", "RECORDED"),
        ("same_screen", "db_frame", "SHOWN"),
    ],
)
def test_words_come_from_the_ladder(start: str, basis: str | None, word: str) -> None:
    report = evaluate(
        loc(start=start), reading("Ada Byron", start=start, basis=basis), run_id=RUN
    )
    assert report.status == "held"
    assert report.word == word


@needs_words
def test_a_check_that_did_not_hold_is_recorded() -> None:
    assert evaluate(loc(), reading("Grace"), run_id=RUN).word == "RECORDED"


def test_without_the_ladder_no_word_is_invented() -> None:
    if _compat.has_ladder_words():
        pytest.skip("the ladder is on this branch")
    assert evaluate(loc(), reading("Ada Byron"), run_id=RUN).word is None


# -- the record as data ----------------------------------------------------------


def test_round_trip_through_json() -> None:
    record = row_loc(baseline={"traveller_name": "x"}, source="demo-1")
    data = json.loads(json.dumps(record.to_dict()))
    assert EffectLocation.from_dict(data) == record
    r = reading("Ada Byron (01/31/1990)")
    assert Reading.from_dict(json.loads(json.dumps(r.to_dict()))) == r


def test_parsing_is_strict() -> None:
    data = loc().to_dict()
    with pytest.raises(ValueError):
        EffectLocation.from_dict({**data, "surprise": 1})
    with pytest.raises(ValueError):
        EffectLocation.from_dict({**data, "schema": "other/v1"})
    with pytest.raises(ValueError):
        EffectLocation.from_dict({**data, "codec": {"kind": "text", "units": "kg"}})
    r = reading("Ada Byron").to_dict()
    for name in r:
        gone = dict(r)
        del gone[name]
        with pytest.raises(ValueError):
            Reading.from_dict(gone)


def test_unknown_kinds_load_and_judge_not_checked() -> None:
    data = {**loc().to_dict(), "codec": {"kind": "future_kind"}}
    future = EffectLocation.from_dict(data)
    assert status(future, reading("Ada Byron")) == "not_checked"
