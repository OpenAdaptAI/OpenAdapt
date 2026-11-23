"""Tests for third-party portal independent read-back oracles (no DB/API access)."""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from openadapt_verify.portal_readback import (
    ConfirmationPdfOracle,
    FreshReloadSummaryOracle,
    ReloadProof,
    extract_pdf_text_stdlib,
    normalize_field_value,
)


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_synthetic_pdf(
    lines: list[str],
    *,
    compress: bool = False,
    use_tj_array: bool = False,
) -> bytes:
    """Build a minimal valid PDF containing text lines in Tj or TJ operators."""
    ops: list[str] = ["BT", "/F1 12 Tf", "72 720 Td"]
    for idx, line in enumerate(lines):
        if idx > 0:
            ops.append("0 -16 Td")
        if use_tj_array and ":" in line:
            left, right = line.split(":", 1)
            ops.append(
                f"[({_escape_pdf_text(left + ':')}) -10 ({_escape_pdf_text(right)})] TJ"
            )
        else:
            ops.append(f"({_escape_pdf_text(line)}) Tj")
    ops.append("ET")
    stream_data = "\n".join(ops).encode("latin-1")

    if compress:
        payload = zlib.compress(stream_data)
        stream_dict = f"<< /Length {len(payload)} /Filter /FlateDecode >>"
    else:
        payload = stream_data
        stream_dict = f"<< /Length {len(payload)} >>"

    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n",
        f"4 0 obj {stream_dict}\nstream\n".encode("latin-1")
        + payload
        + b"\nendstream\nendobj\n",
        b"%%EOF\n",
    ]
    return b"".join(parts)


