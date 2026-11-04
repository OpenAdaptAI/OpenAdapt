"""The possible-duplicate list names no real candidate (no_duplicate_candidate).

A registration program used to stop whenever an app's duplicate check listed
any chart. OpenEMR lists a chart that shares only a first name, so most of
those stops were false. This postcondition reads the listed rows and passes
only when the list has loaded and no row carries this run's value of any
identity parameter (here the last name or the date of birth). It never passes
on a list it can't read, on a list that hasn't loaded, or on a missing value.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.ir import Postcondition, PostconditionKind

PARAMS = {"lname": "Jesswood", "fname": "Rosalind", "DOB": "1990-09-09"}
MARKER = "No matches were found."


class _Backend:
    def __init__(self, rows: Optional[list[str]], marker: bool = False) -> None:
        self.rows, self.marker = rows, marker
        self.read: list[Any] = []
        self.url, self.page_title, self.page_count = "http://x/", "x", 1

    def element_texts(self, selector: str, frame_path: tuple[str, ...]) -> Any:
        self.read.append((selector, frame_path))
        return self.rows

    def visible_text_present(self, text: str) -> bool:
        return self.marker and text == MARKER


def _check(backend: _Backend, params: dict[str, str] = PARAMS) -> bool:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    replayer = Replayer(backend, vision=FakeVision())
    replayer._current_params = dict(params)
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text=MARKER,
        candidate_rows="tr.oneresult",
        candidate_frame_path=("#modalframe",),
        candidate_params=("lname", "DOB"),
    )
    return replayer._postcondition_passes(pc, b"", None, {})


def test_a_first_name_only_hit_is_not_a_candidate() -> None:
    backend = _Backend(["3\tOakwood, Rosalind\t1950-04-21\tFemale"])
    assert _check(backend) is True
    assert backend.read == [("tr.oneresult", ("#modalframe",))]


@pytest.mark.parametrize(
    "row",
    [
        "5\tJesswood, Rosalind\t1990-09-09\tFemale",  # the same person
        "5\tJESSWOOD, Ana\t1961-01-01\tFemale",  # a namesake: a person decides
        "5\tOakwood, Ana\t1990-09-09\tFemale",  # the same date of birth
    ],
)
def test_a_real_candidate_always_stops(row: str) -> None:
    assert _check(_Backend(["3\tOakwood, Rosalind\t1950-04-21\tFemale", row])) is False


def test_an_empty_list_passes_only_with_its_no_matches_marker() -> None:
    assert _check(_Backend([], marker=True)) is True
    # Not loaded yet: no rows and no marker.
    assert _check(_Backend([], marker=False)) is False


def test_it_fails_closed() -> None:
    # The list can't be read (frame missing, or no reader on this backend).
    assert _check(_Backend(None, marker=True)) is False
    # A value to compare with is missing.
    assert _check(_Backend([], marker=True), {"lname": "Jesswood"}) is False
    assert _check(_Backend([], marker=True), {**PARAMS, "DOB": " "}) is False


def test_unset_candidate_fields_serialize_as_before() -> None:
    pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")
    assert not {"candidate_rows", "candidate_frame_path", "candidate_params"} & set(
        pc.model_dump()
    )


def test_a_linear_run_compares_with_its_own_parameters() -> None:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    # A linear (non-program) run keeps its parameters in _run_params only.
    replayer = Replayer(
        _Backend(["3\tOakwood, Rosalind\t1950-04-21"]), vision=FakeVision()
    )
    replayer._run_params = dict(PARAMS)
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text=MARKER,
        candidate_rows="tr.oneresult",
        candidate_params=("lname", "DOB"),
    )
    assert replayer._postcondition_passes(pc, b"", None, {}) is True


DOB_PARAMS = {"lname": "Brennan", "fname": "Nadia", "DOB": "1944-04-20"}


@pytest.mark.parametrize(
    "row",
    [
        # A married-name change: another last name, the same date of birth,
        # shown however the app formats dates.
        "7\tOkafor, Nadia\tApr 20, 1944\tFemale",
        "7\tOkafor, Nadia\t04/20/1944\tFemale",
        "7\tOkafor, Nadia\t20.04.1944\tFemale",
        "7\tOkafor, Nadia\t20 April 1944\tFemale",
        # A row date that reads either way counts when either reading matches.
        "7\tOkafor, Nadia\t04/20/1944 (20/04/1944)\tFemale",
    ],
)
def test_a_shared_date_of_birth_in_any_format_stops(row: str) -> None:
    assert _check(_Backend([row]), DOB_PARAMS) is False


def test_an_ambiguous_row_date_counts_when_either_reading_matches() -> None:
    params = {**DOB_PARAMS, "DOB": "1970-04-03"}
    # 03/04/1970 is 4 March or 3 April: the 3 April reading is this patient's.
    assert _check(_Backend(["7\tOkafor, Nadia\t03/04/1970"]), params) is False


@pytest.mark.parametrize(
    "row",
    [
        "7\tOkafor, Nadia\tApr 21, 1944\tFemale",  # an unrelated date
        "7\tOkafor, Nadia\t1950-04-21\tFemale",
        "7\tOkafor, Nadia\tno date shown\tFemale",  # nothing to compare
    ],
)
def test_a_row_with_an_unrelated_date_or_none_proceeds(row: str) -> None:
    assert _check(_Backend([row]), DOB_PARAMS) is True
