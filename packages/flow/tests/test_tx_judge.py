"""Unit and property tests for ``openadapt_flow.tx.judge`` (Pierce Lens Laws + Two-Tier Judge)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from openadapt_flow.tx import judge as tx_judge_module
from openadapt_flow.tx.judge import (
    FrameAndIntentJudge,
    SafeNormalizedEntailmentOracle,
    StructuralEntailmentOracle,
    apply_field_codec,
    canonical_phones_match,
    canonicalize_iso_date,
    flatten_record,
    infer_field_codec,
    iso_dates_match,
    matches_field_pattern,
    strict_equal,
    violates_hard_safety_invariant,
)
from openadapt_flow.tx.types import (
    EntailmentOracle,
    FieldCodec,
    JudgeProtocol,
    canonical_sha256,
)


class _AlwaysTrueRogueOracle:
    """Adversarial permissive oracle to verify Hard Safety Guardrail runs BEFORE Tier 2."""

    def __init__(self) -> None:
        self.calls: int = 0

    def entails(
        self,
        field_path: str,
        intended_value: Any,
        stored_value: Any,
        before_value: Any = None,
    ) -> tuple[bool, str]:
        self.calls += 1
        return True, "rogue oracle rubber-stamp"


class _CountingSafeOracle(SafeNormalizedEntailmentOracle):
    """Wraps SafeNormalizedEntailmentOracle to count invocations for cache verification."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: int = 0

    def entails(
        self,
        field_path: str,
        intended_value: Any,
        stored_value: Any,
        before_value: Any = None,
    ) -> tuple[bool, str]:
        self.calls += 1
        return super().entails(field_path, intended_value, stored_value, before_value)


# ---------------------------------------------------------------------------
# 0. Architecture Gates: Protocols, Import Boundary, and Generality
# ---------------------------------------------------------------------------


def test_protocols_and_oracle_alias() -> None:
    judge = FrameAndIntentJudge()
    oracle = SafeNormalizedEntailmentOracle()
    assert isinstance(judge, JudgeProtocol)
    assert isinstance(oracle, EntailmentOracle)
    assert StructuralEntailmentOracle is SafeNormalizedEntailmentOracle
    assert isinstance(StructuralEntailmentOracle(), EntailmentOracle)


def test_judge_module_import_boundary_and_zero_app_names() -> None:
    judge_path = Path(tx_judge_module.__file__).resolve()
    source = judge_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned_imports = {
        "numpy",
        "cv2",
        "PIL",
        "rapidocr",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "playwright",
        "torch",
        "scipy",
        "matplotlib",
    }
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(banned_imports)
    allowed_roots = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "fnmatch",
        "re",
        "secrets",
        "typing",
        "unicodedata",
        "pydantic",
        "openadapt_flow",
    }
    assert imported_roots <= allowed_roots

    lowered = source.lower()
    banned_app_tokens = (
        "open" + "emr",
        "med" + "plum",
        "viku" + "nja",
        "doli" + "barr",
        "open" + "imis",
    )
    for token in banned_app_tokens:
        assert token not in lowered


# ---------------------------------------------------------------------------
# 1. Law 1: Partial-PUT Clobbering, Frame Preservation, and Create Mode
# ---------------------------------------------------------------------------


def test_partial_put_clobbering_flags_exact_seven_untouched_fields() -> None:
    r0 = {
        "id": "rec-101",
        "fname": "Rosa",
        "lname": "Delgado",
        "street": "7 Mill Rd",
        "city": "Exampleton",
        "postal_code": "02138",
        "phone": "555-0188",
        "dependents": 2,
        "allergies": ["penicillin"],
        "updated_at": "2026-03-01T10:00:00Z",
    }
    # Partial-PUT bug: intended phone update succeeded, id preserved, but 7 untouched
    # fields were wiped to "", None, 0, or [].
    r1 = {
        "id": "rec-101",
        "fname": "",
        "lname": "",
        "street": None,
        "city": None,
        "postal_code": "",
        "phone": "555-0199",
        "dependents": 0,
        "allergies": [],
        "updated_at": "2026-03-02T11:30:00Z",
    }

    judge = FrameAndIntentJudge()
    verdict = judge.evaluate(
        r0,
        r1,
        intended_delta={"phone": "555-0199"},
        volatile_fields=("updated_at",),
    )

    assert verdict.passed is False
    assert verdict.frame_preserved is False
    assert verdict.intent_satisfied is True
    assert verdict.mismatched_fields == ()
    assert len(verdict.clobbered_fields) == 7
    assert set(verdict.clobbered_fields) == {
        "allergies",
        "city",
        "dependents",
        "fname",
        "lname",
        "postal_code",
        "street",
    }
    clobbered_verdicts = [
        fv for fv in verdict.field_verdicts if fv.field_path in verdict.clobbered_fields
    ]
    assert len(clobbered_verdicts) == 7
    assert all(
        fv.category == "frame" and fv.passed is False and fv.tier_used == "failed"
        for fv in clobbered_verdicts
    )


