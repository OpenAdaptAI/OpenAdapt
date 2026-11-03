"""Synthetic throughput referrals must not trigger the duplicate-name guard."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

HARNESS_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "openemr_throughput"
    / "harness.py"
)
SPEC = importlib.util.spec_from_file_location("throughput_referrals", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class TestSyntheticReferrals(unittest.TestCase):
    def test_batch_has_unique_last_names_and_name_pairs(self) -> None:
        for n in (0, 1, 26, 27, 32, 33, 256, 257, 1024, 4096):
            with self.subTest(n=n):
                rows = harness.synthetic_referrals(n)
                self.assertEqual(len(rows), n)
                self.assertEqual(len({row["lname"].casefold() for row in rows}), n)
                self.assertEqual(
                    len(
                        {
                            (row["fname"].casefold(), row["lname"].casefold())
                            for row in rows
                        }
                    ),
                    n,
                )

    def test_alphabetic_suffix_continues_past_z_and_zz(self) -> None:
        rows = harness.synthetic_referrals(703)
        for index, suffix in (
            (0, "A"),
            (25, "Z"),
            (26, "AA"),
            (701, "ZZ"),
            (702, "AAA"),
        ):
            with self.subTest(index=index):
                self.assertTrue(rows[index]["lname"].endswith(suffix))
        self.assertNotEqual(rows[0]["lname"], rows[26]["lname"])

    def test_generation_is_deterministic(self) -> None:
        self.assertEqual(
            harness.synthetic_referrals(257), harness.synthetic_referrals(257)
        )

    def test_results_do_not_share_mutable_state(self) -> None:
        before = copy.deepcopy(
            (harness.FIRST, harness.LAST, harness.POSSIBLE_DUPLICATE)
        )
        expected = harness.synthetic_referrals(32)
        rows = harness.synthetic_referrals(32)
        rows[0]["lname"] = "Changed"
        self.assertEqual(harness.synthetic_referrals(32), expected)
        self.assertEqual(
            (harness.FIRST, harness.LAST, harness.POSSIBLE_DUPLICATE), before
        )

    def test_referral_fields_remain_synthetic(self) -> None:
        for row in harness.synthetic_referrals(257):
            self.assertEqual(set(row), set(harness.EXISTING_ROSA))
            self.assertIn(row["fname"], harness.FIRST[row["sex"]])
            self.assertGreaterEqual(len(row["fname"]), 4)
            self.assertTrue(row["lname"].startswith(tuple(harness.LAST)))
            self.assertTrue(row["email"].endswith("@example.invalid"))
            self.assertRegex(row["phone_home"], r"^555-01\d{2}$")

    def test_intentional_duplicate_control_is_preserved(self) -> None:
        existing = copy.deepcopy(harness.EXISTING_ROSA)
        duplicate = copy.deepcopy(harness.POSSIBLE_DUPLICATE)
        rows = harness.synthetic_referrals(256)
        self.assertEqual(duplicate["fname"], existing["fname"])
        self.assertEqual(duplicate["lname"], existing["lname"])
        self.assertNotEqual(duplicate["dob"], existing["dob"])
        self.assertNotIn(duplicate["lname"], {row["lname"] for row in rows})
        self.assertEqual(harness.EXISTING_ROSA, existing)
        self.assertEqual(harness.POSSIBLE_DUPLICATE, duplicate)
