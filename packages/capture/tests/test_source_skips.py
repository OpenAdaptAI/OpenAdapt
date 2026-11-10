"""Policy-dropped source ordinals are recorded, never silently missing."""

from __future__ import annotations

import queue

import pytest

from openadapt_capture import window_capture
from openadapt_capture.recorder import (
    Event,
    EventJournalOrderingError,
    OrderedEventJournal,
)
from openadapt_capture.source_skips import (
    INPUT_CONTINUES_DROPPED_INPUT,
    SOURCE_ORDINAL_SKIP_REASONS,
    SOURCE_ORDINAL_SKIPS_FILENAME,
    SourceOrdinalSkipError,
    build_source_ordinal_skips,
    load_source_ordinal_skips,
    write_source_ordinal_skips,
)


def test_skip_reasons_are_exactly_the_window_input_drop_reasons():
    drop_reasons = {
        window_capture.INPUT_KEYBOARD_FOCUS_ELSEWHERE,
        window_capture.INPUT_KEYBOARD_FOCUS_IN_DIALOG,
        window_capture.INPUT_KEYBOARD_FOCUS_UNKNOWN,
        window_capture.INPUT_POINTER_OVER_OTHER_WINDOW,
        window_capture.INPUT_POINTER_TARGET_UNKNOWN,
        window_capture.INPUT_TARGET_NOT_ON_SCREEN,
        INPUT_CONTINUES_DROPPED_INPUT,
    }
    assert SOURCE_ORDINAL_SKIP_REASONS == drop_reasons


def test_skips_merge_into_canonical_runs_and_round_trip(tmp_path):
    skips = [
        (7, "pointer_over_other_window"),
        (2, "pointer_over_other_window"),
        (3, "pointer_over_other_window"),
        (4, "continues_dropped_input"),
    ]
    record = build_source_ordinal_skips(skips)
    assert [(run.first, run.last, run.reason) for run in record.runs] == [
        (2, 3, "pointer_over_other_window"),
        (4, 4, "continues_dropped_input"),
        (7, 7, "pointer_over_other_window"),
    ]
    path = write_source_ordinal_skips(tmp_path, skips)
    assert path == tmp_path / SOURCE_ORDINAL_SKIPS_FILENAME
    assert load_source_ordinal_skips(tmp_path) == dict(skips)
    # Only reason codes and ordinals are stored.
    assert set(path.read_text()) <= set('{}[]":,_/.\n0123456789abcdefghijklmnopqrstuvwxyz-')
    assert [p.name for p in tmp_path.iterdir()] == [SOURCE_ORDINAL_SKIPS_FILENAME]


def test_no_skips_writes_nothing(tmp_path):
    assert write_source_ordinal_skips(tmp_path, []) is None
    assert list(tmp_path.iterdir()) == []
    assert load_source_ordinal_skips(tmp_path) == {}


def test_skip_record_is_create_only(tmp_path):
    write_source_ordinal_skips(tmp_path, [(2, "pointer_over_other_window")])
    with pytest.raises(SourceOrdinalSkipError, match="already exists"):
        write_source_ordinal_skips(tmp_path, [(3, "pointer_over_other_window")])
    assert load_source_ordinal_skips(tmp_path) == {2: "pointer_over_other_window"}


@pytest.mark.parametrize(
    "skips",
    [
        [(2, "crashed")],
        [(2, "pointer_over_other_window"), (2, "keyboard_focus_elsewhere")],
        [(0, "pointer_over_other_window")],
    ],
    ids=["unknown-reason", "duplicate", "zero"],
)
def test_invalid_skips_are_refused(tmp_path, skips):
    with pytest.raises(SourceOrdinalSkipError):
        write_source_ordinal_skips(tmp_path, skips)
    assert list(tmp_path.iterdir()) == []


def test_journal_records_each_policy_drop_it_consumes():
    journal = OrderedEventJournal()
    first = journal.reserve(1.0)
    dropped = journal.reserve(2.0)
    third = journal.reserve(3.0)
    trailing = journal.reserve(4.0)
    first.complete(Event(1.0, "action", {}))
    dropped.discard("pointer_over_other_window")
    third.complete(Event(3.0, "action", {}))
    trailing.discard(INPUT_CONTINUES_DROPPED_INPUT)

    assert journal.get(timeout=1).source_ordinal == 1
    assert journal.get(timeout=1).source_ordinal == 3
    assert journal.empty()
    with pytest.raises(queue.Empty):
        journal.get(block=False)
    assert journal.skipped_source_ordinals == (
        (2, "pointer_over_other_window"),
        (4, INPUT_CONTINUES_DROPPED_INPUT),
    )
    assert journal.last_source_ordinal == 4


def test_journal_refuses_a_drop_without_a_policy_reason():
    journal = OrderedEventJournal()
    reservation = journal.reserve(1.0)
    with pytest.raises(EventJournalOrderingError, match="skip reason"):
        reservation.discard("crashed")
    assert not reservation.finished
    assert journal.skipped_source_ordinals == ()


def test_journal_never_records_a_failed_producer_as_a_skip():
    from openadapt_capture.recorder import EventJournalReservationError

    journal = OrderedEventJournal()
    reservation = journal.reserve(1.0)
    reservation.fail(RuntimeError("producer crashed"))
    with pytest.raises(EventJournalReservationError):
        journal.get(timeout=1)
    assert journal.skipped_source_ordinals == ()