def test_frame_strict_bool_vs_int_distinction() -> None:
    assert strict_equal(True, 1) is False
    assert strict_equal(False, 0) is False
    assert strict_equal([True], [1]) is False
    assert strict_equal({"flag": False}, {"flag": 0}) is False

    r0 = {"id": "1", "active": True, "retry_count": 0, "title": "Draft"}
    r1 = {"id": "1", "active": 1, "retry_count": False, "title": "Final"}
    verdict = FrameAndIntentJudge().evaluate(r0, r1, intended_delta={"title": "Final"})
    assert verdict.passed is False
    assert verdict.frame_preserved is False
    assert set(verdict.clobbered_fields) == {"active", "retry_count"}


def test_create_mode_allows_benign_server_defaults_and_flags_unexpected_sensitive_fields() -> (
    None
):
    judge = FrameAndIntentJudge()
    r1_clean = {
        "id": "srv-9001",
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": "2026-03-22T12:00:00Z",
        "status": "active",
        "fname": "Rosa",
        "middle_name": "",
        "notes": None,
        "archived": False,
        "balance": 0,
        "tags": [],
        "preferences": {},
    }
    clean_verdict = judge.evaluate(
        None,
        r1_clean,
        intended_delta={"fname": "Rosa"},
    )
    assert clean_verdict.passed is True
    assert clean_verdict.frame_preserved is True
    assert clean_verdict.intent_satisfied is True
    assert clean_verdict.extra_fields == ()

    r1_contaminated = {
        **r1_clean,
        "is_superuser": True,
        "credit_limit": 50000,
    }
    bad_verdict = judge.evaluate(
        None,
        r1_contaminated,
        intended_delta={"fname": "Rosa"},
    )
    assert bad_verdict.passed is False
    assert bad_verdict.frame_preserved is False
    assert set(bad_verdict.extra_fields) == {"credit_limit", "is_superuser"}


# ---------------------------------------------------------------------------
# 2. Nested FHIR-like Records, Dotted Paths, Nested Deltas, and Wildcards
# ---------------------------------------------------------------------------


def _sample_nested_patient() -> dict[str, Any]:
    return {
        "resourceType": "Patient",
        "id": "pat-42",
        "meta": {
            "versionId": "1",
            "lastUpdated": "2026-03-01T08:00:00Z",
        },
        "name": [
            {
                "family": "Delgado",
                "given": ["Rosa"],
            }
        ],
        "address": [
            {
                "line": ["7 Mill Rd"],
                "city": "BOSTON",
                "state": "MA",
                "postalCode": "02108",
            }
        ],
        "telecom": [
            {
                "system": "phone",
                "value": "+1-617-555-0100",
                "use": "home",
            }
        ],
    }


