"""Self-check of the held-out carrier quote portals (benchmark/form_tx_heldout).

The fast tests check the answer key's shape, the scorer's normalization and
that perturbing any single expected field makes ``score_case`` fail. The
browser test runs the scripted careful human over every scored case in
headless Chromium and requires every case to score correct, which shows each
case is solvable from the intake, the operator script and the screen.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from benchmark.form_tx_heldout import scoring
from benchmark.form_tx_heldout.case_defs import CASES, PORTAL_META
from benchmark.form_tx_heldout.portals import PORTALS

HERE = Path(scoring.__file__).resolve().parent
AK = json.loads((HERE / "answers.json").read_text())
ALL = [(cid, c) for cid, c in AK["cases"].items()]


def test_answer_key_is_built_from_case_defs() -> None:
    for portal, cases in CASES.items():
        for case in cases:
            entry = AK["cases"][case["case_id"]]
            assert entry["expected_record"] == json.loads(json.dumps(case["expected"]))
            intake = json.loads(
                (HERE / "cases" / portal / f"{case['case_id']}.intake.json").read_text()
            )
            assert intake == json.loads(json.dumps(case["intake"]))


@pytest.mark.parametrize("portal", sorted(PORTALS))
def test_portal_case_counts(portal: str) -> None:
    spec = PORTALS[portal]
    assert spec.dev_case and AK["cases"][spec.dev_case]["split"] == "dev"
    assert 7 <= len(spec.cases) <= 10
    clean = [c for c in spec.cases if "clean" in AK["cases"][c]["traps"]]
    assert len(clean) / len(spec.cases) >= 0.3
    for c in clean:
        assert AK["cases"][c]["expected_behavior"] == "submit_clean"
    assert set(AK["portals"][portal]["field_types"]) == set(
        PORTAL_META[portal]["field_types"]
    )


@pytest.mark.parametrize("case_id,case", ALL)
def test_expected_record_covers_every_stored_field(
    case_id: str, case: dict[str, Any]
) -> None:
    types = AK["portals"][case["portal"]]["field_types"]
    assert set(case["expected_record"]) == set(types)
    assert case["expected_behavior"] in AK["behavior_labels"]
    assert "session_login" in case["operator_script"]


def _perturb(value: Any, ftype: str) -> list[Any]:
    if ftype in ("str", "enum"):
        return [f"{value}x", str(value)[:-1] if str(value) else "x"]
    if ftype == "phone":
        return [str(value) + "1"]
    if ftype in ("int", "money"):
        return [int(value or 0) + 1] if value != "" else [1]
    if ftype == "date":
        return [(date.fromisoformat(value) + timedelta(days=1)).isoformat()]
    if ftype == "bool":
        return [not value]
    if ftype == "list":
        extra = {
            "first_name": "Zed",
            "last_name": "Q",
            "dob": "2000-01-01",
            "relationship": "Other",
        }
        out = [list(value) + [extra]]
        if value:
            out.append(list(value)[:-1])
        return out
    raise AssertionError(ftype)


@pytest.mark.parametrize("case_id,case", ALL)
def test_scorer_accepts_expected_and_rejects_any_single_change(
    case_id: str, case: dict[str, Any]
) -> None:
    types = AK["portals"][case["portal"]]["field_types"]
    rec = dict(copy.deepcopy(case["expected_record"]), case_id=case_id)
    assert scoring.score_case(case_id, [rec])["correct"]
    assert not scoring.score_case(case_id, [rec, rec])["correct"]  # submitted twice
    assert not scoring.score_case(case_id, [])["submitted"]
    for field, value in case["expected_record"].items():
        for bad in _perturb(value, types[field]):
            mutated = dict(rec, **{field: bad})
            res = scoring.score_case(case_id, [mutated])
            assert not res["correct"], (field, bad)
        missing = {k: v for k, v in rec.items() if k != field}
        assert field in scoring.score_case(case_id, [missing])["missing_fields"]


@pytest.mark.parametrize("portal", sorted(PORTALS))
def test_every_stored_field_is_in_the_key(portal: str) -> None:
    import importlib

    mod = importlib.import_module(f"benchmark.form_tx_heldout.{portal}")
    assert list(mod.RECORD_FIELDS) and set(mod.RECORD_FIELDS) == set(
        AK["portals"][portal]["field_types"]
    )


@pytest.mark.parametrize("portal", sorted(PORTALS))
def test_intakes_stay_inside_the_published_vocabulary(portal: str) -> None:
    vocab = PORTALS[portal].intake_vocabulary["keys"]
    for case in CASES[portal]:
        for key, value in case["intake"]["facts"].items():
            assert key in vocab, (case["case_id"], key)
            if vocab[key]["type"] == "enum":
                assert str(value).casefold() in [
                    v.casefold() for v in vocab[key]["values"]
                ], (key, value)
            assert value not in (None, "")


def test_normalization_does_not_forgive_trap_shapes() -> None:
    n = scoring.normalize
    assert n("  Hello   World ", "str") == n("hello world", "str")
    assert n("123 Main St Apt", "str") != n("123 Main St", "str")
    assert n("$412,580", "money") == n(412580, "money")
    assert n("1.21M", "money") != n(1210000, "money")
    assert n("412", "money") != n(412580, "money")
    assert n("06/02/1990", "date") == n("1990-06-02", "date")
    assert n("541-555-0101", "phone") == n("(541) 555 0101", "phone")
    assert n("yes", "bool") is True and n(False, "bool") is False


def test_manifest_hashes_match_files() -> None:
    manifest = json.loads((HERE / "MANIFEST.json").read_text())
    for rel, digest in manifest["files"].items():
        assert hashlib.sha256((HERE / rel).read_bytes()).hexdigest() == digest, rel
    assert manifest["answers_sha256"] == manifest["files"]["answers.json"]


def test_reference_human_solves_every_scored_case() -> None:
    pytest.importorskip("playwright.sync_api")
    from benchmark.form_tx_heldout import reference_human
    from benchmark.form_tx_heldout.runner import run_cases

    try:
        results = run_cases(reference_human.make_source)
    except Exception as exc:  # pragma: no cover - environment without a browser
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium is not installed")
        raise
    scored = {c for spec in PORTALS.values() for c in spec.cases}
    assert {r["case_id"] for r in results} == scored
    bad = [
        r
        for r in results
        if not r["correct"]
        or r.get("solvability_problems")
        or r.get("operator_labels_not_seen")
    ]
    assert not bad, json.dumps(bad, indent=1)
