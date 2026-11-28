"""Tests for order-preserving numeric rank tokenization in flight.pseudonym."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from openadapt_flow.flight.pseudonym import (
    ORD_NUM_KIND,
    TOKEN,
    Placeholders,
    Pseudonyms,
    detokenize_response,
    parse_canonical_number,
)


class TestParseCanonicalNumber:
    def test_currency_commas_decimals_and_whitespace(self) -> None:
        assert parse_canonical_number("  $25,000  ") == Decimal("25000")
        assert parse_canonical_number("25000.00") == Decimal("25000")
        assert parse_canonical_number("USD 25,000.00") == Decimal("25000")
        assert parse_canonical_number("€1,234.50") == Decimal("1234.5")
        assert parse_canonical_number(".75") == Decimal("0.75")

    def test_negatives_and_accounting_parentheses(self) -> None:
        assert parse_canonical_number("-$25,000") == Decimal("-25000")
        assert parse_canonical_number("$-25,000.50") == Decimal("-25000.5")
        assert parse_canonical_number("($1,250.50)") == Decimal("-1250.5")
        assert parse_canonical_number("-0.00") == Decimal("0")

    def test_percentages(self) -> None:
        assert parse_canonical_number("12.5%") == Decimal("0.125")
        assert parse_canonical_number("100 %") == Decimal("1")
        assert parse_canonical_number("-5%") == Decimal("-0.05")
        assert parse_canonical_number("50%") == parse_canonical_number("0.50")

    def test_non_numeric_and_non_finite_return_none(self) -> None:
        for bad in (
            "",
            "   ",
            "abc",
            "$",
            "1,2",
            "1,,000",
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            True,
            False,
        ):
            assert parse_canonical_number(bad) is None


class TestOrderPreservingNumericRankTokenization:
    def test_token_regex_matches_legacy_and_ord_num(self) -> None:
        m1 = TOKEN.fullmatch("[PERSON_1]")
        assert m1 is not None
        assert m1.groups() == ("PERSON", "1")

        m2 = TOKEN.fullmatch("[ORD_NUM_0001]")
        assert m2 is not None
        assert m2.groups() == ("ORD_NUM", "0001")

    def test_seed_numeric_preserves_strict_ascending_order(self) -> None:
        p = Pseudonyms()
        mapping = p.seed_numeric(
            ["$50,000", "-$100", "$25,000", "25000.00", "$0", "12.5%", "$10,000"]
        )

        # Equal numeric values receive the same rank token.
        assert mapping["$25,000"] == mapping["25000.00"]

        # Strict ascending order (both lexicographical and numeric rank).
        ordered_inputs = ["-$100", "$0", "12.5%", "$10,000", "$25,000", "$50,000"]
        tokens = [mapping[raw] for raw in ordered_inputs]
        assert tokens == sorted(tokens)
        ranks = [Pseudonyms.numeric_rank(t) for t in tokens]
        assert ranks == [1, 2, 3, 4, 5, 6]

        # Threshold comparison (>= $25,000) holds directly on rank tokens.
        threshold = mapping["$25,000"]
        assert mapping["$50,000"] >= threshold
        assert mapping["25000.00"] >= threshold
        assert not (mapping["$10,000"] >= threshold)

    def test_detokenize_restores_original_formatted_values(self) -> None:
        p = Pseudonyms()
        mapping = p.seed_numeric(["$25,000", "25000.00", "$100,000.50"])

        text = f"Claim {mapping['$100,000.50']} exceeds {mapping['$25,000']}"
        assert p.detokenize(text) == "Claim $100,000.50 exceeds $25,000"

        # Tool call response detokenization works cleanly for ORD_NUM tokens.
        payload = json.dumps(
            {
                "type": "tool_use",
                "input": {"amount": mapping["$100,000.50"]},
            }
        ).encode("utf-8")
        restored = json.loads(detokenize_response(payload, p))
        assert restored["input"]["amount"] == "$100,000.50"

    def test_known_spans_and_scrub_integration(self) -> None:
        p = Pseudonyms()
        mapping = p.seed_numeric(["$10,000", "$25,000", "25000.00", "$75,000"])
        p.seed([("PERSON", "Jane Doe")])

        sample = "Patient Jane Doe billed $75,000 (threshold 25000.00)."
        spans = p.known_spans(sample)
        found = {(sample[s:e], kind) for s, e, kind in spans}
        assert ("Jane Doe", "PERSON") in found
        assert ("$75,000", ORD_NUM_KIND) in found
        assert ("25000.00", ORD_NUM_KIND) in found

        # Tokenizing via token(ORD_NUM, ...) resolves canonical equivalents.
        assert p.token(ORD_NUM_KIND, "USD 25,000") == mapping["$25,000"]
        assert p.numeric_token(Decimal("25000")) == mapping["$25,000"]

    def test_seed_numeric_rejects_invalid_values(self) -> None:
        p = Pseudonyms()
        with pytest.raises(ValueError, match="cannot parse numeric value"):
            p.seed_numeric(["$10,000", "not-a-number"])
        with pytest.raises(ValueError, match="cannot parse numeric value"):
            p.numeric_token("abc")

    def test_persistence_and_backwards_compatibility(self, tmp_path: Path) -> None:
        path = tmp_path / "pseudonyms.json"
        p = Pseudonyms(path=path)
        t_person = p.token("PERSON", "Alice Smith")
        t_id = p.token("ID", "MRN-9988")
        mapping = p.seed_numeric(["$25,000", "$5,000"])

        assert t_person == "[PERSON_1]"
        assert t_id == "[ID_1]"
        assert mapping["$5,000"] == "[ORD_NUM_0001]"
        assert mapping["$25,000"] == "[ORD_NUM_0002]"

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["tokens"]["[PERSON_1]"] == "Alice Smith"
        assert saved["tokens"]["[ORD_NUM_0001]"] == "$5,000"
        assert saved["tokens"]["[ORD_NUM_0002]"] == "$25,000"

    def test_placeholders_noop_parity(self) -> None:
        ph = Placeholders()
        assert ph.numeric_token("$25,000") == "<ORD_NUM>"
        assert ph.seed_numeric(["$10", "$20"]) == {"$10": "<ORD_NUM>", "$20": "<ORD_NUM>"}
