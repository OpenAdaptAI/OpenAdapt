"""Tests for third-party portal independent read-back oracles (no DB/API access)."""

from __future__ import annotations

import hashlib
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

_SAMPLE_DOM_BYTES = (
    b"<html><body><dl>"
    b"<dt>Insured Name</dt><dd>Jordan Rivera</dd>"
    b"<dt>Effective Date</dt><dd>03/14/2026</dd>"
    b"<dt>Deductible</dt><dd>$1,000.00</dd>"
    b"<dt>Coverage Limit</dt><dd>2000</dd>"
    b"<dt>Prior Losses</dt><dd>0</dd>"
    b"<dt>Prior Losses</dt><dd>2</dd>"
    b"<dt>Bundled Auto</dt><dd>Yes</dd>"
    b"<dt>Quote ID</dt><dd>MRN-88041</dd>"
    b"<dt>Policy Ref</dt><dd>MRN-88O41</dd>"
    b"<dt>Premium</dt><dd>\xe2\x82\xac500</dd>"
    b"<dt>Premium</dt><dd>500.00</dd>"
    b"</dl></body></html>"
)
_SAMPLE_DOM_SHA = hashlib.sha256(_SAMPLE_DOM_BYTES).hexdigest()


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_synthetic_pdf(
    lines: list[str],
    *,
    compress: bool = False,
    use_tj_array: bool = False,
    invisible_lines: list[str] | None = None,
) -> bytes:
    """Build a minimal valid PDF containing text lines in Tj or TJ operators."""
    ops: list[str] = ["BT", "/F1 12 Tf", "72 720 Td"]
    if invisible_lines:
        ops.append("3 Tr")
        for line in invisible_lines:
            ops.append(f"({_escape_pdf_text(line)}) Tj")
        ops.append("0 Tr")
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


def _valid_reload_proof(**overrides: object) -> ReloadProof:
    kwargs: dict[str, object] = {
        "navigation_type": "reload",
        "submit_url": "https://carrier.example/quote/submit",
        "summary_url": "https://carrier.example/quote/Q-90412/summary",
        "submit_dom_token": "dom-submit-111",
        "summary_dom_token": "dom-summary-222",
        "reloaded_at_utc": "2026-11-23T05:10:00Z",
        "summary_dom_sha256": _SAMPLE_DOM_SHA,
        "producer": "separate_session",
    }
    kwargs.update(overrides)
    return ReloadProof(**kwargs)  # type: ignore[arg-type]


