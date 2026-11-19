"""Screen text read from the clock is checked against the run's clock.

The bug this pins: the certified OpenEMR referral program checked that the date
picker showed the literal "October-2026-" (the month it was recorded in), so
from 1 November every referral halted before saving. The class fix compiles a
text whose dates all equal the recording date into a clock template and checks
the run clock's rendering of it, digit for digit: another month still fails.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openadapt_flow import clock_text as ct
from openadapt_flow import volatility
from openadapt_flow.compiler import clock_pass
from openadapt_flow.ir import Postcondition, PostconditionKind
from openadapt_flow.runtime.replayer import Replayer

RECORDED = date(2026, 10, 20)  # a Tuesday


class TestDetection:
    """OCR-joined month-year text is a date, not a bare month word."""

    @pytest.mark.parametrize(
        "text", ["October-2026-", "October2026", "Oct-2026", "OCTOBER/2026"]
    )
    def test_ocr_joined_month_year_is_a_near_date(self, text: str) -> None:
        assert volatility.classify_text(text, reference_date=RECORDED) == "near_date"

    def test_far_joined_month_year_stays_stable(self) -> None:
        assert volatility.classify_text("March-1958-", reference_date=RECORDED) is None

    def test_bare_month_words_are_still_not_dates(self) -> None:
        assert not volatility.has_date_fragment("May I help you")
        assert not volatility.has_date_fragment("Marketing")
        assert not volatility.has_date_fragment("Separate")


class TestCompile:
    @pytest.mark.parametrize(
        "text,template",
        [
            ("October-2026-", "{Month}-{YYYY}-"),
            ("October 2026", "{Month} {YYYY}"),
            ("OCTOBER 2026", "{MONTH} {YYYY}"),
            ("Oct 20, 2026", "{Mon} {D?}, {YYYY}"),
            ("Tuesday, October 20, 2026", "{Weekday}, {Month} {D?}, {YYYY}"),
            ("Tue Oct 20", "{Wkd} {Mon} {D?}"),
            ("10/20/2026", "{M?}/{D?}/{YYYY}"),
            ("20.10.26", "{D?}.{M?}.{YY}"),
            ("2026-10-20", "{YYYY}-{M?}-{D?}"),
            ("Visit on 2026-10-20", "Visit on {YYYY}-{M?}-{D?}"),
        ],
    )
    def test_dates_on_the_recording_day_compile(self, text: str, template: str) -> None:
        assert ct.compile_clock_text(text, RECORDED) == template
        assert text in ct.render(template, RECORDED)

    @pytest.mark.parametrize(
        "text",
        [
            "October 2025",  # another year
            "Oct 18, 2026",  # near the recording day, not on it: chronology
            "Monday, October 20, 2026",  # wrong weekday for that date
            "DOB 1958-03-22",  # far: identity data, stays literal
            "10/10/2026",  # day equals month: field order undecidable
            "Updated 14:32 Oct 20",  # also a clock time
            "Sept 20, 2026",  # a spelling no token renders
            "Save",  # no date at all
            "October {x} 2026",  # literal braces in recorded text
        ],
    )
    def test_other_texts_do_not_compile(self, text: str) -> None:
        assert ct.compile_clock_text(text, RECORDED) is None

    def test_no_recording_date_compiles_nothing(self) -> None:
        assert ct.compile_clock_text("October 2026", None) is None


class TestRender:
    def test_renders_the_run_month(self) -> None:
        assert ct.render("{Month}-{YYYY}-", date(2026, 11, 19)) == ["November-2026-"]

    def test_december_to_january_rolls_the_year(self) -> None:
        assert ct.render("{Month} {YYYY}", date(2027, 1, 2)) == ["January 2027"]
        assert ct.render("{Mon} {D?}, {YYYY}", date(2027, 1, 2)) == [
            "Jan 02, 2027",
            "Jan 2, 2027",
        ]

    def test_every_rendering_names_the_same_date(self) -> None:
        on = date(2026, 3, 4)
        for text in ct.render("{M?}/{D?}/{YYYY}", on):
            assert datetime.strptime(text, "%m/%d/%Y").date() == on

    @pytest.mark.parametrize(
        "template", ["no tokens", "{Bogus}", "{YYYY:>8}", "{YYYY!r}", "{", "{D?}" * 5]
    )
    def test_bad_templates_are_refused(self, template: str) -> None:
        with pytest.raises(ct.ClockTemplateError):
            ct.template_tokens(template)

    def test_run_clock_dates_cover_local_and_utc_near_midnight(self) -> None:
        late = datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert ct.run_clock_dates(late) == [date(2026, 11, 30), date(2026, 12, 1)]
        noon = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
        assert len(ct.run_clock_dates(noon)) == 1

    @pytest.mark.parametrize(
        "template,safe",
        [
            ("{Month}-{YYYY}-", True),
            ("{Mon} {D?}", False),  # "Nov 3" inside "Nov 30"
            ("{M}/{D}/{YYYY}", False),  # "1/3/2026" inside "11/3/2026"
            ("{MM}/{DD}/{YYYY}", True),
            ("({D?} {Mon})", False),
            ("Day {DD}", True),
        ],
    )
    def test_substring_safety(self, template: str, safe: bool) -> None:
        assert ct.substring_safe(template) is safe


class TestPostconditionField:
    def test_unset_field_keeps_old_serialization(self) -> None:
        pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")
        assert "text_clock" not in pc.model_dump()

    def test_template_is_validated(self) -> None:
        with pytest.raises(ValidationError):
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="x", text_clock="{Nope}"
            )
        with pytest.raises(ValidationError):
            Postcondition(kind=PostconditionKind.REGION_STABLE, text_clock="{YYYY}")


class _Vision:
    """OCR stand-in: reads exactly the lines on the 'screen'."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def exact_text_count(self, _png: bytes, text: str) -> int:
        from openadapt_flow.vision.ocr import text_key

        return sum(1 for line in self.lines if text_key(line) == text_key(text))

    def text_present(self, _png: bytes, text: str) -> bool:
        # Deliberately fuzzy, like OCR: 'October 2025' reads as 'October 2026'.
        import difflib

        return any(
            difflib.SequenceMatcher(None, line.lower(), text.lower()).ratio() >= 0.8
            for line in self.lines
        )


