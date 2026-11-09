"""Known-ID list cross-check for look-alike identifier abstains.

String-level unit tests of ``runtime.known_ids``. The replayer-level test
with a real render and real OCR lives in ``test_known_ids_replayer.py``.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.ir import IdentityCheck
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.known_ids import (
    KnownIdList,
    KnownIdListError,
    lookalike_class,
    resolve_lookalike_abstain,
)

BAND = "Harrington, James 03/14/1961 MG4408 M Active"


def _list(*ids: str, complete: bool = True) -> KnownIdList:
    return KnownIdList(ids=ids, source="synthetic test export", complete=complete)


def _reads(observed: str = BAND, n: int = 2) -> list[IdentityCheck]:
    return [I.verify_target_identity(BAND, observed) for _ in range(n)]


def test_baseline_abstains_for_lookalike_reason():
    check = I.verify_target_identity(BAND, BAND)
    assert check.status == "abstain"
    assert check.abstain_reason == "glyph_confusable_identifier"


def test_clean_identifier_has_no_abstain_reason():
    check = I.verify_target_identity(
        "Harrington, James 03/14/1961 RC79284 M Active",
        "Harrington, James 03/14/1961 RC79284 M Active",
    )
    assert check.status == "verified"
    assert check.abstain_reason is None


def test_lifts_when_class_has_one_listed_member():
    ids = _list("MG4408", "RC79284", "AC5OO61")
    lifted = resolve_lookalike_abstain(_reads(), ids)
    assert lifted is not None
    assert lifted.status == "verified"
    assert lifted.abstain_reason is None
    ev = lifted.known_id_evidence
    assert ev is not None and ev.reads == 2 and ev.resolved_ids == 1
    assert ev.list_size == 3 and ev.list_sha256 == ids.sha256
    # No identifier values are copied into the evidence.
    assert "mg4408" not in ev.model_dump_json().lower()


def test_keeps_abstain_when_a_lookalike_twin_is_listed():
    assert resolve_lookalike_abstain(_reads(), _list("MG4408", "MG44O8")) is None


@pytest.mark.parametrize("twin", ["MG44O8", "MG440B", "MG4-4O8"])
def test_twin_classes_cover_o0_and_b8(twin):
    # O/0 and B/8 are look-alike pairs; a separator never makes a new ID.
    assert lookalike_class(twin) == lookalike_class("MG4408")
    assert resolve_lookalike_abstain(_reads(), _list("MG4408", twin)) is None


def test_non_lookalike_neighbour_does_not_block():
    assert resolve_lookalike_abstain(_reads(), _list("MG4408", "MG4409")) is not None


def test_refuses_incomplete_list():
    assert resolve_lookalike_abstain(_reads(), _list("MG4408", complete=False)) is None


def test_refuses_without_list():
    assert resolve_lookalike_abstain(_reads(), None) is None


def test_needs_two_reads():
    assert resolve_lookalike_abstain(_reads(n=1), _list("MG4408")) is None


def test_refuses_when_identifier_missing_from_list():
    assert resolve_lookalike_abstain(_reads(), _list("RC79284")) is None


def test_never_overrides_mismatch():
    reads = [
        I.verify_target_identity(BAND, BAND),
        I.verify_target_identity(BAND, "Okafor, Maria 07/02/1975 MG4408 F Active"),
    ]
    assert reads[1].status == "mismatch"
    assert resolve_lookalike_abstain(reads, _list("MG4408")) is None


def test_never_lifts_evidence_short_abstain():
    check = IdentityCheck(
        status="abstain", abstain_reason="identity_evidence_short", observed=BAND
    )
    assert resolve_lookalike_abstain([check, check], _list("MG4408")) is None


def test_reads_must_agree_on_identifier_class():
    a = I.verify_target_identity(BAND, BAND)
    b = a.model_copy(update={"observed": BAND.replace("MG4408", "MG4408 ZX1001")})
    assert resolve_lookalike_abstain([a, b], _list("MG4408", "ZX1001")) is None


def test_every_identifier_token_must_resolve():
    observed = BAND + " ZX1001"
    reads = [
        IdentityCheck(
            status="abstain",
            abstain_reason="glyph_confusable_identifier",
            expected=observed,
            observed=observed,
        )
    ] * 2
    assert resolve_lookalike_abstain(reads, _list("MG4408")) is None
    assert resolve_lookalike_abstain(reads, _list("MG4408", "ZX1001")) is not None


def test_param_value_must_be_the_resolved_member():
    ids = _list("MG4408", "RC79284")
    assert resolve_lookalike_abstain(_reads(), ids, run_values={"id": "MG4408"})
    # A run value in the same class that is not the listed member is refused.
    assert resolve_lookalike_abstain(_reads(), ids, run_values={"id": "MG44O8"}) is None


def test_template_path_sets_abstain_reason():
    from openadapt_flow.runtime import identity_template as T

    tmpl = T.build_identity_template(BAND)
    check = T.verify_template_identity(tmpl, BAND)
    assert check.status == "abstain"
    assert check.abstain_reason == "glyph_confusable_identifier"
    # The template holds no plaintext, so the caller says how to match the
    # recorded identifier.
    assert resolve_lookalike_abstain([check, check], _list("MG4408")) is None
    lifted = resolve_lookalike_abstain(
        [check, check],
        _list("MG4408"),
        recorded_id_matches=lambda m: T.recorded_identifier_matches(tmpl, m),
    )
    assert lifted is not None


def test_stale_list_holding_only_a_lookalike_does_not_lift():
    # The record MG4408 was added after the export. The list still claims to
    # be complete and holds only a different record, MG44O8, in the same
    # look-alike class. Resolving to it would verify the wrong record.
    stale = _list("MG44O8", "RC79284")
    assert resolve_lookalike_abstain(_reads(), stale) is None


def test_stale_list_holding_only_a_lookalike_does_not_lift_from_template():
    from openadapt_flow.runtime import identity_template as T

    tmpl = T.build_identity_template(BAND)
    check = T.verify_template_identity(tmpl, BAND)
    assert not T.recorded_identifier_matches(tmpl, "mg44o8")
    assert T.recorded_identifier_matches(tmpl, "mg4408")
    lifted = resolve_lookalike_abstain(
        [check, check],
        _list("MG44O8"),
        recorded_id_matches=lambda m: T.recorded_identifier_matches(tmpl, m),
    )
    assert lifted is None


def test_load_requires_complete_and_source(tmp_path):
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"source": "export", "ids": ["A1"]}))
    with pytest.raises(KnownIdListError):
        KnownIdList.load(p)
    p.write_text(json.dumps({"complete": True, "ids": ["A1"]}))
    with pytest.raises(KnownIdListError):
        KnownIdList.load(p)
    p.write_text(json.dumps({"complete": True, "source": "export", "ids": ["A1"]}))
    assert KnownIdList.load(p).size == 1


def test_duplicates_and_case_collapse():
    ids = _list("MG4408", "mg4408", "MG-4408")
    assert ids.size == 1