class TestFreshReloadSummaryOracle:
    def test_happy_path_verified_with_normalization(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = ReloadProof(
            navigation_type="reload",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/Q-90412/summary",
            submit_dom_token="dom-submit-111",
            summary_dom_token="dom-summary-222",
            reloaded_at_utc="2026-11-23T05:10:00Z",
        )
        expected = {
            "insured_name": "Jordan Rivera",
            "effective_date": "2026-03-14",
            "deductible": 1000,
            "prior_losses": 0,
            "bundled_auto": True,
            "quote_id": "MRN-88041",
        }
        observed = {
            "insured_name": "  Jordan   Rivera ",
            "effective_date": "03/14/2026",
            "deductible": "$1,000.00",
            "prior_losses": "0",
            "bundled_auto": "Yes",
            "quote_id": "MRN-88041",
        }

        res = oracle.verify(expected, observed, proof)

        assert res.verdict == "VERIFIED"
        assert res.channel == "fresh_reload_summary"
        assert res.reason == "ok"
        assert res.confirmed_count == 6
        assert res.refuted_count == 0
        assert res.abstained_count == 0
        assert all(c.status == "CONFIRMED" for c in res.checks)
        assert all("fresh_reload:reload:" in c.provenance for c in res.checks)
        # Ensure JSON serializability
        payload = json.dumps(res.to_dict())
        assert "VERIFIED" in payload

    def test_stale_same_dom_halts_fail_closed(self) -> None:
        oracle = FreshReloadSummaryOracle()
        stale_proof = ReloadProof(
            navigation_type="same_dom",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/submit",
            submit_dom_token="dom-111",
            summary_dom_token="dom-222",
        )
        res = oracle.verify({"prior_losses": 0}, {"prior_losses": 0}, stale_proof)

        assert res.verdict == "HALT"
        assert "stale_in_memory_dom_without_reload_proof" in res.reason
        assert res.confirmed_count == 0
        assert res.abstained_count == 1

    def test_identical_dom_tokens_halt_fail_closed(self) -> None:
        oracle = FreshReloadSummaryOracle()
        same_token_proof = ReloadProof(
            navigation_type="reload",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/summary",
            submit_dom_token="dom-same-token",
            summary_dom_token="dom-same-token",
        )
        res = oracle.verify({"prior_losses": 0}, {"prior_losses": 0}, same_token_proof)

        assert res.verdict == "HALT"
        assert "summary_dom_token matches submit_dom_token" in res.reason

    def test_missing_dom_tokens_halt_fail_closed(self) -> None:
        oracle = FreshReloadSummaryOracle()
        missing_token_proof = ReloadProof(
            navigation_type="fresh_navigation",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/summary",
            submit_dom_token="dom-submit-1",
            summary_dom_token="   ",
        )
        res = oracle.verify(
            {"prior_losses": 0}, {"prior_losses": 0}, missing_token_proof
        )

        assert res.verdict == "HALT"
        assert "missing submit/summary DOM tokens" in res.reason

    def test_field_mismatch_halts_with_refuted_check(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = ReloadProof(
            navigation_type="new_session",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/summary",
            submit_dom_token="dom-a",
            summary_dom_token="dom-b",
        )
        res = oracle.verify(
            {"prior_losses": 0, "deductible": 1000},
            {"prior_losses": 2, "deductible": "$1,000.00"},
            proof,
        )

        assert res.verdict == "HALT"
        assert res.reason == "field_mismatch"
        assert res.confirmed_count == 1
        assert res.refuted_count == 1
        assert res.abstained_count == 0
        refuted = [c for c in res.checks if c.status == "REFUTED"]
        assert len(refuted) == 1
        assert refuted[0].field == "prior_losses"
        assert refuted[0].normalized_expected == "0"
        assert refuted[0].normalized_observed == "2"

    def test_missing_required_field_abstains_and_halts(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = ReloadProof(
            navigation_type="reload",
            submit_url="https://carrier.example/quote/submit",
            summary_url="https://carrier.example/quote/summary",
            submit_dom_token="dom-a",
            summary_dom_token="dom-b",
        )
        res = oracle.verify(
            {"prior_losses": 0, "roof_year": 2018},
            {"prior_losses": 0},
            proof,
        )

        assert res.verdict == "HALT"
        assert res.reason == "missing_required_fields"
        assert res.confirmed_count == 1
        assert res.refuted_count == 0
        assert res.abstained_count == 1
        abstained = [c for c in res.checks if c.status == "ABSTAIN"]
        assert len(abstained) == 1
        assert abstained[0].field == "roof_year"

    def test_lookalike_identifier_is_not_blurred(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = ReloadProof(
            navigation_type="reload",
            submit_url="https://carrier.example/submit",
            summary_url="https://carrier.example/summary",
            submit_dom_token="dom-a",
            summary_dom_token="dom-b",
        )
        res = oracle.verify(
            {"policy_ref": "MRN-88041"},
            {"policy_ref": "MRN-88O41"},
            proof,
        )

        assert res.verdict == "HALT"
        assert res.reason == "field_mismatch"
        assert res.refuted_count == 1

    def test_empty_expected_fields_halts(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = ReloadProof(
            navigation_type="reload",
            submit_url="https://carrier.example/submit",
            summary_url="https://carrier.example/summary",
            submit_dom_token="dom-a",
            summary_dom_token="dom-b",
        )
        res = oracle.verify({}, {"a": 1}, proof)
        assert res.verdict == "HALT"
        assert res.reason == "empty_expected_fields"


class TestConfirmationPdfOracle:
    def test_uncompressed_quote_pdf_verified(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "quote_confirmation.pdf"
        pdf_bytes = _make_synthetic_pdf(
            [
                "Carrier Quote Confirmation (Homeowners)",
                "Insured Name: Jordan Rivera",
                "Effective Date: 03/14/2026",
                "Deductible: $1,000.00",
                "Prior Losses: 0",
                "Bundled Auto: Yes",
                "Quote Number: NW-2026-88041",
            ],
            compress=False,
            use_tj_array=True,
        )
        pdf_path.write_bytes(pdf_bytes)

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={
                "insured_name": "Jordan Rivera",
                "effective_date": "2026-03-14",
                "deductible": 1000,
                "prior_losses": 0,
                "bundled_auto": True,
                "quote_number": "NW-2026-88041",
            },
        )

        assert res.verdict == "VERIFIED"
        assert res.channel == "confirmation_pdf"
        assert res.reason == "ok"
        assert res.confirmed_count == 6
        assert res.refuted_count == 0
        assert res.abstained_count == 0
        assert res.artifact_sha256 is not None and len(res.artifact_sha256) == 64
        assert all(
            c.provenance.startswith(f"pdf:{res.artifact_sha256[:12]}:")
            for c in res.checks
        )

    def test_flatedecode_compressed_pdf_verified(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "quote_compressed.pdf"
        pdf_bytes = _make_synthetic_pdf(
            [
                "Named Insured: Taylor Chen",
                "Policy Effective: March 14, 2026",
                "Annual Premium: $2,450.50",
                "Prior Claims: 0",
            ],
            compress=True,
        )
        pdf_path.write_bytes(pdf_bytes)

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={
                "insured_name": "Taylor Chen",
                "effective_date": "2026-03-14",
                "annual_premium": "2450.50",
                "prior_losses": 0,
            },
            field_labels={
                "insured_name": ("Named Insured", "Insured Name"),
                "effective_date": ("Policy Effective", "Effective Date"),
                "prior_losses": ("Prior Claims", "Prior Losses"),
            },
            field_types={"annual_premium": "currency"},
        )

        assert res.verdict == "VERIFIED"
        assert res.confirmed_count == 4
        assert res.artifact_sha256 is not None

    def test_pdf_field_mismatch_halts(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "quote_mismatch.pdf"
        pdf_path.write_bytes(
            _make_synthetic_pdf(
                [
                    "Insured Name: Jordan Rivera",
                    "Prior Losses: 2",
                    "Deductible: $1,000.00",
                ],
                compress=True,
            )
        )

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={
                "insured_name": "Jordan Rivera",
                "prior_losses": 0,
                "deductible": 1000,
            },
        )

        assert res.verdict == "HALT"
        assert res.reason == "field_mismatch"
        assert res.confirmed_count == 2
        assert res.refuted_count == 1
        refuted = [c for c in res.checks if c.status == "REFUTED"]
        assert refuted[0].field == "prior_losses"
        assert refuted[0].normalized_observed == "2"

    def test_pdf_missing_required_field_abstains_and_halts(
        self, tmp_path: Path
    ) -> None:
        pdf_path = tmp_path / "quote_partial.pdf"
        pdf_path.write_bytes(_make_synthetic_pdf(["Insured Name: Jordan Rivera"]))

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={
                "insured_name": "Jordan Rivera",
                "roof_year": 2018,
            },
        )

        assert res.verdict == "HALT"
        assert res.reason == "missing_required_fields"
        assert res.confirmed_count == 1
        assert res.abstained_count == 1

    def test_missing_pdf_file_halts(self, tmp_path: Path) -> None:
        oracle = ConfirmationPdfOracle()
        res = oracle.verify(tmp_path / "does_not_exist.pdf", {"insured_name": "Jordan"})

        assert res.verdict == "HALT"
        assert res.reason.startswith("missing_pdf:")
        assert res.abstained_count == 1

    def test_corrupt_non_pdf_bytes_halt(self, tmp_path: Path) -> None:
        bad_pdf = tmp_path / "corrupt.pdf"
        bad_pdf.write_bytes(b"<html><body>502 Bad Gateway</body></html>")

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(bad_pdf, {"insured_name": "Jordan"})

        assert res.verdict == "HALT"
        assert "corrupt_or_unreadable_pdf" in res.reason

    def test_pdf_without_extractable_text_halts(self, tmp_path: Path) -> None:
        image_only_pdf = tmp_path / "scanned_image_only.pdf"
        image_only_pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj << /Length 4 >>\nstream\n\x00\x01\x02\x03\nendstream\nendobj\n%%EOF\n"
        )

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(image_only_pdf, {"insured_name": "Jordan"})

        assert res.verdict == "HALT"
        assert "no extractable text streams" in res.reason
        assert res.artifact_sha256 is not None

    def test_custom_field_pattern_extraction(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "regex_quote.pdf"
        pdf_path.write_bytes(
            _make_synthetic_pdf(["Confirmation #NW-99421 issued for $1,250.00"])
        )

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={"quote_id": "NW-99421", "premium": 1250},
            field_patterns={
                "quote_id": r"Confirmation #([A-Z0-9-]+)",
                "premium": r"issued for (\$[0-9,]+\.\d{2})",
            },
        )

        assert res.verdict == "VERIFIED"
        assert res.confirmed_count == 2
        assert all(":regex:" in c.provenance for c in res.checks)

    def test_empty_expected_fields_halts(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "any.pdf"
        pdf_path.write_bytes(_make_synthetic_pdf(["Insured Name: Jordan"]))
        oracle = ConfirmationPdfOracle()
        res = oracle.verify(pdf_path, {})
        assert res.verdict == "HALT"
        assert res.reason == "empty_expected_fields"


def test_normalize_and_extract_helpers() -> None:
    assert normalize_field_value("$1,200.50") == "1200.50"
    assert normalize_field_value("03/14/2026") == "2026-03-14"
    assert normalize_field_value("Yes") == "true"
    assert normalize_field_value("No") == "false"

    pdf_bytes = _make_synthetic_pdf(["Line (1): OK"], compress=False)
    assert "Line (1): OK" in extract_pdf_text_stdlib(pdf_bytes)