def test_nested_fhir_like_update_dotted_and_nested_delta_preserves_siblings() -> None:
    r0 = _sample_nested_patient()
    r1 = _sample_nested_patient()
    r1["meta"] = {"versionId": "2", "lastUpdated": "2026-03-02T09:15:00Z"}
    r1["address"][0]["city"] = "CAMBRIDGE"

    judge = FrameAndIntentJudge()

    # Dotted-path intended_delta + explicit volatile paths.
    v_dotted = judge.evaluate(
        r0,
        r1,
        intended_delta={"address.0.city": "CAMBRIDGE"},
        volatile_fields=("meta.versionId", "meta.lastUpdated"),
    )
    assert v_dotted.passed is True
    assert v_dotted.frame_preserved is True
    assert v_dotted.intent_satisfied is True

    # Nested-dict intended_delta + wildcard volatile pattern ("meta.*").
    v_nested = judge.evaluate(
        r0,
        r1,
        intended_delta={"address": [{"city": "CAMBRIDGE"}]},
        volatile_fields=("meta.*",),
    )
    assert v_nested.passed is True
    assert v_nested.frame_preserved is True
    assert v_nested.intent_satisfied is True

    # Verify sibling frame fields were explicitly checked as frame_eq.
    by_path = {fv.field_path: fv for fv in v_nested.field_verdicts}
    assert by_path["address.0.postalCode"].tier_used == "frame_eq"
    assert by_path["name.0.family"].tier_used == "frame_eq"
    assert by_path["meta.versionId"].tier_used == "volatile_skip"
    assert by_path["meta.lastUpdated"].tier_used == "volatile_skip"


def test_nested_fhir_like_update_fails_when_name_given_changes() -> None:
    r0 = _sample_nested_patient()
    r1 = _sample_nested_patient()
    r1["meta"] = {"versionId": "2", "lastUpdated": "2026-03-02T09:15:00Z"}
    r1["address"][0]["city"] = "CAMBRIDGE"
    r1["name"][0]["given"] = ["Maria"]

    verdict = FrameAndIntentJudge().evaluate(
        r0,
        r1,
        intended_delta={"address.0.city": "CAMBRIDGE"},
        volatile_fields=("meta.*",),
    )
    assert verdict.passed is False
    assert verdict.frame_preserved is False
    assert verdict.intent_satisfied is True
    assert verdict.clobbered_fields == ("name.0.given.0",)


def test_wildcard_volatile_patterns_match_suffix_and_prefix() -> None:
    assert matches_field_pattern("items.0.updated_at", ("*.updated_at",)) is True
    assert matches_field_pattern("audit.updated_at", ("*.updated_at",)) is True
    assert matches_field_pattern("audit.my_updated_at", ("*.updated_at",)) is False
    assert matches_field_pattern("meta.versionId", ("meta.*",)) is True
    assert matches_field_pattern("metadata", ("meta.*",)) is False

    r0 = {
        "id": "1",
        "title": "Old",
        "audit": {"updated_at": "t1"},
        "items": [{"updated_at": "t1", "qty": 3}],
    }
    r1 = {
        "id": "1",
        "title": "New",
        "audit": {"updated_at": "t2"},
        "items": [{"updated_at": "t2", "qty": 3}],
    }
    verdict = FrameAndIntentJudge().evaluate(
        r0,
        r1,
        intended_delta={"title": "New"},
        volatile_fields=("*.updated_at",),
    )
    assert verdict.passed is True


# ---------------------------------------------------------------------------
# 3. Law 2: Literalist False-Stop Elimination (Tier-1 Codecs + Tier-2 Oracle)
# ---------------------------------------------------------------------------


def test_tier1_codecs_eliminate_literalist_false_stops() -> None:
    r0 = {
        "state": "NY",
        "street": "1 Main St",
        "full_name": "Jane Roe",
        "phone": "6175550100",
        "birth_date": "1970-01-01T00:00:00Z",
        "email": "JANE@EXAMPLE.ORG",
        "notes": "initial",
    }
    r1 = {
        "state": "MA",
        "street": "7 MILL ROAD",
        "full_name": "Rosa Delgado",
        "phone": "6175550188",
        "birth_date": "1958-03-22T00:00:00Z",
        "email": "rosa@example.org",
        "notes": "follow up needed",
    }
    intended = {
        "state": "Massachusetts",
        "street": "7 mill road",
        "full_name": "rosa delgado",
        "phone": "(617) 555-0188",
        "birth_date": "1958-03-22",
        "email": "ROSA@EXAMPLE.ORG",
        "notes": "  follow   up   needed ",
    }
    codecs = {
        "state": FieldCodec(
            kind="bijection", mapping={"Massachusetts": "MA", "New York": "NY"}
        ),
        "street": FieldCodec(kind="upper"),
        "full_name": FieldCodec(kind="capitalize_words"),
        "phone": FieldCodec(kind="phone_digits"),
        "birth_date": FieldCodec(kind="iso_date"),
        "email": FieldCodec(kind="lower"),
        "notes": FieldCodec(kind="strip_whitespace"),
    }

    verdict = FrameAndIntentJudge().evaluate(
        r0, r1, intended_delta=intended, field_codecs=codecs
    )
    assert verdict.passed is True
    assert verdict.frame_preserved is True
    assert verdict.intent_satisfied is True
    assert all(
        fv.tier_used == "codec" and fv.passed is True
        for fv in verdict.field_verdicts
        if fv.category == "intended"
    )


