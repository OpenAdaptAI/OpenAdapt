"""The words that differ between the quickstart's apps.

MockMed (the default) breaks a save silently: the screen says saved and the
database has nothing. OpenEMR's Act 3 is a referral that matches an existing
chart: the program stops before saving. Everything else in the story and the
views is shared; a :class:`Scene` holds what is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openadapt.quickstart.engine import BreakFacts


@dataclass(frozen=True)
class Scene:
    """One app's words for the three acts."""

    #: The headline under the command.
    intro: str
    #: What the spinner says while the app starts.
    starting: str
    #: Act 1's subtitle (before ", in a hidden browser" when Act 1 is live).
    act1_where: str
    #: Act 2's inputs, plural ("notes": "the program on 5 new notes").
    inputs_noun: str
    #: Where each run is checked ("checked in the database").
    checked_in: str
    #: Act 3's subtitle.
    act3_subtitle: str
    #: The line before Act 3 runs.
    break_setup: str
    #: The prompt before Act 3 runs, on a terminal.
    confirm_prompt: str
    #: What the spinner says while Act 3 runs.
    breaking: str
    #: The record's label in Act 3 ("What the database showed").
    record_label: str
    #: What one record is called ("record": "0 new records").
    record_noun: str
    #: OpenAdapt's verdict when Act 3 stopped.
    stopped: str
    #: The record check's subject in the receipt ("in the database").
    receipt_where: str
    #: What Act 3's screen showed when its text couldn't be read, said only
    #: when the run observed it (see :meth:`screen_line`).
    screen_default: str = "a saved message"
    #: What the screen row says when the run observed neither the text nor
    #: the screen this act expects.
    screen_unread: str = "not read (see the screenshot in the report)"
    #: The part of the 1280x800 screenshot Act 3 shows (left, top, right,
    #: bottom).
    screen_crop: tuple[int, int, int, int] = (0, 0, 640, 320)
    #: True when Act 3's screen claims success (MockMed's silent failure), so
    #: a screen-trusting bot's "Done" and the one-line summary apply.
    screen_claims_success: bool = True
    #: True when Act 1 demonstrates in a browser now; False when it shows the
    #: recorded demonstration a certified program was compiled from.
    act1_live: bool = True
    #: How Act 1 ends, given the program's step count.
    compiled_template: str = "Compiled into a program: {steps} steps."
    #: Why OpenAdapt stopped, when the run shows it stopped at this act's
    #: check and nothing was saved.
    why_stopped: str = (
        "OpenAdapt reads the record back from the database after each save. "
        "It found nothing, so it stopped."
    )
    #: Why, when it stopped at this act's check but a record was saved.
    why_saved_anyway: str = (
        "OpenAdapt read the record back, and it didn't match, so it stopped."
    )
    #: The one-line summary for a screen that doesn't claim success.
    aha_stopped: str = ""

    def compiled_line(self, steps: int) -> str:
        return self.compiled_template.format(steps=steps)

    def database_line(self, broken: "BreakFacts") -> str:
        count = broken.new_records
        return f"{count} new {self.record_noun}" + ("" if count == 1 else "s")

    def screen_observed(self, broken: "BreakFacts") -> bool:
        """True when the run observed the screen this act describes."""

        if self.screen_claims_success:
            return broken.screen_showed_success
        return broken.stop_attributed

    def screen_words(self, broken: "BreakFacts") -> str:
        """What the screen showed: the text read off it, else only what the
        run observed, else that it wasn't read."""

        if broken.screen_text:
            return broken.screen_text
        if self.screen_observed(broken):
            return self.screen_default
        return self.screen_unread

    def screen_line(self, broken: "BreakFacts") -> str:
        """:meth:`screen_words`, with text read off the screen in quotes."""

        if broken.screen_text:
            return f'"{broken.screen_text}"'
        return self.screen_words(broken)

    def verdict(self, broken: "BreakFacts") -> str:
        """OpenAdapt's verdict when it stopped (``stopped`` only if attributed)."""

        if broken.stop_attributed and (
            self.screen_claims_success or broken.new_records == 0
        ):
            return self.stopped
        return "Stopped"

    def why_line(self, broken: "BreakFacts") -> str:
        if not broken.stopped:
            return "OpenAdapt did not stop. Treat this build as broken."
        if not broken.stop_attributed:
            if broken.new_records:
                return (
                    f"OpenAdapt stopped, but a new {self.record_noun} was saved. "
                    "Check it."
                )
            return (
                "OpenAdapt stopped, but not at the check this act sets off, so "
                "this run doesn't show that check. The report says why it stopped."
            )
        if broken.new_records == 0:
            return self.why_stopped
        return self.why_saved_anyway

    def aha_line(self, broken: "BreakFacts") -> Optional[str]:
        """The one-line summary, only when the run showed all of it."""

        if not (broken.stopped and broken.stop_attributed and broken.new_records == 0):
            return None
        if not self.screen_claims_success:
            return self.aha_stopped or None
        if not broken.screen_showed_success:
            return None
        text = broken.screen_text
        end = text.lower().find("saved") if text else -1
        if text and end >= 0:
            screen = f'The screen said "{text[: end + len("saved")]}".'
        else:
            screen = "The screen showed its saved message."
        return (
            f"{screen} The database had nothing. A bot that trusts the screen "
            "reports Done. OpenAdapt stopped."
        )

    def break_detail(self, broken: "BreakFacts") -> list[str]:
        """Act 3's engine detail, for ``--verbose``."""

        if not self.screen_claims_success:
            first = (
                f"case {broken.fault!r}: OpenEMR already has a chart with this name"
                + (
                    ", and its duplicate check listed a match"
                    if broken.stop_attributed
                    else "; the halt doesn't show the duplicate check listing it"
                )
            )
            ran = (
                " (the save step never ran)"
                if broken.transaction_outcome == "HALTED_BEFORE_EFFECT"
                else ""
            )
            checks = f"record checks {broken.effects_required} declared{ran}"
        else:
            first = (
                f"fault mode {broken.fault!r}: the server rejects the write after "
                "the app shows its saved message"
            )
            checks = (
                f"record checks refuted "
                f"{broken.effects_refuted}/{broken.effects_required}"
            )
        return [
            first,
            f"execution {broken.execution_outcome}, transaction "
            f"{broken.transaction_outcome}, {checks}",
            f"engine: {broken.halt_reason}",
            f"report {broken.report_md}",
        ]


