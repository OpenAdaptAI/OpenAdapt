"""Codex's #2429 blockers, fixed by class.

- absent-evidence-as-success: every written, kept or accepted control is a
  subject keyed by where it is, and one with no verdict is ``not_checked``
  (two controls holding one key, an accepted entity row, a kept popup input);
  the signer refuses a verified receipt without per-field verdicts for every
  bound field; a single-screen form with nothing saved never finalizes.
- partial-match-as-match: a radio option matches only when it is selected.
- Receipts signed before later v1 fields existed still verify, labelled as
  the older format.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from openadapt_flow.form_tx.judge import (
    SummaryJudge,
    read_subjects,
    uncovered_subject_verdicts,
)
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.receipt import (
    RECEIPT_FORMAT_CURRENT,
    ed25519_public_key_hex,
    form_tx_receipt_format,
    generate_ed25519_keypair,
    issue_form_tx_receipt,
    verify_form_tx_receipt,
)
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FieldWriteRecord,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    FormTxReceiptPayload,
    HumanDecisionAction,
    HumanResolutionRecord,
    InputDossier,
    ProvenanceKind,
    ReadbackFieldVerdict,
    ReadbackStatus,
    SignedFormTxReceipt,
    SummaryCheckSpec,
    SummaryVerdict,
    UIProfileSpec,
    canonical_json_bytes,
    canonical_sha256,
)


def _dossier(**facts: Any) -> InputDossier:
    return InputDossier(
        dossier_id="d1",
        request_id="r1",
        facts={
            k: DossierFact(
                key=k, value=v, provenance=ProvenanceKind.INTAKE_DOCUMENT, source_id="i"
            )
            for k, v in facts.items()
        },
    )


def _passed(key: str) -> ReadbackFieldVerdict:
    return ReadbackFieldVerdict(
        dossier_key=key,
        codec_kind=FormCodecKind.WHITESPACE_COLLAPSE,
        passed=True,
        status=ReadbackStatus.PASSED,
        provenance=ProvenanceKind.INTAKE_DOCUMENT,
    )


def _codec(_key: str) -> FormCodecKind:
    return FormCodecKind.WHITESPACE_COLLAPSE


# -- coverage by subject ------------------------------------------------------


def test_two_controls_holding_one_key_need_two_verdicts() -> None:
    writes = [
        FieldWriteRecord(
            field_key="home.value",
            dossier_key="home.value",
            selector="#a",
            source="auto_fill",
            in_popup_subroutine="calc",
        ),
        FieldWriteRecord(
            field_key="home.value",
            dossier_key="home.value",
            selector="#b",
            source="auto_fill",
            in_popup_subroutine="calc",
        ),
    ]
    extra = uncovered_subject_verdicts(
        read_subjects(writes, ()), [_passed("home.value")], _codec
    )
    assert [v.status for v in extra] == [ReadbackStatus.NOT_CHECKED]
    assert "2 controls hold" in extra[0].reason


def test_accepted_entity_row_without_read_back_is_not_checked() -> None:
    rec = HumanResolutionRecord(
        halt_id="h1",
        halt_kind=FormHaltKind.PREFILL_ENTITY_CONFLICT,
        action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
        actor_id="op",
        actor_role="operator",
        field_signature="household[0]",
        dossier_key=None,
        resolved_value=None,
        timestamp_utc="2026-11-21T00:00:00Z",
        idempotency_key="k1",
    )
    extra = uncovered_subject_verdicts(read_subjects((), [rec]), [], _codec)
    assert len(extra) == 1 and extra[0].dossier_key.startswith("entity:household[0]|")
    assert extra[0].status == ReadbackStatus.NOT_CHECKED


def test_one_subject_one_verdict_adds_nothing() -> None:
    writes = [
        FieldWriteRecord(
            field_key="a.b", dossier_key="a.b", selector="#a", source="kept_matching"
        )
    ]
    assert (
        uncovered_subject_verdicts(read_subjects(writes, ()), [_passed("a.b")], _codec)
        == []
    )


# -- signer -------------------------------------------------------------------


def _pack(*keys: str) -> FormPack:
    return FormPack(
        pack_id="p",
        domain_schema="test/v1",
        ui_profile_id="t",
        bindings=tuple(
            FieldBindingSpec(
                binding_id=k,
                dossier_key=k,
                anchor=FieldAnchorSpec(css_selector=f"#{k}"),
            )
            for k in keys
        ),
    )


def test_signer_refuses_verified_without_per_field_verdicts() -> None:
    priv, _ = generate_ed25519_keypair()
    with pytest.raises(ValueError):
        issue_form_tx_receipt(
            run_id="r",
            signing_key=priv,
            pack=_pack("a"),
            dossier=_dossier(a="1"),
            outcome=FormTxOutcome.VERIFIED_DONE,
            fields_filled_auto=2,
            readback_checked_count=1,
            readback_passed=True,
        )


def test_signer_refuses_verified_when_one_of_two_bound_fields_passed() -> None:
    priv, _ = generate_ed25519_keypair()
    verdict = SummaryVerdict(
        passed=True, checked_count=1, field_verdicts=(_passed("a"),)
    )
    with pytest.raises(ValueError, match="every bound field"):
        issue_form_tx_receipt(
            run_id="r",
            signing_key=priv,
            pack=_pack("a", "b"),
            dossier=_dossier(a="1", b="2"),
            outcome=FormTxOutcome.VERIFIED_DONE,
            summary_verdict=verdict,
        )


def test_receipt_carries_who_read_it_back() -> None:
    from openadapt_flow.form_tx.types import read_subjects, subjects_digest

    priv, _ = generate_ed25519_keypair()
    verdict = SummaryVerdict(
        passed=True,
        checked_count=1,
        field_verdicts=(_passed("a"),),
        ledger_digest=subjects_digest(read_subjects((), ())),
    )
    receipt = issue_form_tx_receipt(
        run_id="r",
        signing_key=priv,
        pack=_pack("a"),
        dossier=_dossier(a="1"),
        outcome=FormTxOutcome.VERIFIED_DONE,
        summary_verdict=verdict,
        writes=(),
    )
    assert receipt.payload.readback_evidence == "acting_session_reread"
    assert form_tx_receipt_format(receipt) == RECEIPT_FORMAT_CURRENT


# -- older receipts -----------------------------------------------------------


def test_receipt_signed_before_later_fields_still_verifies_as_older() -> None:
    priv, _ = generate_ed25519_keypair()
    payload = FormTxReceiptPayload(
        receipt_id="rcpt_1",
        run_id="r",
        request_id="r1",
        pack_id="p",
        pack_digest="d",
        dossier_digest="d",
        outcome=FormTxOutcome.VERIFIED_DONE,
        timestamp_utc="2026-11-01T00:00:00Z",
        salt_hex="00" * 16,
        fields_filled_auto=1,
        human_resolutions_count=0,
        commit_mutations_observed=1,
        readback_checked_count=1,
        readback_passed=True,
    )
    # What main signed: the payload as it was before the later fields.
    old = {
        k: v
        for k, v in payload.model_dump(mode="json").items()
        if k
        not in ("readback_not_checked_count", "readback_evidence", "covered_keys", "not_covered")
    }
    old_bytes = canonical_json_bytes(old)
    receipt = SignedFormTxReceipt(
        payload=payload,
        payload_sha256=canonical_sha256(old),
        algorithm="Ed25519",
        public_key_hex=ed25519_public_key_hex(priv),
        signature_hex=priv.sign(old_bytes).hex(),
    )
    assert verify_form_tx_receipt(receipt)
    fmt = form_tx_receipt_format(receipt)
    assert fmt is not None and fmt != RECEIPT_FORMAT_CURRENT and "older" in fmt
    tampered = receipt.model_copy(
        update={"payload": payload.model_copy(update={"run_id": "other"})}
    )
    assert not verify_form_tx_receipt(tampered)


# -- radios and single-screen forms (headless) --------------------------------

_RADIO_SUMMARY = """<!doctype html><html><body><main id="done">
<fieldset><legend>Pool</legend>
<label><input type="radio" name="pool_ro" id="pool_yes" value="yes" disabled> Yes</label>
<label><input type="radio" name="pool_ro" id="pool_no" value="no" checked disabled> No</label>
</fieldset></main></body></html>"""

_NO_COMMIT = """<!doctype html><html><body><main id="form">
<label for="fn">First name</label><input id="fn" name="fn">
</main></body></html>"""


@pytest.fixture()
def page() -> Iterator[Any]:
    sync_pw = pytest.importorskip("playwright.sync_api")
    with sync_pw.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def test_unselected_radio_option_does_not_pass_read_back(page: Any) -> None:
    page.set_content(_RADIO_SUMMARY)
    readback = FieldBindingSpec(
        binding_id="rb_pool",
        dossier_key="property.pool",
        anchor=FieldAnchorSpec(css_selector="#pool_yes"),
        control_kind=ControlKind.RADIO_GROUP,
        codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
    )
    pack = FormPack(
        pack_id="radio",
        domain_schema="test/v1",
        ui_profile_id="t",
        bindings=(readback.model_copy(update={"binding_id": "pool"}),),
        summary_check=SummaryCheckSpec(field_readbacks=(readback,)),
    )
    verdict = SummaryJudge().evaluate(
        surface=PlaywrightFormSurface(page, UIProfileSpec(profile_id="t")),
        pack=pack,
        dossier=_dossier(**{"property.pool": "yes"}),
    )
    statuses = {v.dossier_key: v.status for v in verdict.field_verdicts}
    assert statuses["property.pool"] == ReadbackStatus.FAILED
    assert not verdict.passed


def test_single_screen_form_with_nothing_saved_never_finalizes(page: Any) -> None:
    page.set_content(_NO_COMMIT)
    binding = FieldBindingSpec(
        binding_id="fn",
        dossier_key="applicant.first_name",
        anchor=FieldAnchorSpec(css_selector="#fn"),
    )
    pack = FormPack(
        pack_id="nocommit",
        domain_schema="test/v1",
        ui_profile_id="t",
        bindings=(binding,),
        summary_check=SummaryCheckSpec(field_readbacks=(binding,)),
    )
    surface = PlaywrightFormSurface(page, UIProfileSpec(profile_id="t"))
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(
        pack, _dossier(**{"applicant.first_name": "Avery"})
    )
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, result.what_happened
    assert result.receipt is None


_REQUIRED = """<!doctype html><html><body><main id="form">
<label for="fn">First name</label><input id="fn" name="fn" required>
<button id="go" type="button" onclick="document.body.dataset.sent='1'">Submit</button>
</main></body></html>"""


def test_a_retry_does_not_clear_an_open_stop(page: Any) -> None:
    from openadapt_flow.form_tx.types import WizardScreenSpec

    page.set_content(_REQUIRED)
    pack = FormPack(
        pack_id="retry",
        domain_schema="test/v1",
        ui_profile_id="t",
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(css_selector="#go"),
            ),
        ),
    )
    surface = PlaywrightFormSurface(page, UIProfileSpec(profile_id="t"))
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, _dossier())
    first = reconciler.run_until_halt_or_done(session)
    assert first.halts and first.halts[0].kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
    page.fill("#fn", "typed by someone")
    again = reconciler.run_until_halt_or_done(session)
    assert again.halts and again.halts[0].halt_id == first.halts[0].halt_id
    assert page.evaluate("document.body.dataset.sent") is None


def test_digest_only_signing_cannot_issue_verified() -> None:
    priv, _ = generate_ed25519_keypair()
    verdict = SummaryVerdict(
        passed=True, checked_count=1, field_verdicts=(_passed("a"),)
    )
    with pytest.raises(ValueError, match="pack and dossier"):
        issue_form_tx_receipt(
            run_id="r",
            signing_key=priv,
            request_id="r1",
            pack_id="p",
            pack_digest="d",
            dossier_digest="d",
            outcome=FormTxOutcome.VERIFIED_DONE,
            summary_verdict=verdict,
        )


def test_current_receipt_with_a_field_stripped_does_not_verify() -> None:
    from openadapt_flow.form_tx.receipt import receipt_readback_evidence
    from openadapt_flow.form_tx.types import read_subjects, subjects_digest

    priv, _ = generate_ed25519_keypair()
    verdict = SummaryVerdict(
        passed=True,
        checked_count=1,
        field_verdicts=(_passed("a"),),
        ledger_digest=subjects_digest(read_subjects((), ())),
    )
    receipt = issue_form_tx_receipt(
        run_id="r",
        signing_key=priv,
        pack=_pack("a"),
        dossier=_dossier(a="1"),
        outcome=FormTxOutcome.VERIFIED_DONE,
        summary_verdict=verdict,
        writes=(),
    )
    wire = receipt.model_dump(mode="json")
    del wire["payload"]["readback_evidence"]
    stripped = SignedFormTxReceipt.model_validate(wire)
    assert not verify_form_tx_receipt(stripped)
    assert receipt_readback_evidence(receipt) == "acting_session_reread"
    assert receipt_readback_evidence(stripped) == "unknown"


def test_signer_refuses_verified_when_a_written_popup_input_has_no_verdict() -> None:
    from openadapt_flow.form_tx.types import read_subjects, subjects_digest

    priv, _ = generate_ed25519_keypair()
    writes = [
        FieldWriteRecord(
            field_key="a", dossier_key="a", selector="#a", source="auto_fill"
        ),
        FieldWriteRecord(
            field_key="calc.area",
            dossier_key="calc.area",
            selector="#area",
            source="auto_fill",
            in_popup_subroutine="calc",
        ),
    ]
    verdict = SummaryVerdict(
        passed=True,
        checked_count=1,
        field_verdicts=(_passed("a"),),
        ledger_digest=subjects_digest(read_subjects(writes, ())),
    )
    with pytest.raises(ValueError, match="uncovered"):
        issue_form_tx_receipt(
            run_id="r",
            signing_key=priv,
            pack=_pack("a"),
            dossier=_dossier(a="1"),
            outcome=FormTxOutcome.VERIFIED_DONE,
            summary_verdict=verdict,
            writes=writes,
            fields_filled_auto=2,
        )