def test_phone_digits_and_bijection_case_insensitive_variants() -> None:
    assert canonical_phones_match("555-0188", "(555) 0188") is True
    assert canonical_phones_match("+1 (617) 555-0188", "+16175550188") is True
    assert canonical_phones_match("(617) 555-0188", "+16175550188") is True
    assert canonical_phones_match("555-0188", "555-0189") is False
    assert canonical_phones_match("+44 20 7946 0958", "+1 20 7946 0958") is False
    assert canonical_phones_match("+44 617 555 0188", "(617) 555-0188") is False

    bijection = FieldCodec(kind="bijection", mapping={"Massachusetts": "MA"})
    ok_exact, _ = apply_field_codec(bijection, "Massachusetts", "MA")
    ok_ci, _ = apply_field_codec(bijection, "massachusetts", "MA")
    ok_bad, _ = apply_field_codec(bijection, "Massachusetts", "CA")
    assert ok_exact is True
    assert ok_ci is True
    assert ok_bad is False


def test_iso_date_canonicalization_variants() -> None:
    assert canonicalize_iso_date("1958-03-22") == "1958-03-22"
    assert canonicalize_iso_date("1958-03-22T00:00:00Z") == "1958-03-22"
    assert canonicalize_iso_date("1958-03-22T00:00:00.000+00:00") == "1958-03-22"
    assert canonicalize_iso_date("2026-02-30") is None
    assert iso_dates_match("1958-03-22", "1958-03-22T00:00:00Z") is True
    assert iso_dates_match("1958-03-22", "1958-03-27") is False
    assert iso_dates_match("1958-03-22", "1958-03-22T13:45:00Z") is False


def test_safe_normalized_entailment_oracle_scalars_and_structured_telecom() -> None:
    oracle = SafeNormalizedEntailmentOracle()
    judge = FrameAndIntentJudge(entailment_oracle=oracle)

    r0 = {
        "resourceType": "Patient",
        "id": "pat-7",
        "name": [{"family": "Delgado", "given": ["Rosa"]}],
    }
    r1 = {
        "resourceType": "Patient",
        "id": "pat-7",
        "name": [{"family": "Delgado", "given": ["Rosa"]}],
        "birthDate": "1958-03-22T00:00:00Z",
        "telecom": [
            {
                "system": "phone",
                "value": "+16175550188",
                "use": "home",
                "rank": 1,
            }
        ],
    }
    intended = {
        "birthDate": "1958-03-22",
        "telecom": [
            {
                "system": "phone",
                "value": "(617) 555-0188",
                "use": "home",
            }
        ],
    }

    verdict = judge.evaluate(r0, r1, intended_delta=intended)
    assert verdict.passed is True
    assert verdict.frame_preserved is True
    assert verdict.intent_satisfied is True

    # Direct subset object check: benign "rank": 1 passes, contradictory "use" or non-metadata key fails.
    ok_sub, _ = oracle.entails(
        "telecom.0",
        {"system": "phone", "value": "+1-555-0188", "use": "home"},
        {"system": "phone", "value": "+15550188", "use": "home", "rank": 1},
    )
    assert ok_sub is True

    bad_contradict, _ = oracle.entails(
        "telecom.0",
        {"system": "phone", "value": "+1-555-0188", "use": "home"},
        {"system": "phone", "value": "+15550188", "use": "work", "rank": 1},
    )
    assert bad_contradict is False

    bad_extra, _ = oracle.entails(
        "telecom.0",
        {"system": "phone", "value": "+1-555-0188", "use": "home"},
        {
            "system": "phone",
            "value": "+15550188",
            "use": "home",
            "unauthorized_role": "admin",
        },
    )
    assert bad_extra is False