MOCKMED = Scene(
    intro=("show a task once, run it, then watch it catch a save that didn't happen."),
    starting="Starting the synthetic clinic app",
    act1_where="a demonstration in a synthetic clinic app",
    inputs_noun="notes",
    checked_in="checked in the database",
    act3_subtitle="the next save fails silently",
    break_setup=(
        "The app will show its saved message. The database will reject the save."
    ),
    confirm_prompt="Press Enter to make the next save fail silently",
    breaking="Running the program once more, checking the database",
    record_label="What the database showed",
    record_noun="record",
    stopped="Stopped before reporting success",
    receipt_where="in the database",
)

OPENEMR = Scene(
    intro=(
        "show a task once in OpenEMR, run it, then watch it stop before saving a "
        "duplicate chart."
    ),
    starting="Starting OpenEMR",
    act1_where="a referral as it was demonstrated once in OpenEMR 8.0.0.3",
    inputs_noun="referrals",
    checked_in="checked in OpenEMR's record",
    act3_subtitle="a referral that matches an existing chart",
    break_setup=(
        "OpenEMR already has a chart for Rosa Delgado. The next referral is a "
        "Rosa Delgado with another birth date."
    ),
    confirm_prompt="Press Enter to run the program on that referral",
    breaking="Running the program on the referral, then reading OpenEMR's charts",
    record_label="What OpenEMR's record showed",
    record_noun="chart",
    stopped="Stopped before saving: possible duplicate",
    receipt_where="in OpenEMR's record",
    screen_claims_success=False,
    act1_live=False,
    compiled_template=(
        "The program compiled from it: {steps} steps, certified under clinical-write."
    ),
    why_stopped=(
        "OpenEMR's duplicate check found an existing chart. The program saves "
        "only when it finds none, so it stopped before saving."
    ),
    why_saved_anyway="OpenAdapt stopped, but a new chart is in OpenEMR. Check it.",
    aha_stopped=(
        "The demonstration clicked Confirm Create New Patient here. Replaying "
        "those clicks would have saved a possible duplicate. OpenAdapt stopped."
    ),
    screen_default="OpenEMR's duplicate check, listing a matching chart",
    screen_crop=(204, 30, 1078, 262),
)

SCENES = {"mockmed": MOCKMED, "openemr": OPENEMR}


__all__ = ["MOCKMED", "OPENEMR", "SCENES", "Scene"]
