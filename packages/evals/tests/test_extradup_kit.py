"""ExtraDup kit: SoR cardinality kills Extra-NI; Seal must refuse it."""

from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from openadapt_evals.extradup import (
    MOCKMED_GOLD,
    REFUSED,
    VERIFIED,
    apply,
    check_invariants,
    run_cell,
    seal_verdict,
)
from openadapt_evals.extradup.checkers import (
    content_only_check,
    field_inclusion_check,
    identity_check,
    new_records,
    records_under,
    sor_check,
    visual_only_check,
)
from openadapt_evals.extradup.gold import decoy_of, identity_of
from openadapt_evals.extradup.mutations import EVAL_ONLY_OPERATORS, MUTANTS, OPERATORS
from openadapt_evals.extradup.store import MockMedStore
from openadapt_evals.extradup.suite import all_cells, cells, eval_only_cells, main


def test_seal_verified_path_refuses_extra_ni_on_mockmed() -> None:
    """A Seal/VERIFIED path must refuse Extra-NI on MockMed.

    Duplicate-CREATE is gold FAIL. Field-inclusion still PASSes. VERIFIED
    is forbidden.
    """
    report = run_cell("mockmed:dup")
    assert report.gold == "FAIL"
    assert report.sor == "FAIL"
    assert report.new_count != report.spec_count
    assert report.field_inclusion == "PASS"
    assert report.visual_only == "PASS"
    assert report.seal == REFUSED
    assert report.seal != VERIFIED

    store = MockMedStore()
    before, after, _screen = apply(store, MOCKMED_GOLD, "dup")
    assert seal_verdict(MOCKMED_GOLD, before, after) == REFUSED
    assert "|new(M)|=" in ";".join(sor_check(MOCKMED_GOLD, before, after).reasons)


def test_dup_killed_by_cardinality_not_field_inclusion() -> None:
    store = MockMedStore()
    before, after, screen = apply(store, MOCKMED_GOLD, "dup")
    added = new_records(before, after)
    assert len(added) == 2
    assert MOCKMED_GOLD.expected_new == 1
    assert sor_check(MOCKMED_GOLD, before, after).verdict == "FAIL"
    assert field_inclusion_check(MOCKMED_GOLD, after).verdict == "PASS"
    assert visual_only_check(screen).verdict == "PASS"


def test_extra_field_killed_by_sor_not_inclusion() -> None:
    for cell in ("mockmed:extra", "openemr:extra"):
        report = run_cell(cell)
        assert report.gold == "FAIL"
        assert report.sor == "FAIL"
        assert report.field_inclusion == "PASS"
        assert report.visual_only == "PASS"
        assert report.seal == REFUSED
        assert any("extra field" in reason for reason in report.sor_reasons)


@pytest.mark.parametrize("operator", ["omit", "unsubmit", "claim", "wrong_record"])
@pytest.mark.parametrize("env", ["mockmed", "openemr"])
def test_mutant_gold_is_fail(env: str, operator: str) -> None:
    report = run_cell(f"{env}:{operator}")
    assert report.gold == "FAIL"
    assert report.sor == "FAIL"
    assert report.seal == REFUSED
    assert report.visual_only == "PASS"


def test_control_is_verified() -> None:
    for env in ("mockmed", "openemr"):
        report = run_cell(f"{env}:control")
        assert report.gold == "PASS"
        assert report.sor == "PASS"
        assert report.seal == VERIFIED
        assert report.new_count == report.spec_count == 1


def test_openemr_shaped_dup_is_extra_ni() -> None:
    report = run_cell("openemr:dup")
    assert report.env == "openemr_local"
    assert report.gold == "FAIL"
    assert report.sor == "FAIL"
    assert report.field_inclusion == "PASS"
    assert report.seal == REFUSED


def test_wrong_record_is_invisible_to_everything_but_identity() -> None:
    """Right content, right count, right banner, wrong chart.

    This is the family a task-success verifier cannot catch. Cardinality
    matches gold, the content check PASSes, the screen PASSes. Only the
    read that resolves the record by ``oracle_identity`` FAILs it.
    """
    for cell in ("mockmed:wrong_record", "openemr:wrong_record"):
        report = run_cell(cell)
        assert report.gold == "FAIL"
        assert report.new_count == report.spec_count == 1
        assert report.content_only == "PASS"
        assert report.visual_only == "PASS"
        assert report.identity == "FAIL"
        assert report.sor == "FAIL"
        assert report.seal == REFUSED
        assert any("oracle_identity" in reason for reason in report.sor_reasons)