def test_salted_bijection_cache_amortizes_oracle_without_storing_raw_pii() -> None:
    oracle = _CountingSafeOracle()
    cache: dict[tuple[str, str], Any] = {}
    salt = b"0123456789abcdef0123456789abcdef"
    judge = FrameAndIntentJudge(
        entailment_oracle=oracle,
        bijection_cache=cache,
        cache_salt=salt,
    )

    r0 = {"street": "Old Rd"}
    r1 = {"street": "7 MILL ROAD"}
    delta = {"street": "7 mill road"}

    v1 = judge.evaluate(r0, r1, intended_delta=delta)
    assert v1.passed is True
    assert v1.field_verdicts[0].tier_used == "entailment_oracle"
    assert oracle.calls == 1
    assert len(cache) == 1

    cached_path, cached_digest = next(iter(cache.keys()))
    assert cached_path == "street"
    assert cached_digest == judge.compute_pair_digest("7 mill road", "7 MILL ROAD")
    assert "mill" not in cached_digest.lower()

    # Second call resolves via salted bijection_cache without invoking oracle again.
    v2 = judge.evaluate(r0, r1, intended_delta=delta)
    assert v2.passed is True
    assert v2.field_verdicts[0].tier_used == "codec"
    assert oracle.calls == 1

    # Also works with a set-typed bijection_cache and no oracle attached.
    set_cache: set[tuple[str, str]] = {(cached_path, cached_digest)}
    offline_judge = FrameAndIntentJudge(bijection_cache=set_cache, cache_salt=salt)
    v3 = offline_judge.evaluate(r0, r1, intended_delta=delta)
    assert v3.passed is True
    assert v3.field_verdicts[0].tier_used == "codec"


# ---------------------------------------------------------------------------
# 4. Strict Safety Rejections & Guardrail Precedence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "intended_val", "stored_val"),
    [
        ("dosage", "10 mg", "100 mg"),
        ("dosage_compact", "10mg", "100mg"),
        ("quantity", 10, 100),
        ("birth_date", "1958-03-22", "1958-03-27"),
        ("effective_date", "2026-03-22", "2026-03-27"),
        ("given_name", "Bo", "Bob"),
        ("specimen_code", "AB01", "ABO1"),
        ("order_id", "ID-1l", "ID-1I"),
        ("active", True, 1),
        ("archived", False, 0),
        ("allergy_note", "penicillin allergy", "no penicillin allergy"),
        ("phone", "555-0188", "555-0189"),
    ],
)
def test_strict_safety_rejections_fail_default_and_safe_oracle(
    field_name: str, intended_val: Any, stored_val: Any
) -> None:
    r0 = {field_name: intended_val}
    r1 = {field_name: stored_val}
    delta = {field_name: intended_val}

    strict_judge = FrameAndIntentJudge()
    v_strict = strict_judge.evaluate(r0, r1, intended_delta=delta)
    assert v_strict.passed is False
    assert v_strict.intent_satisfied is False
    assert v_strict.mismatched_fields == (field_name,)

    oracle_judge = FrameAndIntentJudge(
        entailment_oracle=SafeNormalizedEntailmentOracle()
    )
    v_oracle = oracle_judge.evaluate(r0, r1, intended_delta=delta)
    assert v_oracle.passed is False
    assert v_oracle.intent_satisfied is False
    assert v_oracle.mismatched_fields == (field_name,)


@pytest.mark.parametrize(
    ("intended_val", "stored_val"),
    [
        ("10 mg", "100 mg"),
        ("10mg", "100mg"),
        (10, 100),
        ("2026-03-22", "2026-03-27"),
        ("AB01", "ABO1"),
        (True, 1),
    ],
)
def test_hard_safety_guardrail_blocks_even_rogue_permissive_oracle(
    intended_val: Any, stored_val: Any
) -> None:
    rogue = _AlwaysTrueRogueOracle()
    judge = FrameAndIntentJudge(entailment_oracle=rogue)
    verdict = judge.evaluate(
        {"target": intended_val},
        {"target": stored_val},
        intended_delta={"target": intended_val},
    )
    assert verdict.passed is False
    assert verdict.intent_satisfied is False
    assert verdict.mismatched_fields == ("target",)
    assert rogue.calls == 0


