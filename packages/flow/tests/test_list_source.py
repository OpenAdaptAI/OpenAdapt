"""List walking, paging, capped searches and the pre-copy guard.

Runs against an in-memory synthetic worktable (accounts in a back-office
ledger). Synthetic data only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from openadapt_flow.records.schema import FieldSpec, RecordSchema
from openadapt_flow.runtime.list_source import (
    CopyGuard,
    KnownModal,
    ListHalt,
    ListLimits,
    ListWalk,
    SearchResult,
    iterate_list,
    iterate_partitioned,
    partition_search,
    split_date_range,
)

SCHEMA = RecordSchema(
    name="account",
    fields=(
        FieldSpec(name="account_no", kind="code"),
        FieldSpec(name="holder", kind="text"),
        FieldSpec(name="opened", kind="date"),
    ),
    identity_fields=("account_no",),
)

START = date(2020, 1, 1)


def make_rows(n: int) -> list[dict[str, str]]:
    return [
        {
            "account_no": f"AC{100000 + i}",
            "holder": f"Holder {i}",
            "opened": (START + timedelta(days=i % 1500)).isoformat(),
        }
        for i in range(n)
    ]


class Worktable:
    """A paged list view over rows, like a legacy search grid."""

    def __init__(
        self,
        rows: list[dict[str, str]],
        page_size: int = 25,
        *,
        repeat_last: bool = False,
        overlap: int = 0,
    ):
        self.rows = rows
        self.page_size = page_size
        self.page = 0
        self.repeat_last = repeat_last
        self.overlap = overlap
        self.reads = 0

    def read_page(self) -> list[dict[str, str]]:
        self.reads += 1
        step = self.page_size - self.overlap
        start = self.page * step
        return [dict(r) for r in self.rows[start : start + self.page_size]]

    def next_page(self) -> bool:
        step = self.page_size - self.overlap
        if self.page * step + self.page_size >= len(self.rows):
            if self.repeat_last:
                return True  # the app re-shows the last page
            return False
        self.page += 1
        return True


def test_walks_every_row_once_across_pages():
    rows = make_rows(2_503)
    table = Worktable(rows)
    walk = ListWalk()
    got = list(iterate_list(table, table, SCHEMA, walk=walk))
    assert [r.values["account_no"] for r in got] == [r["account_no"] for r in rows]
    assert walk.rows == 2_503 and walk.ended_by == "pager_end"
    assert walk.pages == 101


def test_overlapping_pages_are_deduplicated():
    rows = make_rows(100)
    table = Worktable(rows, page_size=10, overlap=3)
    walk = ListWalk()
    got = list(iterate_list(table, table, SCHEMA, walk=walk))
    assert len(got) == 100
    assert walk.duplicates_skipped > 0


def test_repeating_last_page_halts_unless_declared():
    rows = make_rows(30)
    stalled = Worktable(rows, repeat_last=True)
    with pytest.raises(ListHalt) as exc:
        list(iterate_list(stalled, stalled, SCHEMA))
    assert exc.value.reason == "pager_stalled"
    t = Worktable(rows, repeat_last=True)
    walk = ListWalk()
    got = list(
        iterate_list(
            t, t, SCHEMA, walk=walk, limits=ListLimits(repeat_page_is_end=True)
        )
    )
    assert len(got) == 30 and walk.ended_by == "repeat_page"


def test_conflicting_duplicate_identity_halts():
    rows = make_rows(5)
    rows.append(dict(rows[0], holder="Someone Else"))
    t = Worktable(rows)
    with pytest.raises(ListHalt) as exc:
        list(iterate_list(t, t, SCHEMA))
    assert exc.value.reason == "duplicate_identity_conflict"


def test_missing_identity_halts():
    rows = make_rows(3)
    rows[1]["account_no"] = " "
    t = Worktable(rows)
    with pytest.raises(ListHalt) as exc:
        list(iterate_list(t, t, SCHEMA))
    assert exc.value.reason == "row_identity_missing"


@pytest.mark.parametrize(
    "limits,reason",
    [
        (ListLimits(max_pages=3), "page_cap_reached"),
        (ListLimits(max_rows=40), "row_cap_reached"),
    ],
)
def test_caps_halt_rather_than_truncate(limits, reason):
    t = Worktable(make_rows(200))
    with pytest.raises(ListHalt) as exc:
        list(iterate_list(t, t, SCHEMA, limits=limits))
    assert exc.value.reason == reason


def test_empty_list_ends_cleanly():
    t = Worktable([])
    walk = ListWalk()
    assert list(iterate_list(t, t, SCHEMA, walk=walk)) == []
    assert walk.ended_by == "empty_page"


# -- capped searches (more than 1,000 results) ------------------------------

CAP = 1_000


class SearchApp:
    """Search by opened-date range; shows at most CAP results."""

    def __init__(self, rows):
        self.rows = rows
        self.searches = 0

    def matching(self, r):
        lo, hi = r
        return [x for x in self.rows if lo.isoformat() <= x["opened"] <= hi.isoformat()]

    def run_search(self, r) -> SearchResult:
        self.searches += 1
        n = len(self.matching(r))
        return SearchResult(count=min(n, CAP), truncated=n > CAP)

    def open(self, r):
        shown = self.matching(r)[:CAP]
        t = Worktable(shown, page_size=50)
        return t, t


def test_search_over_cap_is_partitioned_and_complete():
    rows = make_rows(4_321)
    app = SearchApp(rows)
    whole = (START, START + timedelta(days=1499))
    parts = partition_search(app.run_search, whole, split_date_range)
    assert len(parts) > 1
    assert all(len(app.matching(p)) <= CAP for p in parts)
    walk = ListWalk()
    got = list(iterate_partitioned(app.open, parts, SCHEMA, walk=walk))
    assert len(got) == 4_321
    assert {r.values["account_no"] for r in got} == {r["account_no"] for r in rows}


def test_unsplittable_truncated_range_halts():
    # 1,200 accounts opened on one day: no date split can get under the cap.
    rows = [dict(r, opened=START.isoformat()) for r in make_rows(1_200)]
    app = SearchApp(rows)
    with pytest.raises(ListHalt) as exc:
        partition_search(
            app.run_search, (START, START + timedelta(days=10)), split_date_range
        )
    assert exc.value.reason == "partition_unsplittable"


def test_split_date_range():
    assert split_date_range((START, START)) is None
    a, b = split_date_range((START, START + timedelta(days=9)))
    assert a == (START, START + timedelta(days=4))
    assert b == (START + timedelta(days=5), START + timedelta(days=9))


# -- focus and modal checks before copy -------------------------------------


class Screen:
    def __init__(self):
        self.focus = "Accounts - Ledger"
        self.modal = None  # name of the modal showing, if any
        self.escapes = 0

    def press_escape(self):
        self.escapes += 1
        if self.modal == "Tip of the day":
            self.modal = None


def _guard(screen, **kw):
    return CopyGuard(
        focused_window=lambda: screen.focus,
        expected_window=r"Accounts - .*",
        known_modals=[
            KnownModal(
                "Tip of the day",
                detect=lambda: screen.modal == "Tip of the day",
                dismiss=screen.press_escape,
                reversible=True,
            ),
            KnownModal(
                "Confirm delete", detect=lambda: screen.modal == "Confirm delete"
            ),
        ],
        modal_open=lambda: screen.modal,
        **kw,
    )


def test_guard_passes_when_focused_and_clear():
    s = Screen()
    _guard(s).check()


def test_guard_dismisses_reversible_known_modal():
    s = Screen()
    s.modal = "Tip of the day"
    g = _guard(s)
    g.check()
    assert s.escapes == 1 and g.dismissed == ["Tip of the day"]


def test_guard_halts_on_known_irreversible_modal():
    s = Screen()
    s.modal = "Confirm delete"
    with pytest.raises(ListHalt) as exc:
        _guard(s).check()
    assert exc.value.reason == "modal_present" and s.escapes == 0


def test_guard_halts_on_unrecognized_modal():
    s = Screen()
    s.modal = "Session expires in 2 minutes"
    with pytest.raises(ListHalt) as exc:
        _guard(s).check()
    assert exc.value.reason == "modal_present"


def test_guard_halts_when_focus_lost():
    s = Screen()
    s.focus = "Mail"
    with pytest.raises(ListHalt) as exc:
        _guard(s).check()
    assert exc.value.reason == "focus_lost"


def test_guard_halts_when_focus_unknown():
    s = Screen()
    s.focus = None
    with pytest.raises(ListHalt) as exc:
        _guard(s).check()
    assert exc.value.reason == "focus_lost"


def test_guard_halts_when_modal_keeps_returning():
    s = Screen()

    class Sticky:
        def detect(self):
            return True

    g = CopyGuard(
        focused_window=lambda: s.focus,
        expected_window="Accounts - Ledger",
        known_modals=[
            KnownModal(
                "Nag", detect=Sticky().detect, dismiss=lambda: None, reversible=True
            )
        ],
    )
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_not_cleared"


def test_guard_runs_before_every_page_read():
    rows = make_rows(100)
    t = Worktable(rows)
    s = Screen()
    checks = []
    g = _guard(s)
    orig = g.check

    def counting():
        checks.append(t.reads)
        orig()

    g.check = counting  # type: ignore[method-assign]
    list(iterate_list(t, t, SCHEMA, guard=g))
    # Checked before and after every read.
    assert t.reads == 4 and checks == [0, 1, 1, 2, 2, 3, 3, 4]
    # A modal that appears mid-walk halts before the next read.
    t2 = Worktable(rows)
    s2 = Screen()
    g2 = _guard(s2)

    class Pager:
        def next_page(self):
            ok = t2.next_page()
            if t2.page == 2:
                s2.modal = "Confirm delete"
            return ok

    with pytest.raises(ListHalt):
        list(iterate_list(t2, Pager(), SCHEMA, guard=g2))
    assert t2.reads == 2


def test_modal_during_copy_rereads_the_page():
    rows = make_rows(30)
    s = Screen()

    class Popping(Worktable):
        def read_page(self):
            page = super().read_page()
            if self.reads == 1:
                s.modal = "Tip of the day"  # appears during the first copy
            return page

    t = Popping(rows)
    got = list(iterate_list(t, t, SCHEMA, guard=_guard(s)))
    assert len(got) == 30
    assert t.reads == 3  # page 1 read again after the dismissal, then page 2


def test_empty_page_after_the_pager_advanced_halts():
    t = Worktable(make_rows(30))
    real = t.read_page
    t.read_page = lambda: [] if t.page == 1 else real()  # type: ignore[method-assign]
    with pytest.raises(ListHalt) as exc:
        list(iterate_list(t, t, SCHEMA))
    assert exc.value.reason == "pager_stalled"


def test_conflicting_page_yields_none_of_its_rows():
    rows = make_rows(3)
    clash = dict(rows[0], holder="Someone Else")
    t = Worktable([*rows, clash], page_size=10)
    got = []
    with pytest.raises(ListHalt) as exc:
        for row in iterate_list(t, t, SCHEMA):
            got.append(row)
    assert exc.value.reason == "duplicate_identity_conflict"
    assert got == []


def test_partition_rechecked_when_walked():
    from openadapt_flow.runtime.list_source import SearchResult

    rows = make_rows(10)

    def opened(result):
        def open_partition(_part):
            t = Worktable(rows)
            return t, t, result

        return open_partition

    with pytest.raises(ListHalt) as exc:
        list(iterate_partitioned(opened(SearchResult(None, True)), ["a"], SCHEMA))
    assert exc.value.reason == "partition_truncated"
    with pytest.raises(ListHalt) as exc:
        list(iterate_partitioned(opened(SearchResult(11, False)), ["a"], SCHEMA))
    assert exc.value.reason == "partition_count_mismatch"
    assert (
        len(list(iterate_partitioned(opened(SearchResult(10, False)), ["a"], SCHEMA)))
        == 10
    )