def test_a_content_only_oracle_pays_the_wrong_record_write() -> None:
    """The negative control. Drop identity resolution and the family scores.

    An oracle that reads the store but resolves nothing by identity sees a
    correct write. That is the hole this family exists to measure; if this
    test ever fails, the content check grew an identity notion and the
    demonstration is no longer honest.
    """
    store = MockMedStore()
    before, after, screen = apply(store, MOCKMED_GOLD, "wrong_record")
    assert content_only_check(MOCKMED_GOLD, after).ok
    assert visual_only_check(screen).ok
    assert len(new_records(before, after)) == MOCKMED_GOLD.expected_new
    assert not identity_check(MOCKMED_GOLD, before, after).ok
    assert not sor_check(MOCKMED_GOLD, before, after).ok


def test_wrong_record_stays_out_of_the_frozen_corpus() -> None:
    """The kill-scan corpus and the Phase-1 M-freeze pin OPERATORS.

    A family added after that freeze belongs in EVAL_ONLY_OPERATORS, so
    adding it neither redigests the frozen corpus nor amends the freeze.
    """
    assert "wrong_record" in EVAL_ONLY_OPERATORS
    assert "wrong_record" not in OPERATORS
    assert "wrong_record" not in MUTANTS
    assert set(cells()).isdisjoint(eval_only_cells())
    assert "mockmed:wrong_record" in all_cells()
    assert "mockmed:wrong_record" not in cells()


def test_frozen_operator_reasons_did_not_move() -> None:
    """Identity resolution must not add a reason where cardinality already spoke.

    proof_2026-09-02.json pins sor_reasons for the frozen families.
    """
    assert run_cell("mockmed:control").sor_reasons == ()
    assert run_cell("mockmed:dup").sor_reasons == ("|new(M)|=2 != |spec(M)|=1",)
    assert run_cell("mockmed:extra").sor_reasons == ("extra field(s): priority",)


def test_identity_and_decoy_never_coincide() -> None:
    for spec in (MOCKMED_GOLD,):
        assert identity_of(spec) != decoy_of(spec)
        assert set(decoy_of(spec)) == set(identity_of(spec))


def test_check_invariants_hold() -> None:
    assert check_invariants() == []


def test_python_module_check() -> None:
    assert main(["check"]) == 0
    assert main(["list"]) == 0
    assert main(["run", "mockmed:dup"]) == 0
    assert main(["run", "mockmed:control"]) == 0


def test_identity_is_resolved_even_when_the_count_agrees() -> None:
    """`expected_new=2`, one correct row and one on the decoy patient.

    Guarding the identity resolution on "nothing landed under the contract
    identity" hid this: `sor_check` returned PASS while `identity_check`
    returned FAIL and named the decoy. Both gold specs write one record,
    where a right count forces a right subject, so no shipped fixture could
    reach it.
    """
    spec = replace(MOCKMED_GOLD, expected_new=2)
    identity = identity_of(spec)
    decoy = decoy_of(spec)
    before = [{"id": "e0", **identity, "type": "Triage", "note": "old"}]
    content = {key: value for key, value in spec.fields.items() if key not in identity}
    correct = {"id": "e1", **identity, **content}
    elsewhere = {"id": "e2", **decoy, **content}

    split = sor_check(spec, before, before + [correct, elsewhere])
    assert split.verdict == "FAIL"
    assert identity_check(spec, before, before + [correct, elsewhere]).verdict == "FAIL"
    reasons = ";".join(split.reasons)
    assert "under oracle_identity" in reasons
    assert str(decoy) in reasons

    both_right = {"id": "e3", **identity, **content}
    assert sor_check(spec, before, before + [correct, both_right]).verdict == "PASS"


def test_the_wording_rule_never_decides_the_verdict() -> None:
    """`sor_check` suppresses a redundant sentence, never a check.

    The identity reason is left out when every added row is already under
    the contract identity, because the cardinality line then reports the
    same number about the same rows. Whenever identity resolution fails,
    one of the two lines fires.
    """
    spec = replace(MOCKMED_GOLD, expected_new=2)
    identity = identity_of(spec)
    decoy = decoy_of(spec)
    content = {key: value for key, value in spec.fields.items() if key not in identity}
    rows = {
        "right": {**identity, **content},
        "wrong": {**decoy, **content},
    }
    for count in range(4):
        for places in product(("right", "wrong"), repeat=count):
            after = [
                {"id": f"e{index}", **rows[place]} for index, place in enumerate(places)
            ]
            landed = len(records_under(identity, after))
            result = sor_check(spec, [], after)
            assert result.ok == (
                landed == spec.expected_new and len(after) == spec.expected_new
            ), (places, result)