def test_exact_codec_blocks_oracle_on_pinned_field() -> None:
    rogue = _AlwaysTrueRogueOracle()
    judge = FrameAndIntentJudge(entailment_oracle=rogue)
    verdict = judge.evaluate(
        {"code": "alpha"},
        {"code": "ALPHA"},
        intended_delta={"code": "alpha"},
        field_codecs={"code": FieldCodec(kind="exact")},
    )
    assert verdict.passed is False
    assert rogue.calls == 0


def test_silent_noop_missing_new_intended_field_and_volatile_precedence() -> None:
    judge = FrameAndIntentJudge()
    # Intended field absent from both r0 and r1 must fail Law 2.
    v_missing = judge.evaluate(
        {"fname": "Rosa"},
        {"fname": "Rosa"},
        intended_delta={"phone": "555-0188"},
    )
    assert v_missing.passed is False
    assert v_missing.intent_satisfied is False
    assert v_missing.mismatched_fields == ("phone",)

    # Intended field listed in volatile_fields must STILL be verified under Law 2.
    v_prec = judge.evaluate(
        {"status": "draft"},
        {"status": "draft"},
        intended_delta={"status": "submitted"},
        volatile_fields=("status",),
    )
    assert v_prec.passed is False
    assert v_prec.intent_satisfied is False
    assert v_prec.mismatched_fields == ("status",)


# ---------------------------------------------------------------------------
# 5. Codec Induction: infer_field_codec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intended_val", "stored_val", "expected_kind"),
    [
        ("Rosa", "Rosa", "exact"),
        ("7 mill road", "7 MILL ROAD", "upper"),
        ("ROSA@EXAMPLE.ORG", "rosa@example.org", "lower"),
        ("rosa delgado", "Rosa Delgado", "capitalize_words"),
        ("  7   Mill Rd  ", "7 Mill Rd", "strip_whitespace"),
        ("(617) 555-0188", "6175550188", "phone_digits"),
        ("+1 (617) 555-0188", "+16175550188", "phone_digits"),
        ("1958-03-22", "1958-03-22T00:00:00Z", "iso_date"),
        ("Massachusetts", "MA", "bijection"),
        ("California", "CA", "bijection"),
        ("In Progress", "in_progress", "bijection"),
        ("mcDonald", "McDonald", "casefold"),
    ],
)
def test_infer_field_codec_discovers_valid_codecs_and_roundtrips(
    intended_val: str, stored_val: str, expected_kind: str
) -> None:
    codec = infer_field_codec(intended_val, stored_val)
    assert codec is not None
    assert codec.kind == expected_kind
    if expected_kind == "bijection":
        assert codec.mapping == {intended_val: stored_val}

    verdict = FrameAndIntentJudge().evaluate(
        {"f": "before"},
        {"f": stored_val},
        intended_delta={"f": intended_val},
        field_codecs={"f": codec},
    )
    assert verdict.passed is True


@pytest.mark.parametrize(
    ("intended_val", "stored_val"),
    [
        ("Alice", "Bob"),
        ("10", "20"),
        ("10 mg", "100 mg"),
        ("Bo", "Bob"),
        ("AB01", "ABO1"),
        ("1958-03-22", "1958-03-27"),
        ("Massachusetts", "CA"),
        ("Rosa", ""),
        ("", "Rosa"),
        (True, 1),
        (10, 20),
    ],
)
def test_infer_field_codec_returns_none_on_corrupt_pairs(
    intended_val: Any, stored_val: Any
) -> None:
    assert infer_field_codec(intended_val, stored_val) is None


# ---------------------------------------------------------------------------
# 6. Property-Based Tests (Pierce Lens Laws + Safety Invariants)
# ---------------------------------------------------------------------------