def _replayer(lines: list[str], now: datetime) -> Replayer:
    replayer = Replayer.__new__(Replayer)
    replayer.vision = _Vision(lines)
    replayer.backend = SimpleNamespace()  # no DOM seam
    replayer.clock = lambda: now
    return replayer


PICKER = Postcondition(
    kind=PostconditionKind.TEXT_PRESENT,
    text="October-2026-",
    text_clock="{Month}-{YYYY}-",
)
NOV = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class TestReplayCheck:
    """The same compiled check, run in another month."""

    def test_passes_when_the_screen_shows_the_run_month(self) -> None:
        replayer = _replayer(["DOB:", "November-2026-", "Sun"], NOV)
        assert replayer._postcondition_passes(PICKER, b"", None)

    def test_refuses_the_recorded_month_in_another_month(self) -> None:
        replayer = _replayer(["DOB:", "October-2026-"], NOV)
        assert not replayer._postcondition_passes(PICKER, b"", None)
        assert "November-2026-" in replayer._describe_postcondition(PICKER)

    def test_refuses_a_near_miss_the_fuzzy_reader_would_accept(self) -> None:
        replayer = _replayer(["November-2025-"], NOV)
        assert replayer.vision.text_present(b"", "November-2026-")  # fuzzy: yes
        assert not replayer._postcondition_passes(PICKER, b"", None)

    def test_rolls_over_the_year(self) -> None:
        jan = datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc)
        assert _replayer(["January-2027-"], jan)._postcondition_passes(
            PICKER, b"", None
        )
        assert not _replayer(["January-2026-"], jan)._postcondition_passes(
            PICKER, b"", None
        )

    def test_literal_check_still_fails_in_another_month(self) -> None:
        """The pre-fix bundle (no template) halts in November: the bug."""
        literal = PICKER.model_copy(update={"text_clock": None})
        replayer = _replayer(["November-2026-"], NOV)
        assert not replayer._postcondition_passes(literal, b"", None)

    def test_dom_substring_proof_is_not_used_for_open_ended_numbers(self) -> None:
        pc = Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Oct 20",
            text_clock="{Mon} {D?}",
        )
        replayer = _replayer([], datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
        replayer.backend = SimpleNamespace(visible_text_present=lambda _t: True)
        assert not replayer._postcondition_passes(pc, b"", None)

    def test_text_absent_must_be_absent_in_every_rendering(self) -> None:
        pc = Postcondition(
            kind=PostconditionKind.TEXT_ABSENT,
            text="Oct 20, 2026",
            text_clock="{Mon} {D?}, {YYYY}",
        )
        at = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        assert _replayer(["Nothing due"], at)._postcondition_passes(pc, b"", None)
        assert not _replayer(["Nov 3, 2026"], at)._postcondition_passes(
            pc, b"", None
        )


class TestBundlePass:
    def test_the_pass_templates_only_clock_texts(self) -> None:
        from openadapt_flow.ir import ActionKind, Step, Workflow

        steps = [
            Step(
                id="step_001",
                intent="open the picker",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="October-2026-"),
                    Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Date of birth"),
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="October 2026",
                        exact=True,
                    ),
                ],
            )
        ]
        workflow = Workflow(name="w", steps=steps)
        new_steps, changes = clock_pass.relativize_clock_texts(workflow, RECORDED)
        assert changes == ["step_001: 'October-2026-' -> '{Month}-{YYYY}-'"]
        clocks = [pc.text_clock for pc in new_steps[0].expect]
        assert clocks == ["{Month}-{YYYY}-", None, None]
        assert workflow.steps[0].expect[0].text_clock is None  # input unchanged
