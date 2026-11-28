"""Tests for EXTRACT/PAGINATE_UNTIL_EOF IR, pagination, typeahead, and clinical modals."""

from __future__ import annotations

import asyncio

from openadapt_flow.clinical_modals import (
    ModalCategory,
    ModalDisposition,
    ModalPolicyPack,
    ModalSnapshot,
    default_clinical_modal_policy_pack,
)
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.pagination import (
    PageSnapshot,
    PaginationStatus,
    paginate_until_eof,
    parse_visible_header_count,
)
from openadapt_flow.typeahead import (
    DisambiguationStatus,
    TypeAheadCandidate,
    disambiguate_async,
    disambiguate_candidates,
)


class TestIrExtractAndPaginationActions:
    def test_extract_and_paginate_steps_roundtrip(self) -> None:
        wf = Workflow(
            name="worklist-extract",
            steps=[
                Step(
                    id="s1",
                    intent="Extract visible worklist rows",
                    action=ActionKind.EXTRACT,
                ),
                Step(
                    id="s2",
                    intent="Paginate worklist until EOF",
                    action=ActionKind.PAGINATE_UNTIL_EOF,
                ),
            ],
        )
        raw = wf.model_dump_json()
        loaded = Workflow.model_validate_json(raw)
        assert loaded.steps[0].action is ActionKind.EXTRACT
        assert loaded.steps[1].action is ActionKind.PAGINATE_UNTIL_EOF


class TestPaginateUntilEof:
    def test_parse_visible_header_count_variants(self) -> None:
        h1 = parse_visible_header_count("1-20 of 47")
        assert h1 is not None
        assert (h1.start, h1.end, h1.total) == (1, 20, 47)

        h2 = parse_visible_header_count("Showing 21 to 40 of 1,240 entries")
        assert h2 is not None
        assert (h2.start, h2.end, h2.total) == (21, 40, 1240)

        h3 = parse_visible_header_count("Total: 15")
        assert h3 is not None
        assert h3.total == 15

        h4 = parse_visible_header_count("82 results")
        assert h4 is not None
        assert h4.total == 82

        assert parse_visible_header_count("No count here") is None

    def test_cross_page_composite_key_dedup_and_reconciled_complete(self) -> None:
        pages = [
            PageSnapshot(
                rows=[
                    {"mrn": "M1", "dos": "2026-01-01", "amt": "$100"},
                    {"mrn": "M2", "dos": "2026-01-02", "amt": "$200"},
                ],
                header_text="1-2 of 3",
                has_next=True,
            ),
            PageSnapshot(
                rows=[
                    # Overlapping row from page 0 (must be deduplicated)
                    {"mrn": "m2", "dos": "2026-01-02", "amt": "$200"},
                    {"mrn": "M3", "dos": "2026-01-03", "amt": "$300"},
                ],
                header_text="2-3 of 3",
                has_next=False,
            ),
        ]

        res = paginate_until_eof(
            lambda idx: pages[idx] if idx < len(pages) else None,
            key_fields=("mrn", "dos"),
        )
        assert res.status is PaginationStatus.COMPLETE
        assert res.observed_total == 3
        assert res.expected_total == 3
        assert res.duplicate_rows_skipped == 1
        assert [r["mrn"] for r in res.rows] == ["M1", "M2", "M3"]

    def test_header_count_mismatch_returns_reconciliation_required(self) -> None:
        pages = [
            PageSnapshot(
                rows=[{"mrn": "M1", "dos": "2026-01-01"}],
                header_text="1-1 of 5",
                has_next=True,
            ),
            PageSnapshot(
                rows=[{"mrn": "M2", "dos": "2026-01-02"}],
                header_text="2-2 of 5",
                has_next=False,
            ),
        ]
        res = paginate_until_eof(
            lambda idx: pages[idx] if idx < len(pages) else None,
            key_fields=("mrn", "dos"),
        )
        assert res.status is PaginationStatus.RECONCILIATION_REQUIRED
        assert res.expected_total == 5
        assert res.observed_total == 2
        assert "5" in (res.reason or "")

    def test_cycle_detection_on_repeated_page_hash(self) -> None:
        p0 = PageSnapshot(
            rows=[{"mrn": "M1", "dos": "2026-01-01"}],
            header_text="1-1 of 10",
            has_next=True,
        )
        res = paginate_until_eof(
            lambda _idx: p0,
            key_fields=("mrn", "dos"),
        )
        assert res.status is PaginationStatus.CYCLE_DETECTED
        assert res.pages_visited == 2

    def test_cycle_detection_on_stalled_zero_new_keys(self) -> None:
        pages = [
            PageSnapshot(
                rows=[
                    {"mrn": "M1", "dos": "2026-01-01"},
                    {"mrn": "M2", "dos": "2026-01-02"},
                ],
                has_next=True,
            ),
            # Different order -> different page hash, but zero new keys!
            PageSnapshot(
                rows=[
                    {"mrn": "M2", "dos": "2026-01-02"},
                    {"mrn": "M1", "dos": "2026-01-01"},
                ],
                has_next=True,
            ),
        ]
        res = paginate_until_eof(
            lambda idx: pages[idx],
            key_fields=("mrn", "dos"),
        )
        assert res.status is PaginationStatus.CYCLE_DETECTED
        assert "no new unique row keys" in (res.reason or "")

    def test_max_pages_exceeded(self) -> None:
        res = paginate_until_eof(
            lambda idx: PageSnapshot(
                rows=[{"mrn": f"M{idx}", "dos": "2026-01-01"}],
                has_next=True,
            ),
            key_fields=("mrn", "dos"),
            max_pages=3,
        )
        assert res.status is PaginationStatus.MAX_PAGES_EXCEEDED
        assert res.pages_visited == 3