_SCALAR_ST = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10_000, max_value=10_000),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -_", max_size=20),
)

_FIELD_NAME_ST = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True)


@given(
    frame_map=st.dictionaries(_FIELD_NAME_ST, _SCALAR_ST, min_size=1, max_size=8),
    delta_map=st.dictionaries(_FIELD_NAME_ST, _SCALAR_ST, min_size=1, max_size=4),
    ts_before=st.text(min_size=1, max_size=12),
    ts_after=st.text(min_size=1, max_size=12),
)
@settings(max_examples=60)
def test_property_valid_lens_put_preserves_frame_and_satisfies_intent(
    frame_map: dict[str, Any],
    delta_map: dict[str, Any],
    ts_before: str,
    ts_after: str,
) -> None:
    r0 = {**frame_map, "updated_at": ts_before}
    effective_delta = {k: v for k, v in delta_map.items() if k != "updated_at"} or {
        "target_field": "updated_value"
    }
    r1 = {**r0, **effective_delta, "updated_at": ts_after}

    verdict = FrameAndIntentJudge().evaluate(
        r0,
        r1,
        intended_delta=effective_delta,
        volatile_fields=("updated_at",),
    )
    assert verdict.passed is True
    assert verdict.frame_preserved is True
    assert verdict.intent_satisfied is True
    assert verdict.clobbered_fields == ()
    assert verdict.mismatched_fields == ()


@given(
    frame_map=st.dictionaries(_FIELD_NAME_ST, _SCALAR_ST, min_size=2, max_size=8),
    clobbered_val=_SCALAR_ST,
)
@settings(max_examples=60)
def test_property_any_untouched_leaf_mutation_breaks_frame_preservation(
    frame_map: dict[str, Any],
    clobbered_val: Any,
) -> None:
    keys = sorted(frame_map.keys())
    intended_key = keys[0]
    untouched_key = keys[1]
    if strict_equal(frame_map[untouched_key], clobbered_val):
        return

    r0 = dict(frame_map)
    r1 = dict(frame_map)
    r1[intended_key] = "updated_ok"
    r1[untouched_key] = clobbered_val

    verdict = FrameAndIntentJudge().evaluate(
        r0,
        r1,
        intended_delta={intended_key: "updated_ok"},
    )
    assert verdict.passed is False
    assert verdict.frame_preserved is False
    assert verdict.intent_satisfied is True
    assert verdict.clobbered_fields == (untouched_key,)


@given(
    n1=st.integers(min_value=0, max_value=50_000),
    n2=st.integers(min_value=0, max_value=50_000),
)
@settings(max_examples=60)
def test_property_distinct_dosages_never_false_accept(n1: int, n2: int) -> None:
    if n1 == n2:
        return
    intended = f"{n1} mg"
    stored = f"{n2} mg"
    violated, _ = violates_hard_safety_invariant(intended, stored)
    assert violated is True

    rogue = _AlwaysTrueRogueOracle()
    verdict = FrameAndIntentJudge(entailment_oracle=rogue).evaluate(
        {"dose": intended},
        {"dose": stored},
        intended_delta={"dose": intended},
    )
    assert verdict.passed is False
    assert rogue.calls == 0


def test_deterministic_verdict_digest_under_dict_key_permutations() -> None:
    r0_a = {"z": 1, "a": {"y": "keep", "b": "old"}, "m": [True, False]}
    r1_a = {"z": 99, "a": {"y": "keep", "b": "new"}, "m": [1, False]}
    r0_b = {"m": [True, False], "a": {"b": "old", "y": "keep"}, "z": 1}
    r1_b = {"a": {"b": "new", "y": "keep"}, "m": [1, False], "z": 99}

    judge = FrameAndIntentJudge()
    va = judge.evaluate(r0_a, r1_a, intended_delta={"a.b": "new"})
    vb = judge.evaluate(r0_b, r1_b, intended_delta={"a": {"b": "new"}})

    assert va == vb
    assert canonical_sha256(va.model_dump()) == canonical_sha256(vb.model_dump())
    assert set(flatten_record(r0_a).keys()) == {"z", "a.y", "a.b", "m.0", "m.1"}
