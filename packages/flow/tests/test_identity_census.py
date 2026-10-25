"""The identity candidate census: does a verified identity pick one record?

Real-browser coverage is in ``tests/test_realism_browser.py`` (the worklist
cases). These tests pin the decision rules and the teach-time record id.
"""

from __future__ import annotations

from openadapt_flow.ir import IdentityCheck, IdentityTemplate
from openadapt_flow.runtime import identity_template as itmpl
from openadapt_flow.runtime.identity_census import (
    CensusDecision,
    count_matching_rows,
    decide,
)

ROW = "John Smith Follow-up Dr Lee"


def _census(*rows: tuple[bool, str | None, float], top_level: bool = True) -> dict:
    """Rows as ``(is_target, record_key, dy)``."""

    target_key = next(key for is_target, key, _ in rows if is_target)
    return {
        "record_key": target_key,
        "incomplete": False,
        "top_level": top_level,
        "candidates": [
            {"self": is_target, "record_key": key, "dx": 0.0, "dy": dy}
            for is_target, key, dy in rows
        ],
    }


def _template(record_key: str | None) -> IdentityTemplate:
    tmpl = itmpl.build_identity_template(
        None, structured_identity=ROW, record_key=record_key
    )
    assert tmpl is not None
    return tmpl


def _decide(census: dict, record_key: str | None) -> CensusDecision:
    tmpl = _template(record_key)
    return decide(
        census,
        has_recorded_key=tmpl.record_key is not None,
        key_matches=lambda key: itmpl.record_key_matches(tmpl, key),
    )


def test_two_identical_rows_without_ids_are_ambiguous() -> None:
    census = _census((True, None, 0.0), (False, None, 49.0))
    assert _decide(census, None) == CensusDecision("ambiguous", 2)


def test_three_identical_rows_without_ids_are_ambiguous() -> None:
    census = _census((False, None, -49.0), (True, None, 0.0), (False, None, 49.0))
    assert _decide(census, None) == CensusDecision("ambiguous", 3)


def test_distinct_ids_on_identical_text_pick_the_recorded_row() -> None:
    """The resolver landed on 1002; the recorded 1001 is one row below."""

    census = _census((True, "data-id=1002", 0.0), (False, "data-id=1001", 49.0))
    assert _decide(census, "data-id=1001") == CensusDecision("retarget", 2, (0.0, 49.0))


def test_the_recorded_id_under_the_point_verifies_among_homonyms() -> None:
    census = _census((True, "data-id=1001", 0.0), (False, "data-id=1002", 49.0))
    assert _decide(census, "data-id=1001") == CensusDecision("unique", 2)


def test_live_ids_without_a_recorded_id_do_not_pick_a_row() -> None:
    """Distinct live ids say the rows differ, not which one was recorded."""

    census = _census((True, "data-id=1002", 0.0), (False, "data-id=1001", 49.0))
    assert _decide(census, None) == CensusDecision("ambiguous", 2)


def test_a_unique_row_verifies() -> None:
    assert _decide(_census((True, None, 0.0)), None) == CensusDecision("unique", 1)
    keyed = _census((True, "data-id=1003", 0.0))
    assert _decide(keyed, "data-id=1003") == CensusDecision("unique", 1)


def test_a_unique_row_with_another_record_id_is_the_wrong_record() -> None:
    census = _census((True, "data-id=2002", 0.0))
    assert _decide(census, "data-id=1001") == CensusDecision("wrong_record", 1)


def test_the_recorded_id_on_two_rows_is_ambiguous() -> None:
    census = _census((True, "data-id=1001", 0.0), (False, "data-id=1001", 49.0))
    assert _decide(census, "data-id=1001") == CensusDecision("ambiguous", 2)


def test_no_retarget_inside_a_frame() -> None:
    census = _census(
        (True, "data-id=1002", 0.0), (False, "data-id=1001", 49.0), top_level=False
    )
    assert _decide(census, "data-id=1001").verdict == "wrong_record"


def test_incomplete_or_malformed_census_is_uncounted() -> None:
    assert _decide({"incomplete": True}, None).verdict == "uncounted"
    assert _decide({"candidates": "x"}, None).verdict == "uncounted"
    # The target must appear exactly once in its own census.
    assert _decide({"candidates": []}, None).verdict == "uncounted"
    assert _decide(None, None).verdict == "cannot_count"


def test_a_target_that_is_not_a_row_is_not_counted() -> None:
    assert _decide({"row": False}, None) == CensusDecision("not_a_row")
    assert _decide({"row": False}, "data-id=1001") == CensusDecision("not_a_row")


def test_record_id_is_stored_hashed_and_only_for_unparameterized_rows() -> None:
    tmpl = _template("data-id=1001")
    assert tmpl.record_key is not None and "1001" not in tmpl.record_key
    assert itmpl.record_key_matches(tmpl, "data-id=1001")
    assert not itmpl.record_key_matches(tmpl, "data-id=1002")
    assert not itmpl.record_key_matches(tmpl, None)
    # A row picked by a run parameter is a different record on every run.
    param = itmpl.build_identity_template(
        None,
        structured_identity=ROW,
        param_examples={"patient": "John Smith"},
        record_key="data-id=1001",
    )
    assert param is not None and param.structured_params == ["patient"]
    assert param.record_key is None


def test_unset_record_id_and_census_fields_leave_serialized_bytes_unchanged() -> None:
    assert "record_key" not in _template(None).model_dump()
    dumped = IdentityCheck(status="verified").model_dump()
    assert "candidate_count" not in dumped and "ambiguity_halt" not in dumped


def test_ocr_rows_count_every_verifying_row_but_the_target() -> None:
    rows = [(100, ROW), (149, ROW), (198, "Maria Garcia Intake Dr Lee")]
    count = count_matching_rows(
        rows, target_y=100, verifies=lambda text: text == ROW, same_row_px=16
    )
    assert count == 2
    unique = count_matching_rows(
        rows[2:], target_y=198, verifies=lambda text: text == ROW, same_row_px=16
    )
    assert unique == 1


def test_a_teach_time_twin_is_one_no_record_id_separates() -> None:
    from openadapt_flow.recorder import _census_has_twin

    def census(own, other):
        return {
            "record_key": own,
            "candidates": [
                {"self": True, "record_key": own},
                {"self": False, "record_key": other},
            ],
        }

    assert _census_has_twin(census(None, None))
    assert _census_has_twin(census("data-id=1001", "data-id=1001"))
    assert not _census_has_twin(census("data-id=1001", "data-id=1002"))
    assert not _census_has_twin({"candidates": [{"self": True}]})


def test_teach_ambiguity_is_stored_and_left_out_when_unset() -> None:
    twin = itmpl.build_identity_template(
        None, structured_identity=ROW, structured_ambiguous=True
    )
    plain = itmpl.build_identity_template(None, structured_identity=ROW)
    assert twin is not None and twin.teach_ambiguous is True
    assert plain is not None and "teach_ambiguous" not in plain.model_dump()
