"""Unit tests for openadapt_flow.volatility — the stability classifier that
postcondition mining and identity-context extraction share.

The concrete failures these pin down (docs/validation/VALIDATION.md):

- a live OpenEMR recording mined ``text_present ':01'`` (a clock-minute OCR
  fragment) and every later replay false-halted on it;
- the old blanket timestamp filter ate identity banners because a DOB looks
  like a date;
- the 2026-07-09 review verified that month-name dates ('Jul 8, 2026'),
  relative times ('3 min ago'), badge counters ('Inbox (2)'), count phrases
  ('56 total entries'), dot-clocks ('18.38') and pagination position
  ('Page 2 of 9') all evaded the numeric-only patterns and classified as
  stable — OpenEMR's post-login calendar alone means a mined 'July 2026'
  header false-halts every replay the next month. Every reviewer probe
  string is pinned below, in both directions.
"""

from __future__ import annotations

from datetime import date

import pytest

from openadapt_flow.volatility import (
    CLOCK_RE,
    DATE_VOLATILITY_WINDOW_DAYS,
    classify_text,
    date_fragments_near,
    is_volatile_line,
)

RECORDED = date(2026, 7, 8)  # the reference (recording) date used throughout


class TestClockFragments:
    """Clock times are volatile at replay latency — always rejected."""

    @pytest.mark.parametrize(
        "text",
        [
            ":01",  # the OpenEMR false-halt fragment, verbatim
            "18:38",
            "6:05",
            "12:45:59",
            "Last updated 14:32",
            "Message received at 9:07 today",
        ],
    )
    def test_clock_bearing_text_is_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is not None

    def test_the_openemr_colon_fragment_never_survives(self) -> None:
        """The ':01'-class fragment must be volatile via EVERY route: the
        clock regex, the length floor, and the digit-dominance rule."""
        assert CLOCK_RE.search(":01")
        assert classify_text(":01", reference_date=RECORDED) == "too_short"
        assert classify_text("x :01 y", reference_date=RECORDED) == "clock_time"

    def test_clock_line_is_volatile_for_identity_context(self) -> None:
        assert is_volatile_line("12:45", reference_date=RECORDED)
        assert is_volatile_line("Updated 14:32", reference_date=RECORDED)