class TestAsyncTypeAheadDisambiguator:
    def test_selects_unique_primary_and_secondary_match(self) -> None:
        candidates = [
            TypeAheadCandidate(
                label="Maria Garcia (04/12/1980)",
                fields={"name": "Maria Garcia", "dob": "04/12/1980", "mrn": "MRN-101"},
                candidate_id="c1",
                index=0,
            ),
            TypeAheadCandidate(
                label="Maria Garcia (09/03/1975)",
                fields={"name": "Maria Garcia", "dob": "09/03/1975", "mrn": "MRN-202"},
                candidate_id="c2",
                index=1,
            ),
        ]
        res = disambiguate_candidates(
            candidates,
            primary_key="name",
            primary_value="maria garcia",
            secondary_keys={"dob": "1980-04-12"},
        )
        assert res.status is DisambiguationStatus.SELECTED
        assert res.selected is not None
        assert res.selected.candidate_id == "c1"

    def test_halts_on_ambiguous_tie(self) -> None:
        candidates = [
            TypeAheadCandidate(
                label="John Smith",
                fields={"name": "John Smith", "dob": "1980-01-01"},
                candidate_id="c1",
            ),
            TypeAheadCandidate(
                label="John Smith",
                fields={"name": "John Smith", "dob": "01/01/1980"},
                candidate_id="c2",
            ),
        ]
        res = disambiguate_candidates(
            candidates,
            primary_key="name",
            primary_value="John Smith",
            secondary_keys={"dob": "1980-01-01"},
        )
        assert res.status is DisambiguationStatus.HALT_AMBIGUOUS_TIE
        assert res.selected is None
        assert len(res.matched_candidates) == 2

    def test_halts_on_zero_matches(self) -> None:
        candidates = [
            TypeAheadCandidate(
                label="John Smith",
                fields={"name": "John Smith", "dob": "1979-05-05"},
            )
        ]
        res = disambiguate_candidates(
            candidates,
            primary_key="name",
            primary_value="John Smith",
            secondary_keys={"dob": "1980-01-01"},
        )
        assert res.status is DisambiguationStatus.HALT_NO_MATCH
        assert res.selected is None

    def test_async_polling_waits_for_settled_list_then_selects(self) -> None:
        snapshots = [
            [],
            [
                TypeAheadCandidate(
                    label="Lee Chen",
                    fields={"name": "Lee Chen", "mrn": "MRN-500"},
                    candidate_id="a1",
                )
            ],
            [
                TypeAheadCandidate(
                    label="Lee Chen",
                    fields={"name": "Lee Chen", "mrn": "MRN-500"},
                    candidate_id="a1",
                )
            ],
        ]
        calls = {"n": 0}

        async def fetch() -> list[TypeAheadCandidate]:
            idx = min(calls["n"], len(snapshots) - 1)
            calls["n"] += 1
            return snapshots[idx]

        res = asyncio.run(
            disambiguate_async(
                fetch,
                primary_key="name",
                primary_value="Lee Chen",
                secondary_keys={"mrn": "MRN-500"},
                timeout_s=1.0,
                poll_interval_s=0.005,
                settle_polls=2,
            )
        )
        assert res.status is DisambiguationStatus.SELECTED
        assert res.selected is not None
        assert res.selected.candidate_id == "a1"
        assert res.attempts == 3

    def test_async_timeout_when_suggestions_never_arrive(self) -> None:
        async def empty_fetch() -> list[TypeAheadCandidate]:
            return []

        res = asyncio.run(
            disambiguate_async(
                empty_fetch,
                primary_key="name",
                primary_value="Lee Chen",
                secondary_keys={"mrn": "MRN-500"},
                timeout_s=0.03,
                poll_interval_s=0.01,
                settle_polls=2,
            )
        )
        assert res.status is DisambiguationStatus.HALT_TIMEOUT
        assert res.selected is None


