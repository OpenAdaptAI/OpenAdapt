"""Track B — mid-run fault injection (adversarial validation).

Sabotages MockMed state BETWEEN steps of a live replay via ``ChaosBackend``
and pins down halt-vs-improvise behavior. The silent wrong-patient save and
the silent empty-note save originally found here were FIXED on 2026-07-08
(pre-click identity check + typed-input verification); their tests now pin
the safe-halt / recovery behavior. See ``docs/validation/VALIDATION.md``
for the ranked matrix.

Click order in the canonical demo: 1=username 2=password 3=sign-in 4=Open
5=New Encounter 6=Triage 7=note field 8=Save. Type order: 1=username
2=password 3=note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend

from .conftest import PARAMS
from .validation_utils import ChaosBackend, describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)


def chaos(inject, *, after_click=None, after_type=None):
    """Backend factory wiring an injection to a click/type trigger."""

    def factory(page) -> PlaywrightBackend:
        return ChaosBackend(
            page, inject=inject, after_click=after_click, after_type=after_type
        )

    return factory


def run(bundle, mockmed_url, _browser, tmp_path: Path, factory):
    return replay_on_page(
        _browser,
        bundle.dir,
        mockmed_url,
        tmp_path / "run",
        params=dict(PARAMS),
        backend_factory=factory,
    )


class TestEntityDeletedMidRun:
    def test_target_row_deleted_after_login_safe_halts(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """FIXED (was: wrong-action, silent — saved to '#patient/p2').
        Between signing in and clicking 'Open', the target patient's
        referral row is deleted (as if another user claimed it). The next
        patient's row slides into the recorded position and every rung
        resolves to it — but the pre-click identity check reads 'Alex
        Testcase Cardiology follow-up Medium' where 'Jane Sample Knee pain
        referral High' was recorded and halts the run without clicking:
        the mid-run twin of the drift=missing perturbation."""

        def inject(page):
            page.wait_for_selector("#open-p1", timeout=5000)
            page.evaluate("document.getElementById('open-p1').closest('tr').remove()")

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_click=3),
        )
        assert report.success is False, describe(report, state)
        assert state["hash"] == "#tasks", describe(report, state)  # no click
        assert state["banner"] is None  # nothing saved
        failed = failing_step(report)
        assert failed is not None and failed.step_id == "step_005"
        assert "Identity check failed" in (failed.error or "")
        assert failed.identity is not None
        assert failed.identity.status == "mismatch"


class TestBlockingModalMidRun:
    def test_opaque_overlay_before_save_halts_without_clicking(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT. An opaque 'maintenance' overlay appears after the
        note is typed. Focus-bound exact readback confirms delivery even though
        the cover hides the field from hit-testing; save resolution then fails
        before a click is fired into the overlay."""

        def inject(page):
            page.evaluate(
                "var d = document.createElement('div');"
                "d.id = 'chaos-overlay';"
                "d.style.cssText = 'position:fixed;inset:0;background:#222a35;"
                "color:#fff;z-index:9999;display:flex;align-items:center;"
                "justify-content:center;font-size:28px';"
                "d.textContent = 'System maintenance in progress';"
                "document.body.appendChild(d);"
            )

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_type=3),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_010", describe(report, state)
        assert failed.resolution is None  # halted BEFORE acting
        assert "Could not resolve" in (failed.error or "")

    def test_invisible_click_shield_before_save_halts_after_wasted_click(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT (with one neutralized click). A fully transparent
        overlay intercepts pointer events. Vision sees an unchanged screen,
        resolves the save button, and clicks — into the shield. Nothing
        happens, postconditions fail, the run aborts. No state was written,
        but note the runtime cannot tell 'button clicked and app ignored
        it' from 'click never reached the app'."""

        def inject(page):
            page.evaluate(
                "var d = document.createElement('div');"
                "d.id = 'chaos-shield';"
                "d.style.cssText = "
                "'position:fixed;inset:0;background:transparent;z-index:9999';"
                "document.body.appendChild(d);"
            )

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_type=3),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_010", describe(report, state)
        assert failed.resolution is not None  # it DID click
        assert failed.postconditions_ok is False


class TestLayoutSwapMidRun:
    def test_swapped_type_buttons_still_select_the_right_type(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """PASS (healed). 'Triage' and 'Consult' swap positions after the
        encounter form renders. Their labels differ, so lower rungs
        re-locate the true 'Triage' at its new position and the saved
        encounter carries the correct type. Swaps of visually IDENTICAL
        controls are the lookalike case in test_perturbation.py — those go
        wrong."""

        def inject(page):
            page.wait_for_selector("#type-consult", timeout=5000)
            page.evaluate(
                "var seg = document.getElementById('type-seg');"
                "seg.insertBefore(document.getElementById('type-consult'),"
                "document.getElementById('type-triage'));"
            )

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_click=5),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        assert state["enc_item"] is not None
        assert state["enc_item"].startswith("Triage"), describe(report, state)


