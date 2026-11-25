"""One ladder: the same evidence gets the same level, word and basis on
every path (the runtime's report, verify-run, the tx engine and Execute v1).

``openadapt_types.check_level`` is the only source of levels; this file
checks that each path's own tables are derived from it and that its receipt
words agree.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openadapt_types.check_level import (
    BASES,
    VERIFIED_LEVELS,
    CheckLevel,
    from_effect_strength,
    level_for_basis,
    outcome_word,
    to_oracle_tier,
    to_verification_tier,
)
from openadapt_types.evidence_scope import (
    build_evidence_scope,
    predicate_evidence_scope_problem,
    with_evidence_scope,
)
from openadapt_types.execute import _EFFECT_STRENGTH_RANK

import openadapt_flow.verify_run as verify_run_mod
from openadapt_flow.evidence_basis import (
    basis_for_tier,
    evidence_scope_for_run,
    level_and_basis,
)
from openadapt_flow.readback_ladder import summarize_report, tier_floor
from openadapt_flow.tx.api_readback import (
    api_readback_basis,
    served_from_cache,
    tx_evidence_scope,
)
from openadapt_flow.verification import (
    VERIFIED_EFFECT_TIER,
    VerificationTier,
    oracle_tier_from_verification_tier,
)

H = "sha256:" + "0" * 64
T = "2026-11-21T12:00:00Z"


# -- every path's tables come from the one ladder ------------------------------


def test_path_tables_are_derived_from_check_level() -> None:
    for tier in VerificationTier:
        level = tier.check_level
        assert to_verification_tier(level) == int(tier)
        assert oracle_tier_from_verification_tier(tier) == to_oracle_tier(level)
        strength = verify_run_mod._STRENGTH_BY_TIER[tier]
        assert from_effect_strength(strength) is level
        assert verify_run_mod._STRENGTH_RANK[strength] == int(level)
    for strength, rank in _EFFECT_STRENGTH_RANK.items():
        assert from_effect_strength(strength) == rank
    # The runtime's VERIFIED floor is the ladder's weakest VERIFIED level.
    assert VERIFIED_EFFECT_TIER.check_level is min(VERIFIED_LEVELS)
    assert tier_floor("separate_session").check_level is CheckLevel.SEPARATE_SESSION
    assert tier_floor("system_of_record").check_level is CheckLevel.SYSTEM_OF_RECORD


def test_each_basis_names_one_level() -> None:
    assert level_for_basis("separate_session") is CheckLevel.SEPARATE_SESSION
    for basis in BASES[1:]:
        assert level_for_basis(basis) is CheckLevel.SYSTEM_OF_RECORD
    with pytest.raises(ValueError):
        level_for_basis("screen")
    with pytest.raises(ValueError):
        outcome_word(CheckLevel.SEPARATE_SESSION, "system_api")


@pytest.mark.parametrize(
    ("level", "basis", "confirmed", "attested", "word"),
    [
        (CheckLevel.SYSTEM_OF_RECORD, "system_api", True, False, "VERIFIED"),
        (CheckLevel.SYSTEM_OF_RECORD, "activity_log", True, False, "VERIFIED"),
        (CheckLevel.SEPARATE_SESSION, "separate_session", True, False, "VERIFIED"),
        # No basis: a level 3 or 4 read is never VERIFIED.
        (CheckLevel.SYSTEM_OF_RECORD, None, True, False, "RECORDED"),
        (CheckLevel.REOPENED, None, True, False, "SHOWN"),
        (CheckLevel.SAME_SCREEN, None, True, False, "SHOWN"),
        (CheckLevel.COUNTERPARTY_CONFIRMED, None, True, False, "CONFIRMED"),
        (CheckLevel.UNCHECKED, None, True, True, "ATTESTED"),
        (CheckLevel.UNCHECKED, None, True, False, "RECORDED"),
        (CheckLevel.SYSTEM_OF_RECORD, "db_frame", False, False, "RECORDED"),
    ],
)
def test_outcome_word(level, basis, confirmed, attested, word) -> None:
    assert outcome_word(level, basis, confirmed=confirmed, attested=attested) == word


def test_a_raised_minimum_keeps_a_separate_session_read_from_verified() -> None:
    assert (
        outcome_word(
            CheckLevel.SEPARATE_SESSION,
            "separate_session",
            minimum=CheckLevel.SYSTEM_OF_RECORD,
        )
        == "RECORDED"
    )
    # A minimum can raise the floor, never lower it below separate_session.
    assert (
        outcome_word(CheckLevel.REOPENED, None, minimum=CheckLevel.SAME_SCREEN)
        == "SHOWN"
    )


# -- the same evidence, every path ---------------------------------------------


def _runtime_scope(tier: int) -> dict:
    """What the runtime's report.json says, and the run receipt's block."""

    report = SimpleNamespace(
        results=[
            SimpleNamespace(
                step_id="save",
                effect_evidence=[
                    SimpleNamespace(
                        verification_tier=tier,
                        final_verdict="confirmed",
                        observed_effect="present",
                    )
                ],
                effect_contract_hashes=[H],
            )
        ]
    )
    readback = summarize_report(report, VERIFIED_EFFECT_TIER)
    assert readback is not None
    # The runtime's rule: VERIFIED at or above its floor.
    outcome = (
        "VERIFIED"
        if VerificationTier(tier).satisfies(VERIFIED_EFFECT_TIER)
        else "COMPLETED_UNVERIFIED"
    )
    return evidence_scope_for_run(readback, outcome, read_back_at=T)


def _verify_run_scope(tier: VerificationTier, tmp_path) -> dict:
    from openadapt_flow.runtime.effects import adapter as adapter_mod
    from openadapt_flow.runtime.effects.adapter import register_verifier_factory
    from tests.test_verify_run import AGENT_RECORD, PARAMS, _fake_cfg, _FakeVerifier

    saved = dict(adapter_mod._REGISTRY)
    try:
        register_verifier_factory(
            "fake-ladder", lambda cfg, params: _FakeVerifier(tier), replace=True
        )
        cfg = _fake_cfg("fake-ladder", minimum_effect_strength="independent_session")
        baseline = tmp_path / f"baseline-{int(tier)}.json"
        verify_run_mod.capture_baseline(cfg, PARAMS, baseline)
        result = verify_run_mod.verify_run(
            cfg,
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            baseline_path=baseline,
            private_dir=tmp_path / f"private-{int(tier)}",
        )
    finally:
        adapter_mod._REGISTRY.clear()
        adapter_mod._REGISTRY.update(saved)
    receipt = result["receipt"]
    assert verify_run_mod.receipt_problem(receipt) is None
    return receipt["evidence_scope"]


def _key(scope: dict) -> tuple:
    return scope["check_level"], scope["word"], scope.get("basis")


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (
            VerificationTier.INDEPENDENT_SESSION,
            ("separate_session", "VERIFIED", "separate_session"),
        ),
        (
            VerificationTier.INDEPENDENT_SYSTEM,
            ("system_of_record", "VERIFIED", "owned_connection"),
        ),
    ],
)
def test_same_evidence_same_level_word_and_basis_on_every_path(
    tier, expected, tmp_path
) -> None:
    runtime = _key(_runtime_scope(int(tier)))
    verify = _key(_verify_run_scope(tier, tmp_path))
    level, basis = level_and_basis(int(tier), confirmed=True)
    direct = (level.label, outcome_word(level, basis), basis)
    assert runtime == verify == direct == expected


def test_screen_reads_are_shown_and_never_verified_on_every_path(tmp_path) -> None:
    for tier in (
        VerificationTier.PERSISTED_STATE_REACQUISITION,
        VerificationTier.IMMEDIATE_SCREEN,
    ):
        runtime = _key(_runtime_scope(int(tier)))
        verify = _key(_verify_run_scope(tier, tmp_path))
        assert runtime == verify == (tier.check_level.label, "SHOWN", None)


def test_verify_run_names_db_frame_for_a_sql_read() -> None:
    assert basis_for_tier(1, ["sql"]) == "db_frame"
    assert basis_for_tier(1, ["rest"]) == "owned_connection"
    assert basis_for_tier(2, ["ui_readback"]) == "separate_session"
    assert basis_for_tier(3) is None and basis_for_tier(None) is None


# -- tx: a cache-bypassed, version-checked API read is system_api --------------


def _api(**overrides):
    args = {
        "operation": "update",
        "r0": {"id": "1", "version": 4},
        "r1": {"id": "1", "version": 5},
        "headers": {"ETag": '"v5"'},
        "status": 200,
        "version_field": "version",
        "occ_status": "UNKNOWN",
        "cache_bypassed": True,
    }
    args.update(overrides)
    return api_readback_basis(**args)


def test_tx_api_read_is_system_api_only_when_bypassed_and_version_checked() -> None:
    assert _api() == "system_api"
    assert _api(operation="create") is None
    assert _api(cache_bypassed=False) is None
    assert _api(version_field=None) is None
    assert _api(r1={"id": "1", "version": 6}) is None  # another write landed
    assert _api(r1={"id": "1", "version": True}) is None
    assert _api(occ_status="CONCURRENT_MODIFICATION") is None
    assert _api(status=304) is None
    for headers in (
        {"Age": "12"},
        {"X-Cache": "HIT from edge"},
        {"CF-Cache-Status": "STALE"},
        {"Warning": '110 - "Response is Stale"'},
    ):
        assert served_from_cache(headers, 200), headers
        assert _api(headers=headers) is None
    assert not served_from_cache({"Age": "0", "X-Cache": "MISS"}, 200)


def test_tx_scope_matches_the_other_paths() -> None:
    api = tx_evidence_scope(
        outcome="CONFIRMED", basis="system_api", read_back=True, wire_observed=True
    )
    assert _key(api) == ("system_of_record", "VERIFIED", "system_api")
    assert api["wire"] == {"channel": "http_proxy", "protocols_parsed": ["http"]}
    atomic = tx_evidence_scope(
        outcome="CONFIRMED",
        basis="owned_connection",
        read_back=True,
        wire_observed=False,
    )
    # The same basis verify-run names for its own system-of-record read.
    assert _key(atomic) == ("system_of_record", "VERIFIED", "owned_connection")
    unqualified = tx_evidence_scope(
        outcome="UNCERTAIN", basis=None, read_back=True, wire_observed=True
    )
    assert _key(unqualified) == ("system_of_record", "RECORDED", None)


# -- the record/run/v1 block is additive ---------------------------------------


def test_old_predicates_without_the_block_still_pass() -> None:
    old = {"outcome": "VERIFIED", "model_calls": 0}
    assert predicate_evidence_scope_problem(old) is None


def test_block_must_agree_with_the_predicate_outcome() -> None:
    verified = build_evidence_scope(
        CheckLevel.SEPARATE_SESSION, basis="separate_session", read_back_at=T
    )
    assert with_evidence_scope({"outcome": "VERIFIED"}, verified)["evidence_scope"]
    assert (
        predicate_evidence_scope_problem(
            {"outcome": "UNKNOWN", "evidence_scope": verified}
        )
        == "evidence_scope_outcome"
    )
    shown = build_evidence_scope(CheckLevel.REOPENED, read_back_at=T)
    assert (
        predicate_evidence_scope_problem(
            {"outcome": "VERIFIED", "evidence_scope": shown}
        )
        == "evidence_scope_outcome"
    )
    for broken in (
        dict(verified, not_covered=["changes_after_read_back"]),
        dict(verified, basis="system_api"),
        {k: v for k, v in verified.items() if k != "read_back_at"},
        dict(verified, word="CONFIRMED"),
        dict(verified, extra=1),
    ):
        assert (
            predicate_evidence_scope_problem(
                {"outcome": "VERIFIED", "evidence_scope": broken}
            )
            == "evidence_scope"
        ), broken


def test_every_block_lists_what_no_read_back_sees() -> None:
    for level in CheckLevel:
        block = build_evidence_scope(level, read_back_at=T)
        assert block["not_covered"][:2] == [
            "server_side_async_effects",
            "outbound_effects",
        ]


# -- the run receipt (record/run/v1) -------------------------------------------


def _run_report(checked_how, outcome="VERIFIED"):
    report = {
        "workflow_name": "ladder-test",
        "started_at": "2026-11-21T12:34:56Z",
        "execution_outcome": outcome,
        "model_calls": 0,
        "results": [],
    }
    if checked_how is not None:
        report["readback"] = {
            "minimum": "separate_session",
            "checked_how": checked_how,
            "label": checked_how,
            "below_minimum": False,
            "saves": [],
        }
    return report


def _statement(report):
    from openadapt_types import receipt as r

    from openadapt_flow.receipt_record import build_statement

    statement, _ = build_statement(report, "1" * 64, salt=lambda: "2" * 64)
    # The signer's own check: refuses a block that disagrees with the outcome.
    r.statement_payload(statement)
    return statement["predicate"]


@pytest.mark.parametrize(
    ("checked_how", "word", "basis"),
    [
        ("separate_session", "VERIFIED", "separate_session"),
        ("system_of_record", "VERIFIED", "owned_connection"),
    ],
)
def test_run_receipt_states_its_evidence_scope(checked_how, word, basis) -> None:
    predicate = _statement(_run_report(checked_how))
    scope = predicate["evidence_scope"]
    assert predicate["outcome"] == "VERIFIED"
    assert (scope["check_level"], scope["word"], scope["basis"]) == (
        checked_how,
        word,
        basis,
    )
    # The report has no per-read time, and the block says so.
    assert "read_back_at" not in scope
    assert "read_back_time" in scope["not_covered"]
    # Same level, word and basis as the runtime and verify-run paths.
    assert _key(scope) == _key(
        _runtime_scope(to_verification_tier(CheckLevel.from_label(checked_how)))
    )


def test_run_receipt_never_says_verified_below_level_3() -> None:
    predicate = _statement(_run_report("reopened"))
    assert predicate["outcome"] == "UNKNOWN"
    assert predicate["reason_code"] == "below_verified_level"
    # A confirmed level 2 re-read is SHOWN, never VERIFIED.
    assert predicate["evidence_scope"]["word"] == "SHOWN"


def test_run_receipt_without_a_readback_block_keeps_its_old_shape() -> None:
    from openadapt_types import receipt as r

    predicate = _statement(_run_report(None))
    assert predicate["outcome"] == "VERIFIED"
    assert "evidence_scope" not in predicate
    # And a statement without the block serializes with no trace of it.
    statement, _ = __import__(
        "openadapt_flow.receipt_record", fromlist=["build_statement"]
    ).build_statement(_run_report(None), "1" * 64, salt=lambda: "2" * 64)
    assert b"evidence_scope" not in r.statement_payload(statement)


def test_stopped_run_receipt_is_recorded() -> None:
    predicate = _statement(_run_report("separate_session", outcome="HALTED"))
    assert predicate["outcome"] == "STOPPED"
    assert predicate["evidence_scope"]["word"] == "RECORDED"
    assert "basis" not in predicate["evidence_scope"]
