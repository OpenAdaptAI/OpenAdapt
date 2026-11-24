"""A written field with no expected value and an empty summary still gets a verdict.

Real headless Chromium: the read-back judge reads a summary page in which an
optional declared field is empty. The run wrote that field (an operator
cleared it) but the dossier has no value for it. Before the fix the judge
skipped it and the verdict passed, so a receipt could be signed with a
written field never mentioned. Now it is ``not_checked``.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from openadapt_flow.form_tx.judge import SummaryJudge
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.types import (
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldWriteRecord,
    FormPack,
    InputDossier,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
)

_SUMMARY = """<!doctype html><html><body><main id="done">
<label for="fn_ro">First name</label><input id="fn_ro" readonly value="Avery">
<label for="nick_ro">Nickname</label><input id="nick_ro" readonly value="">
</main></body></html>"""


@pytest.fixture()
def page() -> Iterator[Any]:
    sync_pw = pytest.importorskip("playwright.sync_api")
    with sync_pw.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            p = browser.new_page()
            p.set_content(_SUMMARY)
            yield p
        finally:
            browser.close()


def _bind(key: str, ident: str) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=key,
        dossier_key=key,
        anchor=FieldAnchorSpec(name_or_id_patterns=(rf"^{ident}(?:\s|$)",)),
    )


@pytest.mark.parametrize("written", [False, True])
def test_written_field_with_no_fact_and_empty_summary_is_not_checked(
    page: Any, written: bool
) -> None:
    pack = FormPack(
        pack_id="test-written-no-fact",
        domain_schema="test.quote/v1",
        ui_profile_id="test",
        bindings=(
            _bind("applicant.first_name", "fn"),
            _bind("applicant.nickname", "nick"),
        ),
        summary_check=SummaryCheckSpec(
            field_readbacks=(
                _bind("applicant.first_name", "fn_ro"),
                _bind("applicant.nickname", "nick_ro"),
            )
        ),
    )
    dossier = InputDossier(
        dossier_id="d1",
        request_id="r1",
        facts={
            "applicant.first_name": DossierFact(
                key="applicant.first_name",
                value="Avery",
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake",
            )
        },
    )
    writes = [
        FieldWriteRecord(
            field_key="applicant.first_name",
            dossier_key="applicant.first_name",
            selector="#fn",
            source="auto_fill",
        )
    ]
    if written:
        writes.append(
            FieldWriteRecord(
                field_key="applicant.nickname",
                dossier_key="applicant.nickname",
                selector="#nick",
                source="operator_value",
            )
        )
    surface = PlaywrightFormSurface(page, UIProfileSpec(profile_id="test"))
    verdict = SummaryJudge().evaluate(
        surface=surface, pack=pack, dossier=dossier, writes=writes
    )
    statuses = {v.dossier_key: v.status for v in verdict.field_verdicts}
    assert statuses["applicant.first_name"] == ReadbackStatus.PASSED
    if written:
        assert statuses["applicant.nickname"] == ReadbackStatus.NOT_CHECKED
        assert not verdict.passed
    else:
        # An optional field nobody wrote, shown empty, needs no verdict.
        assert "applicant.nickname" not in statuses
        assert verdict.passed