class TestDialogOverFieldWhileTyping:
    def test_dialog_over_note_field_halts_instead_of_false_verifying(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """ADDED 2026-07-09 (review P2a). A survey dialog renders over the
        note field between the keystrokes and their verification. The
        field region visibly changes, so under the old diff-alone rule the
        typed input verified on ANY pixel change — including when the
        keystrokes had actually fallen elsewhere (that stolen-keystroke
        variant is pinned as a unit test:
        test_replayer.test_type_dialog_over_field_halts_without_retyping).
        The runtime cannot distinguish 'typed, then covered' from 'dialog
        swallowed the keystrokes', so it must halt rather than trust the
        diff: the typed value is not readable in the changed region, and
        the run stops WITHOUT the select-all retype (which could destroy
        field content). Nothing is saved."""

        def inject(page):
            page.evaluate(
                "var d = document.createElement('div');"
                "d.id = 'chaos-dialog';"
                "d.setAttribute('tabindex', '-1');"
                "d.style.cssText = 'position:fixed;left:30px;top:190px;"
                "width:560px;padding:28px;background:#fff;"
                "border:3px solid #333;z-index:9999;font-size:20px';"
                "d.textContent = "
                "'Quick survey: how is MockMed working for you today?';"
                "document.body.appendChild(d);"
                "d.focus();"
            )

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_type=3),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None  # nothing was saved
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_009", describe(report, state)
        assert "Typed input could not be verified" in (failed.error or "")
        assert "retyping is unsafe" in (failed.error or "")


class TestFocusStolenBeforeTyping:
    def test_blur_between_click_and_type_recovers_and_saves_the_note(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """FIXED (was: wrong-action, silent — the encounter was saved with
        an EMPTY note and a green report). Focus is stolen between clicking
        the note field and typing — a one-line stand-in for any app that
        re-renders or pops a late dialog at the wrong moment. The
        keystrokes fall on <body>; typed-input verification sees the field
        region unchanged, refocuses (re-clicks the field), retypes once,
        confirms the text landed, and the run completes with the CORRECT
        note. (Were the retry also to fail, the run would safe-halt — see
        test_replayer.test_type_verification_failure_halts_run.)"""

        def inject(page):
            page.evaluate("document.activeElement && document.activeElement.blur()")

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_click=7),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        # The banner proves the save; its text proves the note ARRIVED.
        assert state["banner"] is not None
        assert state["banner"].strip() == (
            f"Encounter saved — {PARAMS['note'][:40]}"
        ), describe(report, state)
        type_result = next(r for r in report.results if r.step_id == "step_009")
        assert type_result.input_retried is True
        assert type_result.input_verified is True


class TestNavigationHijackMidRun:
    def test_navigate_away_before_save_halts_without_saving(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT. The app navigates back to the task list right after
        the note-field click (session bounce, deep-link, etc.). The
        note-field step's own postconditions fail on the wrong screen and
        the run aborts before anything is typed or saved."""

        def inject(page):
            page.evaluate("location.hash = '#tasks'")

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_click=7),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        assert state["hash"] == "#tasks"
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_008", describe(report, state)


class TestTargetRenamedMidRun:
    def test_save_button_renamed_mid_run_heals_and_saves(
        self, bundle_writes_reversible, mockmed_url, _browser, tmp_path
    ) -> None:
        """PASS (healed). The save button's label changes to 'Commit
        Record' after the form renders. Template and OCR evidence die, the
        geometry rung (landmarks are unchanged) resolves it, the anchor is
        healed, and the encounter saves normally.

        Uses the writes-reversible bundle so this isolates the HEALING
        mechanism: with the default bundle the auto-classified irreversible
        Save would (correctly) refuse a geometry-rung resolution — the risk
        gate covered by TestIrreversibleRiskGate."""
        bundle = bundle_writes_reversible

        def inject(page):
            page.wait_for_selector("#save-encounter", timeout=5000)
            page.evaluate(
                "document.getElementById('save-encounter')"
                ".textContent = 'Commit Record'"
            )

        report, state = run(
            bundle,
            mockmed_url,
            _browser,
            tmp_path,
            chaos(inject, after_click=5),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        assert state["banner"] is not None
        assert report.heal_count >= 1, describe(report, state)