class TestFreshReloadSummaryOracle:
    def test_happy_path_verified_with_normalization(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = _valid_reload_proof()
        expected = {
            "insured_name": "Jordan Rivera",
            "effective_date": "2026-03-14",
            "deductible": 1000,
            "coverage_limit": 2000,
            "prior_losses": 0,
            "bundled_auto": True,
            "quote_id": "MRN-88041",
        }
        observed = {
            "insured_name": "  Jordan   Rivera ",
            "effective_date": "03/14/2026",
            "deductible": "$1,000.00",
            "coverage_limit": "2000",
            "prior_losses": "0",
            "bundled_auto": "Yes",
            "quote_id": "MRN-88041",
        }

        res = oracle.verify(
            expected, observed, proof, summary_dom_bytes=_SAMPLE_DOM_BYTES
        )

        assert res.verdict == "VERIFIED"
        assert res.receipt_verdict == "CONFIRMED"
        assert res.channel == "fresh_reload_summary"
        assert res.reason == "ok"
        assert res.confirmed_count == 7
        assert res.refuted_count == 0
        assert res.abstained_count == 0
        assert res.artifact_sha256 == _SAMPLE_DOM_SHA
        assert all(c.status == "CONFIRMED" for c in res.checks)
        payload = json.dumps(res.to_dict())
        assert "VERIFIED" in payload

        # Missing summary_dom_bytes, fabricated digest, or value absent from DOM bytes -> HALT
        res_no_dom = oracle.verify(expected, observed, proof)
        assert res_no_dom.verdict == "HALT"
        assert res_no_dom.receipt_verdict == "UNCERTAIN"
        assert res_no_dom.artifact_sha256 is None

        res_fake_sha = oracle.verify(
            {"prior_losses": 0},
            {"prior_losses": 0},
            _valid_reload_proof(summary_dom_sha256="0" * 64),
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res_fake_sha.verdict == "HALT"
        assert res_fake_sha.receipt_verdict == "REFUTED"
        assert res_fake_sha.reason == "summary_dom_digest_mismatch"

        res_not_in_dom = oracle.verify(
            {"insured_name": "Phantom Name"},
            {"insured_name": "Phantom Name"},
            proof,
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res_not_in_dom.verdict == "HALT"
        assert res_not_in_dom.receipt_verdict == "UNCERTAIN"
        assert res_not_in_dom.reason == "observed_value_not_in_dom_bytes"

    def test_untrusted_producer_and_missing_url_or_digest_halt(self) -> None:
        oracle = FreshReloadSummaryOracle()
        for bad_proof in (
            _valid_reload_proof(producer="unknown"),
            _valid_reload_proof(producer="acting_session"),
            _valid_reload_proof(submit_url="", summary_url=""),
            _valid_reload_proof(summary_url="https://other-host.example/summary"),
            _valid_reload_proof(reloaded_at_utc=""),
            _valid_reload_proof(summary_dom_sha256="not-a-sha"),
        ):
            res = oracle.verify({"prior_losses": 0}, {"prior_losses": 0}, bad_proof)
            assert res.verdict == "HALT"
            assert res.receipt_verdict == "UNCERTAIN"

    def test_stale_same_dom_halts_fail_closed(self) -> None:
        oracle = FreshReloadSummaryOracle()
        res = oracle.verify(
            {"prior_losses": 0},
            {"prior_losses": 0},
            _valid_reload_proof(navigation_type="same_dom"),
        )
        assert res.verdict == "HALT"
        assert "stale_in_memory_dom_without_reload_proof" in res.reason

    def test_identical_dom_tokens_halt_fail_closed(self) -> None:
        oracle = FreshReloadSummaryOracle()
        res = oracle.verify(
            {"prior_losses": 0},
            {"prior_losses": 0},
            _valid_reload_proof(
                submit_dom_token="dom-same", summary_dom_token="dom-same"
            ),
        )
        assert res.verdict == "HALT"
        assert "summary_dom_token matches submit_dom_token" in res.reason

    def test_currency_mismatch_and_field_mismatch_halt(self) -> None:
        oracle = FreshReloadSummaryOracle()
        proof = _valid_reload_proof(navigation_type="new_session")
        # $500 vs €500 must NOT compare equal
        res_cur = oracle.verify(
            {"premium": "$500"},
            {"premium": "€500"},
            proof,
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res_cur.verdict == "HALT"
        assert res_cur.receipt_verdict == "REFUTED"
        assert res_cur.refuted_count == 1

        # One-sided currency: expected €500 vs bare 500.00 -> ABSTAIN (HALT / UNCERTAIN), never VERIFIED
        res_one_sided = oracle.verify(
            {"premium": "€500"},
            {"premium": "500.00"},
            proof,
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res_one_sided.verdict == "HALT"
        assert res_one_sided.receipt_verdict == "UNCERTAIN"
        assert res_one_sided.abstained_count == 1

        res = oracle.verify(
            {"prior_losses": 0, "deductible": 1000},
            {"prior_losses": 2, "deductible": "$1,000.00"},
            proof,
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res.verdict == "HALT"
        assert res.receipt_verdict == "REFUTED"
        assert res.reason == "field_mismatch"
        assert res.confirmed_count == 1
        assert res.refuted_count == 1

    def test_missing_required_field_abstains_and_halts(self) -> None:
        oracle = FreshReloadSummaryOracle()
        res = oracle.verify(
            {"prior_losses": 0, "roof_year": 2018},
            {"prior_losses": 0},
            _valid_reload_proof(),
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res.verdict == "HALT"
        assert res.receipt_verdict == "UNCERTAIN"
        assert res.reason == "missing_required_fields"
        assert res.abstained_count == 1

    def test_lookalike_identifier_is_not_blurred(self) -> None:
        oracle = FreshReloadSummaryOracle()
        res = oracle.verify(
            {"policy_ref": "MRN-88041"},
            {"policy_ref": "MRN-88O41"},
            _valid_reload_proof(),
            summary_dom_bytes=_SAMPLE_DOM_BYTES,
        )
        assert res.verdict == "HALT"
        assert res.receipt_verdict == "REFUTED"

    def test_empty_expected_fields_halts(self) -> None:
        oracle = FreshReloadSummaryOracle()
        res = oracle.verify({}, {"a": 1}, _valid_reload_proof())
        assert res.verdict == "HALT"
        assert res.reason == "empty_expected_fields"


class TestConfirmationPdfOracle:
    def test_uncompressed_quote_pdf_verified_with_separate_session_producer(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "independent_store"
        store.mkdir()
        pdf_path = store / "quote_confirmation.pdf"
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
        expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

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
            producer="separate_session",
            expected_fetch_sha256=expected_sha,
        )

        assert res.verdict == "VERIFIED"
        assert res.receipt_verdict == "CONFIRMED"
        assert res.channel == "confirmation_pdf"
        assert res.confirmed_count == 6
        assert res.artifact_sha256 == expected_sha
        assert res.provenance_evidence["producer"] == "separate_session"

    def test_unverified_or_acting_producer_cannot_issue_verified(
        self, tmp_path: Path
    ) -> None:
        pdf_path = tmp_path / "quote.pdf"
        pdf_path.write_bytes(_make_synthetic_pdf(["Insured Name: Jordan Rivera"]))
        oracle = ConfirmationPdfOracle()

        # Default producer="unknown" -> HALT / UNCERTAIN
        res_unknown = oracle.verify(pdf_path, {"insured_name": "Jordan Rivera"})
        assert res_unknown.verdict == "HALT"
        assert res_unknown.receipt_verdict == "UNCERTAIN"
        assert "unverified_producer_provenance" in res_unknown.reason

        # Explicit acting_session -> HALT / UNCERTAIN
        res_acting = oracle.verify(
            pdf_path, {"insured_name": "Jordan Rivera"}, producer="acting_session"
        )
        assert res_acting.verdict == "HALT"

        # Declared separate_session but inside acting_roots -> downgraded -> HALT
        res_overlap = oracle.verify(
            pdf_path,
            {"insured_name": "Jordan Rivera"},
            producer="separate_session",
            acting_roots=[tmp_path],
        )
        assert res_overlap.verdict == "HALT"
        assert "unverified_producer_provenance" in res_overlap.reason

        # Inside playwright-artifacts-* download dir -> refused -> HALT
        pw_dir = tmp_path / "playwright-artifacts-xyz"
        pw_dir.mkdir()
        pw_pdf = pw_dir / "quote.pdf"
        pw_pdf.write_bytes(_make_synthetic_pdf(["Insured Name: Jordan Rivera"]))
        res_pw = oracle.verify(
            pw_pdf,
            {"insured_name": "Jordan Rivera"},
            producer="separate_session",
        )
        assert res_pw.verdict == "HALT"

    def test_flatedecode_compressed_pdf_verified(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        store.mkdir()
        pdf_path = store / "quote_compressed.pdf"
        pdf_path.write_bytes(
            _make_synthetic_pdf(
                [
                    "Named Insured: Taylor Chen",
                    "Policy Effective: March 14, 2026",
                    "Annual Premium: $2,450.50",
                    "Prior Claims: 0",
                ],
                compress=True,
            )
        )

        oracle = ConfirmationPdfOracle()
        res = oracle.verify(
            pdf_path,
            expected_fields={
                "insured_name": "Taylor Chen",
                "effective_date": "2026-03-14",
                "annual_premium": "2450.50",
                "prior_losses": 0,
            },
            producer="system_of_record",
            field_labels={
                "insured_name": ("Named Insured", "Insured Name"),
                "effective_date": ("Policy Effective", "Effective Date"),
                "prior_losses": ("Prior Claims", "Prior Losses"),
            },
            field_types={"annual_premium": "currency"},
        )
        assert res.verdict == "VERIFIED"
        assert res.receipt_verdict == "CONFIRMED"
        assert res.confirmed_count == 4

    def test_duplicate_conflicting_labels_and_invisible_text_fail_closed(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "store"
        store.mkdir()
        oracle = ConfirmationPdfOracle()

        # Duplicate label with conflicting values (requested vs issued) -> HALT
        dup_pdf = store / "dup.pdf"
        dup_pdf.write_bytes(_make_synthetic_pdf(["Prior Losses: 0", "Prior Losses: 2"]))
        res_dup = oracle.verify(
            dup_pdf, {"prior_losses": 0}, producer="separate_session"
        )
        assert res_dup.verdict == "HALT"
        assert res_dup.receipt_verdict == "UNCERTAIN"
        assert "ambiguous_duplicate_label" in res_dup.reason

        # Refuted + ambiguous MUST report receipt_verdict="REFUTED", never downgrade to UNCERTAIN
        ref_amb_pdf = store / "ref_amb.pdf"
        ref_amb_pdf.write_bytes(
            _make_synthetic_pdf(["Premium: $600", "Policy: P1", "Policy: P2"])
        )
        res_ref_amb = oracle.verify(
            ref_amb_pdf,
            {"premium": "$500", "policy": "P1"},
            producer="separate_session",
        )
        assert res_ref_amb.verdict == "HALT"
        assert res_ref_amb.receipt_verdict == "REFUTED"
        assert res_ref_amb.refuted_count == 1
        assert "ambiguous_duplicate_label" in res_ref_amb.reason

        # Invisible text (3 Tr and 7 Tr) must be ignored; q/Q restores visible mode after 3 Tr
        inv_pdf = store / "inv.pdf"
        inv_pdf.write_bytes(
            _make_synthetic_pdf(
                ["Prior Losses: 2"], invisible_lines=["Prior Losses: 0"]
            )
        )
        res_inv = oracle.verify(
            inv_pdf, {"prior_losses": 0}, producer="separate_session"
        )
        assert res_inv.verdict == "HALT"
        assert res_inv.receipt_verdict == "REFUTED"

        clip_only_pdf = store / "clip7.pdf"
        clip_only_pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj << /Length 36 >>\nstream\nBT 7 Tr (Premium: $500) Tj ET\nendstream\nendobj\n%%EOF\n"
        )
        res_clip = oracle.verify(
            clip_only_pdf, {"premium": "$500"}, producer="separate_session"
        )
        assert res_clip.verdict == "HALT"
        assert "no extractable text streams" in res_clip.reason

        qq_pdf = store / "qq.pdf"
        qq_pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj << /Length 64 >>\nstream\n"
            b"BT q 3 Tr (Premium: $999) Tj Q (Premium: $500) Tj ET\n"
            b"endstream\nendobj\n%%EOF\n"
        )
        res_qq = oracle.verify(qq_pdf, {"premium": "$500"}, producer="separate_session")
        assert res_qq.verdict == "VERIFIED"
        assert res_qq.receipt_verdict == "CONFIRMED"

    def test_incremental_update_corrupt_flate_and_fetch_digest_mismatch_halt(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "store"
        store.mkdir()
        oracle = ConfirmationPdfOracle()

        # Incremental update (multiple %%EOF) -> HALT
        inc_pdf = store / "inc.pdf"
        inc_pdf.write_bytes(
            _make_synthetic_pdf(["Prior Losses: 0"]) + b"\n% incremental\n%%EOF\n"
        )
        res_inc = oracle.verify(
            inc_pdf, {"prior_losses": 0}, producer="separate_session"
        )
        assert res_inc.verdict == "HALT"
        assert "incremental PDF updates" in res_inc.reason

        # Corrupt /FlateDecode stream -> HALT (never silently skipped!)
        bad_flate = store / "bad_flate.pdf"
        bad_flate.write_bytes(
            b"%PDF-1.4\n"
            b"1 0 obj << /Length 20 >>\nstream\nBT (Prior Losses: 0) Tj ET\nendstream\nendobj\n"
            b"2 0 obj << /Length 8 /Filter /FlateDecode >>\nstream\nnot-zlib\nendstream\nendobj\n"
            b"%%EOF\n"
        )
        res_flate = oracle.verify(
            bad_flate, {"prior_losses": 0}, producer="separate_session"
        )
        assert res_flate.verdict == "HALT"
        assert "corrupt FlateDecode stream" in res_flate.reason

        # Fetch digest mismatch -> HALT / REFUTED
        good_pdf = store / "good.pdf"
        good_pdf.write_bytes(_make_synthetic_pdf(["Prior Losses: 0"]))
        res_sha = oracle.verify(
            good_pdf,
            {"prior_losses": 0},
            producer="separate_session",
            expected_fetch_sha256="0" * 64,
        )
        assert res_sha.verdict == "HALT"
        assert res_sha.receipt_verdict == "REFUTED"
        assert res_sha.reason == "fetch_digest_mismatch"

    def test_missing_corrupt_and_textless_pdfs_halt(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        store.mkdir()
        oracle = ConfirmationPdfOracle()

        res_missing = oracle.verify(
            store / "nope.pdf", {"insured_name": "Jordan"}, producer="separate_session"
        )
        assert res_missing.verdict == "HALT"

        bad_pdf = store / "corrupt.pdf"
        bad_pdf.write_bytes(b"<html>502</html>")
        res_bad = oracle.verify(
            bad_pdf, {"insured_name": "Jordan"}, producer="separate_session"
        )
        assert res_bad.verdict == "HALT"

        img_pdf = store / "img.pdf"
        img_pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj << /Length 4 >>\nstream\n\x00\x01\x02\x03\nendstream\nendobj\n%%EOF\n"
        )
        res_img = oracle.verify(
            img_pdf, {"insured_name": "Jordan"}, producer="separate_session"
        )
        assert res_img.verdict == "HALT"
        assert "no extractable text streams" in res_img.reason


def test_normalize_and_extract_helpers() -> None:
    assert normalize_field_value("$1,200.50") == "$:1200.50"
    assert normalize_field_value("€500") == "€:500.00"
    assert normalize_field_value(2000) == "2000"
    assert normalize_field_value("03/14/2026") == "2026-03-14"
    assert normalize_field_value("Yes") == "true"
    assert normalize_field_value("No") == "false"

    pdf_bytes = _make_synthetic_pdf(["Line (1): OK"], compress=False)
    assert "Line (1): OK" in extract_pdf_text_stdlib(pdf_bytes)


def test_fresh_reload_rejects_value_substring_of_larger_amount() -> None:
    """Probe R5: $500 must not match inside $5000."""
    dom = b"<html><body><dl><dt>Premium</dt><dd>$5000</dd></dl></body></html>"
    proof = _valid_reload_proof(summary_dom_sha256=hashlib.sha256(dom).hexdigest())
    res = FreshReloadSummaryOracle().verify(
        {"premium": "$500"}, {"premium": "$500"}, proof, summary_dom_bytes=dom
    )
    assert res.verdict == "HALT"
    assert res.receipt_verdict != "CONFIRMED"


def test_fresh_reload_rejects_value_under_wrong_label() -> None:
    """Probe R3: Deductible $500 must not satisfy Premium $500."""
    dom = (
        b"<html><body><dl>"
        b"<dt>Premium</dt><dd>$600</dd>"
        b"<dt>Deductible</dt><dd>$500</dd>"
        b"</dl></body></html>"
    )
    proof = _valid_reload_proof(summary_dom_sha256=hashlib.sha256(dom).hexdigest())
    res = FreshReloadSummaryOracle().verify(
        {"premium": "$500"}, {"premium": "$500"}, proof, summary_dom_bytes=dom
    )
    assert res.verdict == "HALT"
    assert res.receipt_verdict != "CONFIRMED"


def test_fresh_reload_rejects_value_only_in_hidden_input_attribute() -> None:
    """Probe R4: values only in HTML attributes (hidden inputs) must not verify."""
    dom = (
        b'<html><body><input type="hidden" name="premium" value="$500">'
        b"<p>Session expired. Nothing was saved.</p></body></html>"
    )
    proof = _valid_reload_proof(summary_dom_sha256=hashlib.sha256(dom).hexdigest())
    res = FreshReloadSummaryOracle().verify(
        {"premium": "$500"}, {"premium": "$500"}, proof, summary_dom_bytes=dom
    )
    assert res.verdict == "HALT"
    assert res.receipt_verdict != "CONFIRMED"


def test_pdf_font_name_slash_q_does_not_reset_invisible_render_mode(
    tmp_path: Path,
) -> None:
    """P3: a PDF resource name like /Q must not pop graphics state and unhide 3 Tr text."""
    store = tmp_path / "store"
    store.mkdir()
    stream = b"BT 3 Tr /Q 1 Tf (Premium: $500) Tj ET"
    pdf_bytes = (
        b"%PDF-1.4\n1 0 obj << /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream\nendobj\n%%EOF\n"
    )
    pdf_path = store / "hidden_font_q.pdf"
    pdf_path.write_bytes(pdf_bytes)
    res = ConfirmationPdfOracle().verify(
        pdf_path, {"premium": "$500"}, producer="separate_session"
    )
    assert res.verdict == "HALT"
    assert res.receipt_verdict != "CONFIRMED"