class TestClinicalModalPolicyPack:
    def test_drug_allergy_warning_takes_priority_and_halts_for_human(self) -> None:
        pack = default_clinical_modal_policy_pack()
        modal = ModalSnapshot(
            title="Drug-Allergy Alert",
            body="Severe penicillin allergy alert detected. Are you sure you want to proceed?",
            buttons=("Override", "Cancel"),
        )
        result = pack.classify(modal)
        assert result.category is ModalCategory.DRUG_ALLERGY_WARNING
        assert result.disposition is ModalDisposition.HALT_FOR_HUMAN
        assert result.requires_human is True
        assert result.recommended_button is None

    def test_unsaved_changes_halts_for_human(self) -> None:
        pack = default_clinical_modal_policy_pack()
        modal = ModalSnapshot(
            title="Unsaved Changes",
            body="You have unsaved changes. Leave without saving?",
            buttons=("Leave", "Stay"),
        )
        result = pack.classify(modal)
        assert result.category is ModalCategory.UNSAVED_CHANGES
        assert result.disposition is ModalDisposition.HALT_FOR_HUMAN
        assert result.requires_human is True

    def test_session_timeout_keep_alive_and_missing_button_escalation(self) -> None:
        pack = default_clinical_modal_policy_pack()
        ok_modal = ModalSnapshot(
            title="Session Timeout",
            body="Your session will expire in 2 minutes due to inactivity.",
            buttons=("Log Out", "Stay Signed In"),
        )
        res_ok = pack.classify(ok_modal)
        assert res_ok.category is ModalCategory.SESSION_TIMEOUT
        assert res_ok.disposition is ModalDisposition.KEEP_ALIVE
        assert res_ok.requires_human is False
        assert res_ok.recommended_button == "Stay Signed In"

        # If expected keep-alive button is missing, escalate to human.
        no_btn_modal = ModalSnapshot(
            title="Session Timeout",
            body="Your session will expire shortly.",
            buttons=("Log Out",),
        )
        res_halt = pack.classify(no_btn_modal)
        assert res_halt.disposition is ModalDisposition.HALT_FOR_HUMAN
        assert res_halt.requires_human is True

    def test_confirmation_prompt_and_unknown_modal(self) -> None:
        pack = default_clinical_modal_policy_pack()
        confirm = ModalSnapshot(
            title="Confirm Action",
            body="Please confirm you want to close this filter panel.",
            buttons=("Cancel", "Confirm"),
        )
        res_confirm = pack.classify(confirm)
        assert res_confirm.category is ModalCategory.CONFIRMATION_PROMPT
        assert res_confirm.disposition is ModalDisposition.CONFIRM_CONTROLLED
        assert res_confirm.recommended_button == "Confirm"
        assert res_confirm.requires_human is False

        unknown = ModalSnapshot(
            title="Unexpected System Notice",
            body="Error code 0x80040154 occurred.",
            buttons=("OK",),
        )
        res_unknown = pack.classify(unknown)
        assert res_unknown.category is ModalCategory.UNKNOWN
        assert res_unknown.disposition is ModalDisposition.HALT_FOR_HUMAN
        assert res_unknown.requires_human is True

    def test_declarative_from_dict_policy_pack(self) -> None:
        pack = ModalPolicyPack.from_dict(
            {
                "name": "custom-site-pack",
                "rules": [
                    {
                        "rule_id": "site.banner",
                        "category": "confirmation_prompt",
                        "disposition": "dismiss_safe",
                        "priority": 50,
                        "any_text_terms": ["maintenance notice"],
                        "recommended_button_terms": ["dismiss"],
                    }
                ],
            }
        )
        res = pack.classify(
            ModalSnapshot(
                title="Maintenance Notice",
                body="Scheduled maintenance tonight at 02:00.",
                buttons=("Dismiss",),
            )
        )
        assert res.disposition is ModalDisposition.DISMISS_SAFE
        assert res.recommended_button == "Dismiss"
        assert res.requires_human is False
