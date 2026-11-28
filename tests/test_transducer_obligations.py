from __future__ import annotations

import pytest

from openadapt.transducer_obligations import (
    MicroHandoffResolution,
    ObligationStatus,
    apply_human_resolution,
    build_micro_handoff,
    compile_obligations_from_spec,
    diff_readback_against_obligations,
    mark_drafted_from_action,
)

PROMPT = (
    "Submit prior auth REF-2024-0017 for MRN-889123, CPT 99213 and J1745, "
    "diagnosis E11.9 on 2025-03-04 (also 03/05/2025), cost $1,250.00, "
    'note must say "failed conservative therapy".'
)


def by_kind(obs):
    d = {}
    for o in obs:
        d.setdefault(o.kind, []).append(o.expected_value)
    return d


def test_extraction():
    k = by_kind(compile_obligations_from_spec(PROMPT))
    assert sorted(k["identifier"]) == ["MRN-889123", "REF-2024-0017"]
    assert k["cpt"] == ["99213"]
    assert k["hcpcs"] == ["J1745"]
    assert k["icd10"] == ["E11.9"]
    assert sorted(k["date"]) == ["03/05/2025", "2025-03-04"]
    assert k["amount"] == ["$1,250.00"]
    assert k["phrase"] == ["failed conservative therapy"]


def test_ids_not_double_counted_as_icd():
    k = by_kind(compile_obligations_from_spec("Use PA-A12 now"))
    assert "icd10" not in k


def test_structured_fields():
    obs = compile_obligations_from_spec(
        "",
        {"patient": "Jane Roe", "claim": "CLM-77", "n": 3, "skip": None, "x": {"a": 1}},
    )
    ids = {o.field_id: o for o in obs}
    assert set(ids) == {"field:patient", "field:claim", "field:n"}
    assert ids["field:claim"].exact_match and not ids["field:patient"].exact_match


def test_drafted_tracking():
    obs = compile_obligations_from_spec(PROMPT)
    ids = mark_drafted_from_action(obs, "type E11.9 into diagnosis")
    assert ids == ["icd10:E11.9"]
    assert next(o for o in obs if o.kind == "cpt").status is ObligationStatus.PENDING
    assert mark_drafted_from_action(obs, "E11.9") == []


@pytest.mark.parametrize("kw", [{"is_toast": True}, {"is_editable": True}])
def test_rejects_toast_and_editable(kw):
    obs = compile_obligations_from_spec(PROMPT)
    r = diff_readback_against_obligations(obs, PROMPT, **kw)
    assert not r.all_verified and r.coverage_ratio == 0.0
    assert len(r.missing_obligations) == len(obs)


def test_partial_coverage_refused_and_prompt_lists_only_missing():
    obs = compile_obligations_from_spec(PROMPT)
    mark_drafted_from_action(obs, "99213")
    r = diff_readback_against_obligations(obs, "REF-2024-0017 MRN-889123 e11.9")
    assert not r.all_verified and r.coverage_ratio < 1.0
    miss = {o.expected_value for o in r.missing_obligations}
    assert "E11.9" in miss  # codes are case-sensitive
    assert "REF-2024-0017" not in miss and "REF-2024-0017" not in r.reason
    assert "HOLD_DONE" in r.reason and "99213" in r.reason
    cpt = next(o for o in obs if o.kind == "cpt")
    assert cpt.status is ObligationStatus.PENDING_EFFECT
    assert next(o for o in obs if o.kind == "hcpcs").status is ObligationStatus.MISSING


def test_full_coverage_verifies_with_normalised_free_text():
    obs = compile_obligations_from_spec(PROMPT)
    text = (
        "REF-2024-0017 MRN-889123 99213 J1745 E11.9 2025-03-04 03/05/2025 "
        "$1,250.00 Failed   Conservative Therapy"
    )
    r = diff_readback_against_obligations(obs, text)
    assert r.all_verified and r.coverage_ratio == 1.0 and not r.missing_obligations
    assert all(o.status is ObligationStatus.VERIFIED for o in obs)


def test_empty_obligations_vacuous():
    assert diff_readback_against_obligations([], "x").all_verified


def test_handoff_round_trip():
    obs = compile_obligations_from_spec("code J1745")
    t = build_micro_handoff(
        run_id="r1",
        task_id="t1",
        reason="stalled",
        blocker_kind="stalled_selector",
        current_url="https://x/y",
        missing_obligations=obs,
        stalled_selectors=["#old", "#keep"],
    )
    assert t.missing_obligations == ["HCPCS code: J1745"]
    assert "J1745" in t.suggested_human_action
    patch = apply_human_resolution(
        t, MicroHandoffResolution(t.ticket_id, True, {"click": "#new"}, "#keep")
    )
    assert patch["applied"] and patch["selector_hints"][0]["selector"] == "#keep"
    assert patch["avoid_selectors"] == ["#old"]
    assert not apply_human_resolution(t, MicroHandoffResolution(t.ticket_id, False))[
        "applied"
    ]
    with pytest.raises(ValueError):
        apply_human_resolution(t, MicroHandoffResolution("other", True))