class TestCountersAndNoise:
    @pytest.mark.parametrize(
        "text,reason",
        [
            ("42", "too_short"),
            ("#1234", "digit_dominated"),
            ("(3)", "too_short"),
            ("12345", "digit_dominated"),
            ("3 / 12", "digit_dominated"),
            ("----", "digit_dominated"),
            ("aaaa", "low_entropy"),
            ("OK", "too_short"),
        ],
    )
    def test_fragments_are_volatile(self, text: str, reason: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == reason

    @pytest.mark.parametrize(
        "text",
        [
            "No encounters yet.",
            "Encounter saved",
            "Patient Messages",
            "Chart synchronization complete",
        ],
    )
    def test_real_ui_text_is_stable(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is None

    def test_pagination_position_is_volatile(self) -> None:
        """'Page 2 of 9' is navigation state, not identity: the position
        changes with data volume on shared instances (reclassified from
        stable on 2026-07-09 review)."""
        assert classify_text("Page 2 of 9", reference_date=RECORDED) == "count"
        assert is_volatile_line("Page 2 of 9", reference_date=RECORDED)


class TestMonthNameDates:
    """Month-name dates evaded the numeric-only DATE_RE (reviewer probes)."""

    @pytest.mark.parametrize(
        "text",
        [
            "Jul 8, 2026",
            "08 Jul 2026",
            "July 2026",  # the OpenEMR post-login calendar header class
            "Wednesday July 8",
        ],
    )
    def test_near_month_dates_are_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == "near_date"
        assert is_volatile_line(text, reference_date=RECORDED)

    def test_month_day_without_year_is_always_volatile(self) -> None:
        """'Updated Jul 8' recurs annually — chronology, never identity."""
        assert classify_text("Updated Jul 8", reference_date=RECORDED) == "near_date"
        assert is_volatile_line("Updated Jul 8", reference_date=RECORDED)

    def test_far_month_date_is_identity_data(self) -> None:
        """A month-name DOB in an identity region is kept, like numeric DOBs."""
        banner = "Jane Sample DOB: Jan 1, 1980"
        assert classify_text(banner, reference_date=RECORDED) is None
        assert not is_volatile_line(banner, reference_date=RECORDED)

    def test_far_month_year_is_stable(self) -> None:
        assert (
            classify_text("Member since January 1980", reference_date=RECORDED) is None
        )

    def test_bare_month_word_is_not_a_date(self) -> None:
        """'May' the modal verb / bare month word carries no attached digits."""
        assert classify_text("May I help you", reference_date=RECORDED) is None
        assert not is_volatile_line("May I help you", reference_date=RECORDED)

    def test_no_reference_date_treats_month_dates_as_volatile(self) -> None:
        assert classify_text("Jul 8, 2026") == "date"
        assert is_volatile_line("Jul 8, 2026")


class TestRelativeTime:
    @pytest.mark.parametrize(
        "text",
        [
            "3 min ago",
            "2 hours ago",
            "5 days ago",
            "just now",
            "yesterday",
            "Yesterday",
            "Tomorrow",
        ],
    )
    def test_relative_time_is_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == "relative_time"
        assert is_volatile_line(text, reference_date=RECORDED)

    def test_day_words_are_volatile_only_standalone(self) -> None:
        """'Today's Appointments' is chrome that always reads the same."""
        assert classify_text("Today's Appointments", reference_date=RECORDED) is None
        assert not is_volatile_line("Today's Appointments", reference_date=RECORDED)


class TestCountsAndCounters:
    ENTRIES_BANNER = "Showing 1 to 1 of 1 entries (filtered from 56 total entries)"

    @pytest.mark.parametrize(
        "text",
        [
            "56 total entries",
            "Page 2 of 9",
            "0 to 0 of 0 entries",
            "5 new messages",
        ],
    )
    def test_count_phrases_are_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == "count"
        assert is_volatile_line(text, reference_date=RECORDED)

    def test_entries_banner_is_rejected_at_compile_time(self) -> None:
        """The OpenEMR results banner (mined verbatim on 2026-07-09) must
        now be rejected as volatile: per-line fuzzy 0.8 scored the exact
        drift it should catch ('0 to 0 of 0 entries…') at 0.95 against it —
        a passing match on the wrong state — so the string may never become
        an assertion at all."""
        assert classify_text(self.ENTRIES_BANNER, reference_date=RECORDED) == "count"
        # The squashed form is what OCR actually returned on the live demo.
        squashed = "Showing1to1of1entries(filteredfrom56totalentries)"
        assert classify_text(squashed, reference_date=RECORDED) == "count"
        assert is_volatile_line(self.ENTRIES_BANNER, reference_date=RECORDED)

    @pytest.mark.parametrize("text", ["Inbox (2)", "Messages (14)"])
    def test_parenthesized_counters_are_volatile_decoration(self, text: str) -> None:
        """Strip-and-test: removing the parenthesized number leaves the
        classification unchanged, so the counter is live decoration and the
        composite must not be asserted (the label alone may still be)."""
        assert classify_text(text, reference_date=RECORDED) == "counter"
        assert is_volatile_line(text, reference_date=RECORDED)

    def test_counter_stripped_label_stays_stable(self) -> None:
        assert classify_text("Inbox", reference_date=RECORDED) is None


class TestDotClocks:
    @pytest.mark.parametrize(
        "text",
        [
            "Last updated 18.38",  # the reviewer's European dot-clock probe
            "18.38",
            "08.30",
            "updated 8.30",  # one-digit hour, time-ish context
            "8.30 pm",
        ],
    )
    def test_dot_clocks_are_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == "clock_time"
        assert is_volatile_line(text, reference_date=RECORDED)

    @pytest.mark.parametrize(
        "text",
        [
            "MyApp v2.0",  # single minute digit: not a clock
            "v2.10 changelog",  # leading letter blocks the hour
            "Version 2.10 release notes",  # one-digit hour, no time context
        ],
    )
    def test_version_numbers_are_not_clocks(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is None
        assert not is_volatile_line(text, reference_date=RECORDED)


class TestStableKept:
    """The other direction of every new rule: identity-bearing text with
    digits must survive classification."""

    @pytest.mark.parametrize(
        "text",
        [
            "Suite 300",
            "123 Main Street, Suite 300",
            "Jane Sample DOB 1980-01-01",  # numeric DOB, identity region
            "Jane Sample DOB: Jan 1, 1980",  # month-name DOB
            "Treatment Intervention Preferences",
            "Calendar Finder Flow Recalls Messages Patient Fees Modules",
        ],
    )
    def test_stable_text_is_kept(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is None
        assert not is_volatile_line(text, reference_date=RECORDED)


class TestDates:
    """Near dates are chronology (volatile); far dates are identity data."""

    def test_dob_far_from_recording_is_stable(self) -> None:
        banner = "Jane Sample DOB 1980-01-01"
        assert classify_text(banner, reference_date=RECORDED) is None
        assert not is_volatile_line(banner, reference_date=RECORDED)

    def test_date_near_recording_is_volatile(self) -> None:
        row = "Lab result posted 2026-07-07"
        assert classify_text(row, reference_date=RECORDED) == "near_date"
        assert is_volatile_line(row, reference_date=RECORDED)

    def test_two_digit_year_near_date_is_volatile(self) -> None:
        assert classify_text("Visit 8.7.26", reference_date=RECORDED) == "near_date"

    def test_mdy_and_dmy_orders_both_considered(self) -> None:
        # 07/08/2026 is near whether read M/D/Y or D/M/Y.
        assert date_fragments_near("07/08/2026", RECORDED) is True
        # 01/02/1980 is far under both readings.
        assert date_fragments_near("01/02/1980", RECORDED) is False

    def test_unparseable_date_like_text_is_conservatively_volatile(self) -> None:
        assert date_fragments_near("99/99/2026", RECORDED) is True

    def test_no_reference_date_treats_all_dates_as_volatile(self) -> None:
        """Heal-time re-extraction has no recording date: conservative."""
        assert classify_text("Jane Sample DOB 1980-01-01") == "date"
        assert is_volatile_line("Jane Sample DOB 1980-01-01")

    def test_bare_far_date_without_text_is_still_volatile(self) -> None:
        """A DOB is stable evidence only inside an identity region with real
        text; a bare date fragment has no semantics to anchor."""
        assert classify_text("1980-01-01", reference_date=RECORDED) == "digit_dominated"

    def test_window_boundary(self) -> None:
        near = RECORDED.toordinal() - DATE_VOLATILITY_WINDOW_DAYS
        far = near - 2
        near_s = date.fromordinal(near).isoformat()
        far_s = date.fromordinal(far).isoformat()
        assert date_fragments_near(near_s, RECORDED) is True
        assert date_fragments_near(far_s, RECORDED) is False

    def test_text_without_dates_returns_none(self) -> None:
        assert date_fragments_near("No encounters yet.", RECORDED) is None
