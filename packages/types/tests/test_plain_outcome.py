"""The plain-result mapping keeps the safety rule for every input it can get.

Three kinds of test:

* coverage: every outcome value an emitter can send maps to a result, so a
  new engine value fails here before it reaches a person unmapped;
* conformance: the hand-written cases in outcome-vectors.json, which other
  languages run too;
* safety: across every combination of inputs, nothing that might have
  written is ever reported as "nothing changed" or "safe to retry".
"""

from __future__ import annotations

import importlib
import itertools
import json
import pkgutil
import re
from collections import defaultdict
from enum import Enum
from importlib.resources import files
from typing import Any

import pytest
from pydantic import ValidationError

import openadapt_types
from openadapt_types import (
    CONTROL_OVERLAY_TERMINAL_PHASES,
    ClinicToolStatusV1,
    ControlOverlayPhase,
    EffectStrengthV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteLifecycleStateV1,
    ExecuteStatusV1,
    ExecuteTerminalOutcomeV1,
    ExecuteWebhookEventTypeV1,
    OutcomeSourceV1,
    PlainOutcomeInProgress,
    PlainOutcomeV1,
    PlainProgressV1,
    PlainProofV1,
    PlainResultV1,
    plain_outcome,
    plain_outcome_for_execute_receipt,
    plain_outcome_for_execute_status,
    plain_outcome_for_execute_webhook,
    plain_outcome_for_run_report,
    sign_execute_webhook_hmac,
)
from openadapt_types.vocabulary import known_values, load_outcome_vectors, load_registry
from openadapt_types.vocabulary.lint import check_coverage

VOCABULARY, TABLE = load_registry()
SOURCES = [source.value for source in OutcomeSourceV1]
NO_EFFECT = {"stopped_before_change", "refused", "did_not_finish"}


def _resolve(inputs: dict[str, Any]) -> PlainOutcomeV1 | PlainProgressV1:
    try:
        return plain_outcome(**inputs)
    except PlainOutcomeInProgress as exc:
        return exc.progress


# ---------------------------------------------------------------- coverage


def test_sources_match_the_table_in_precedence_order() -> None:
    assert SOURCES == TABLE["precedence"]


@pytest.mark.parametrize(
    ("enum_cls", "source"),
    [
        (ExecuteTerminalOutcomeV1, "execute_terminal_outcome"),
        (ExecuteLifecycleStateV1, "execute_state"),
        (ClinicToolStatusV1, "clinic_tool_status"),
        (ControlOverlayPhase, "overlay_phase"),
    ],
)
def test_every_value_of_this_packages_enums_is_mapped(enum_cls: type, source: str) -> None:
    # A new member added to one of these enums fails here until outcomes.json
    # gives it a plain result. A removed member fails as a stale row.
    assert {member.value for member in enum_cls} == known_values(TABLE, source)


@pytest.mark.parametrize(
    "spec",
    [
        "openadapt_types.execute:ExecuteTerminalOutcomeV1=execute_terminal_outcome",
        "openadapt_types.execute:ExecuteLifecycleStateV1=execute_state",
        "openadapt_types.clinic_job:ClinicToolStatusV1=clinic_tool_status",
        "openadapt_types.execute:ExecuteStatusV1.terminal_outcome=execute_terminal_outcome",
        "openadapt_types.clinic_job:ClinicToolResultV1.status=clinic_tool_status",
        "openadapt_types.control_overlay:ControlOverlayPhase=overlay_phase",
        "openadapt_types.control_overlay:ControlOverlayFrameV1.phase=overlay_phase",
        "openadapt_types.control_overlay_tracking:ControlOverlayFrameV2.phase=overlay_phase",
    ],
)
def test_the_coverage_lint_passes_on_this_packages_enums(spec: str) -> None:
    assert check_coverage(TABLE, enums=[spec]) == []


_TYPES_SYMBOL = re.compile(r"^(openadapt_types/[\w/]+\.py) (\w+)\b")
# Values that name how a run ended. An enum in this package that carries one
# either feeds plain_outcome() or is listed as something else on purpose.
_RUN_OUTCOME_MARKERS = {
    value.lower()
    for source in ("transaction_outcome", "execute_terminal_outcome", "execution_outcome")
    for value in known_values(TABLE, source)
}


def _this_packages_enums() -> list[type[Enum]]:
    found: list[type[Enum]] = []
    for info in pkgutil.walk_packages(openadapt_types.__path__, "openadapt_types."):
        module = importlib.import_module(info.name)
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Enum) and obj.__module__ == module.__name__:
                found.append(obj)
    return found


def _symbol_names(symbol: str, enum_cls: type[Enum]) -> bool:
    path = enum_cls.__module__.replace(".", "/") + ".py"
    return symbol.startswith(path + " ") and re.search(rf"\b{enum_cls.__name__}\b", symbol) is not None


def test_every_outcome_enum_in_this_package_is_mapped_or_set_aside() -> None:
    # A new enum here that names run outcomes fails until outcomes.json
    # either maps it (an emitter) or says why it isn't a run result.
    symbols = [e["symbol"] for e in TABLE["emitters"] if e["repository"] == "OpenAdaptAI/openadapt-types"]
    symbols += [entry["symbol"] for entry in TABLE["not_run_outcomes"]]
    outcome_enums = [
        enum_cls
        for enum_cls in _this_packages_enums()
        if {str(member.value).lower() for member in enum_cls} & _RUN_OUTCOME_MARKERS
    ]
    assert ControlOverlayPhase in outcome_enums and ExecuteTerminalOutcomeV1 in outcome_enums
    unaccounted = [
        f"{enum_cls.__module__}.{enum_cls.__name__}"
        for enum_cls in outcome_enums
        if not any(_symbol_names(symbol, enum_cls) for symbol in symbols)
    ]
    assert unaccounted == []


def test_this_packages_emitters_list_exactly_their_enum_values() -> None:
    checked = 0
    for emitter in TABLE["emitters"]:
        if emitter["repository"] != "OpenAdaptAI/openadapt-types":
            continue
        match = _TYPES_SYMBOL.match(emitter["symbol"])
        assert match, emitter["symbol"]
        module = importlib.import_module(match.group(1)[:-3].replace("/", "."))
        enum_cls = getattr(module, match.group(2))
        assert {member.value for member in enum_cls} == set(emitter["values"]), emitter["symbol"]
        checked += 1
    assert checked == 4


def test_every_source_lists_where_its_values_come_from() -> None:
    by_source: dict[str, set[str]] = defaultdict(set)
    for emitter in TABLE["emitters"]:
        by_source[emitter["source"]].update(emitter["values"])
    assert set(by_source) == set(SOURCES)
    for source in SOURCES:
        # Every value an emitter sends has a row, and no row maps a value no
        # emitter sends.
        assert by_source[source] == known_values(TABLE, source), source


def test_emitters_name_a_commit_they_were_read_at() -> None:
    for emitter in TABLE["emitters"]:
        assert re.search(r"origin/main [0-9a-f]{7,40}", emitter["read_at"]), emitter


def test_live_flow_enums_are_mapped_when_flow_is_installed() -> None:
    pytest.importorskip("openadapt_flow")
    specs = [
        "openadapt_flow.transaction:TransactionOutcome=transaction_outcome",
        "openadapt_flow.ir:RunReport.transaction_outcome=transaction_outcome",
        "openadapt_flow.ir:RunReport.execution_outcome=execution_outcome",
        "openadapt_flow.ir:ExecutionOutcomeEnvelope.outcome=execution_outcome",
        "openadapt_flow.ir:State.outcome=program_terminal_state",
    ]
    errors = [f for f in check_coverage(TABLE, enums=specs) if f.severity == "error"]
    assert errors == []


@pytest.mark.parametrize("source", SOURCES)
def test_every_known_value_gives_a_result_or_progress(source: str) -> None:
    for value in sorted(known_values(TABLE, source)):
        resolved = _resolve({source: value})
        assert isinstance(resolved, (PlainOutcomeV1, PlainProgressV1)), (source, value)


@pytest.mark.parametrize("source", SOURCES)
def test_an_unmapped_value_fails_closed(source: str) -> None:
    result = plain_outcome(**{source: "SOMETHING_NEW"})
    assert result.result is PlainResultV1.CHECK_THE_RECORD
    assert result.may_have_changed is True
    assert result.safe_to_retry is False
    assert result.technical_code == "fallback=unrecognized"


# ------------------------------------------------------------- conformance


VECTORS = load_outcome_vectors()["cases"]


@pytest.mark.parametrize("case", VECTORS, ids=[case["name"] for case in VECTORS])
def test_outcome_vectors(case: dict[str, Any]) -> None:
    inputs = dict(case["input"])
    if "expect_error" in case:
        with pytest.raises(ValueError, match=re.escape(case["expect_error"])):
            plain_outcome(**inputs)
        return
    resolved = _resolve(inputs)
    if "expect_progress" in case:
        assert isinstance(resolved, PlainProgressV1)
        assert resolved.state == case["expect_progress"]
        return
    assert isinstance(resolved, PlainOutcomeV1)
    expected = {"proof": "none", **case["expect"]}
    actual = resolved.model_dump(mode="json")
    assert {key: actual[key] for key in expected} == expected


def test_the_vectors_exercise_every_merge_rule() -> None:
    rules = {case["rule"] for case in VECTORS}
    assert {"row", *(str(number) for number in range(1, 12))} <= rules
    assert len(TABLE["merge_rules"]) == 11


# ------------------------------------------------------------------ safety


def _value_choices(source: str) -> list[str | None]:
    return [None, "SOMETHING_NEW", *sorted(known_values(TABLE, source))]


def _proves_no_effect(source: str, value: str, dispatched: bool | None) -> bool:
    row = TABLE["sources"][source]["values"].get(value)
    if row is None:
        return False
    if dispatched is False and "if_not_dispatched" in row:
        row = row["if_not_dispatched"]
    return row["may_have_changed"] is False


def _check_invariants(inputs: dict[str, Any], resolved: PlainOutcomeV1 | PlainProgressV1) -> None:
    if isinstance(resolved, PlainProgressV1):
        assert resolved.safe_to_retry is False
        return
    if resolved.safe_to_retry:
        assert not resolved.may_have_changed
    if resolved.result.value in NO_EFFECT and resolved.result is not PlainResultV1.DID_NOT_FINISH:
        assert not resolved.may_have_changed
    if not resolved.may_have_changed:
        # The safety rule: a no-change result needs every deciding input to
        # prove no effect, and no uncertain delivery.
        assert inputs.get("delivery_uncertain") is not True, inputs
        tier = next(t for t in TABLE["tiers"] if any(_present(inputs, s) for s in t))
        for source in tier:
            if _present(inputs, source) and inputs[source] not in TABLE["in_progress"].get(source, {}):
                assert _proves_no_effect(source, inputs[source], inputs.get("dispatched")), inputs
    # These outcomes never let a person think nothing happened.
    if inputs.get("transaction_outcome") in {
        "VERIFIED",
        "RECONCILIATION_REQUIRED",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    } or inputs.get("execute_terminal_outcome") in {
        "verified",
        "reconciliation_required",
        "rolled_back_verified",
    }:
        assert resolved.may_have_changed and not resolved.safe_to_retry, inputs
    if inputs.get("transaction_outcome") == "RECONCILIATION_REQUIRED":
        assert resolved.result is PlainResultV1.CHECK_THE_RECORD
    # A coarse halt alone is never "stopped before saving".
    precise = ("transaction_outcome", "execute_terminal_outcome", "execute_state")
    if not any(_present(inputs, s) for s in precise) and inputs.get("execution_outcome") == "HALTED":
        assert resolved.result is PlainResultV1.CHECK_THE_RECORD
    if inputs.get("delivery_uncertain") is True:
        assert resolved.result is PlainResultV1.CHECK_THE_RECORD
    assert resolved.exact.dispatched == inputs.get("dispatched")


def _present(inputs: dict[str, Any], source: str) -> bool:
    value = inputs.get(source)
    return value is not None and value not in TABLE["not_an_outcome"].get(source, [])


FLAGS = list(itertools.product([None, True, False], [None, True, False]))


def test_precise_and_coarse_combinations_keep_the_safety_rule() -> None:
    checked = 0
    for tx, ex, state, coarse in itertools.product(
        _value_choices("transaction_outcome"),
        _value_choices("execute_terminal_outcome"),
        _value_choices("execute_state"),
        _value_choices("execution_outcome"),
    ):
        for dispatched, uncertain in FLAGS:
            inputs = {
                "transaction_outcome": tx,
                "execute_terminal_outcome": ex,
                "execute_state": state,
                "execution_outcome": coarse,
                "agent_status": "halt",
                "dispatched": dispatched,
                "delivery_uncertain": uncertain,
            }
            _check_invariants(inputs, _resolve(inputs))
            checked += 1
    assert checked > 30_000


def test_coarse_status_combinations_keep_the_safety_rule() -> None:
    for agent, connector, clinic, program in itertools.product(
        _value_choices("agent_status"),
        _value_choices("connector_status"),
        _value_choices("clinic_tool_status"),
        _value_choices("program_terminal_state"),
    ):
        for dispatched, uncertain in FLAGS:
            inputs = {
                "agent_status": agent,
                "connector_status": connector,
                "clinic_tool_status": clinic,
                "program_terminal_state": program,
                "dispatched": dispatched,
                "delivery_uncertain": uncertain,
            }
            _check_invariants(inputs, _resolve(inputs))


def test_a_coarse_halt_never_reads_as_stopped_before_saving() -> None:
    for status in ("HALTED",):
        for result in (
            plain_outcome(execution_outcome=status),
            plain_outcome(clinic_tool_status=status),
            plain_outcome(agent_status="halt"),
            plain_outcome(connector_status="halt"),
            plain_outcome(program_terminal_state="halt"),
            plain_outcome(overlay_phase=ControlOverlayPhase.HALTED),
        ):
            assert result.result is PlainResultV1.CHECK_THE_RECORD
            assert "nothing was written" not in result.next_action.lower()


def test_coarse_and_overlay_combinations_keep_the_safety_rule() -> None:
    for coarse, overlay, agent in itertools.product(
        _value_choices("execution_outcome"),
        _value_choices("overlay_phase"),
        [None, "refused", "timeout", "halt"],
    ):
        for dispatched, uncertain in FLAGS:
            inputs = {
                "execution_outcome": coarse,
                "overlay_phase": overlay,
                "agent_status": agent,
                "dispatched": dispatched,
                "delivery_uncertain": uncertain,
            }
            _check_invariants(inputs, _resolve(inputs))


@pytest.mark.parametrize("phase", sorted(CONTROL_OVERLAY_TERMINAL_PHASES, key=lambda p: p.value))
def test_an_overlay_end_phase_reads_like_the_outcome_it_mirrors(phase: ControlOverlayPhase) -> None:
    # Flow emits each end phase from exactly one execution_outcome
    # (runtime/control_overlay.py), so the two must read the same.
    from_overlay = plain_outcome(overlay_phase=phase)
    from_outcome = plain_outcome(execution_outcome=phase.value.upper())
    fields = ("result", "label", "may_have_changed", "safe_to_retry", "next_action")
    assert {f: getattr(from_overlay, f) for f in fields} == {f: getattr(from_outcome, f) for f in fields}
    assert from_overlay.technical_code == f"overlay_phase={phase.value}"


def test_an_overlay_mid_run_has_no_result_yet() -> None:
    labels: dict[str, str] = {}
    for phase in ControlOverlayPhase:
        if phase in CONTROL_OVERLAY_TERMINAL_PHASES or phase.value in TABLE["not_an_outcome"]["overlay_phase"]:
            continue
        with pytest.raises(PlainOutcomeInProgress) as caught:
            plain_outcome(overlay_phase=phase)
        progress = caught.value.progress
        assert progress.technical_code == f"overlay_phase={phase.value}"
        assert progress.safe_to_retry is False
        labels[phase.value] = progress.label
    assert labels["executing"] == "Running"
    assert labels["paused"] == "Waiting for a person"


def test_only_rows_that_prove_no_effect_invite_a_retry() -> None:
    proving = {
        ("transaction_outcome", "HALTED_BEFORE_EFFECT"),
        ("transaction_outcome", "FAILED_PLATFORM"),
        ("transaction_outcome", "CANCELED"),
        ("transaction_outcome", "REJECTED_POLICY"),
        ("execute_terminal_outcome", "halted_before_effect"),
        ("execute_terminal_outcome", "rejected_policy"),
        ("execute_terminal_outcome", "failed_platform"),
        ("agent_status", "refused"),
    }
    retryable = {
        (source, value)
        for source, spec in TABLE["sources"].items()
        for value, row in spec["values"].items()
        if row["safe_to_retry"]
    }
    assert retryable == proving


# ------------------------------------------------------------- the model


def _fields(**updates: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "result": "stopped_before_change",
        "label": "Stopped before saving",
        "may_have_changed": False,
        "safe_to_retry": True,
        "proof": "none",
        "next_action": "Nothing was written. Look at why it stopped, fix that, then run it again.",
        "technical_code": "transaction_outcome=HALTED_BEFORE_EFFECT",
        "exact": {"transaction_outcome": "HALTED_BEFORE_EFFECT"},
    }
    fields.update(updates)
    return fields


def test_a_hand_built_result_that_keeps_the_rule_is_accepted() -> None:
    assert PlainOutcomeV1.model_validate(_fields()).result is PlainResultV1.STOPPED_BEFORE_CHANGE


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"may_have_changed": True, "safe_to_retry": False}, "may_have_changed must be false"),
        ({"result": "check_the_record", "label": "Check the record"}, "must keep may_have_changed=true"),
        (
            {
                "result": "check_the_record",
                "label": "Check the record",
                "may_have_changed": True,
                "safe_to_retry": True,
            },
            "safe_to_retry needs may_have_changed=false",
        ),
        ({"label": "Stopped safely"}, "is labeled"),
        (
            {
                "result": "did_not_finish",
                "label": "Didn't finish",
                "may_have_changed": True,
                "safe_to_retry": False,
                "next_action": "It stopped. Nothing was written.",
            },
            "something may have changed",
        ),
        ({"proof": "sealed"}, "signed receipt"),
        ({"technical_code": "status=halted"}, "technical_code"),
        ({"exact": {"agent_status": "the record did not change"}}, "agent_status"),
        (
            {"technical_code": "execution_outcome=HALTED", "exact": {"execution_outcome": "HALTED"}},
            "execution_outcome=HALTED reads as check_the_record",
        ),
        (
            {
                "result": "refused",
                "label": "Blocked before saving",
                "technical_code": "agent_status=halt",
                "exact": {"agent_status": "halt"},
            },
            "agent_status=halt reads as check_the_record",
        ),
        (
            {
                "result": "done",
                "label": "Done and checked",
                "may_have_changed": True,
                "safe_to_retry": False,
                "next_action": "Nothing else to do.",
                "technical_code": "connector_status=success",
                "exact": {"connector_status": "success"},
            },
            "connector_status=success reads as finished_not_checked",
        ),
        (
            {
                "result": "did_not_finish",
                "label": "Didn't finish",
                "technical_code": "agent_status=timeout",
                "exact": {"agent_status": "timeout"},
            },
            "agent_status=timeout reads as check_the_record",
        ),
        (
            {
                "exact": {
                    "transaction_outcome": "HALTED_BEFORE_EFFECT",
                    "execution_outcome": "VERIFIED",
                },
            },
            "isn't what the exact inputs decide",
        ),
        (
            {"exact": {"transaction_outcome": "HALTED_BEFORE_EFFECT", "delivery_uncertain": True}},
            "isn't what the exact inputs decide",
        ),
        ({"exact": {}}, "isn't what the exact inputs decide"),
        (
            {
                "result": "check_the_record",
                "label": "Check the record",
                "may_have_changed": True,
                "safe_to_retry": False,
                "next_action": "Check the record before anyone runs this again.",
                "technical_code": "fallback=unknown",
            },
            "names no fallback",
        ),
    ],
)
def test_a_hand_built_result_that_breaks_the_rule_is_refused(
    updates: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        PlainOutcomeV1.model_validate(_fields(**updates))


@pytest.mark.parametrize(
    "updates",
    [
        {
            "result": "did_not_finish",
            "label": "Didn't finish",
            "next_action": "The run never reached the app, so nothing was written. You can send it again.",
            "technical_code": "agent_status=timeout",
            "exact": {"agent_status": "timeout", "dispatched": False},
        },
        {
            "result": "check_the_record",
            "label": "Check the record",
            "may_have_changed": True,
            "safe_to_retry": False,
            "next_action": "Check the record before anyone runs this again.",
            "technical_code": "fallback=conflict",
        },
    ],
)
def test_a_hand_built_result_the_table_allows_is_accepted(updates: dict[str, Any]) -> None:
    PlainOutcomeV1.model_validate(_fields(**updates))


def test_every_vector_result_survives_a_json_round_trip() -> None:
    checked = 0
    for case in VECTORS:
        if "expect" not in case:
            continue
        resolved = _resolve(dict(case["input"]))
        assert isinstance(resolved, PlainOutcomeV1), case["name"]
        assert PlainOutcomeV1.model_validate_json(resolved.model_dump_json()) == resolved
        checked += 1
    assert checked > 30


def test_flags_must_be_real_booleans() -> None:
    with pytest.raises(TypeError, match="dispatched"):
        plain_outcome(agent_status="timeout", dispatched="false")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="delivery_uncertain"):
        plain_outcome(transaction_outcome="HALTED_BEFORE_EFFECT", delivery_uncertain=1)  # type: ignore[arg-type]


def test_enum_members_are_accepted_and_kept_as_exact_values() -> None:
    result = plain_outcome(
        execute_terminal_outcome=ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT,
        execute_state=ExecuteLifecycleStateV1.TERMINAL,
    )
    assert result.result is PlainResultV1.STOPPED_BEFORE_CHANGE
    assert result.exact.execute_terminal_outcome == "halted_before_effect"
    assert result.exact.execute_state == "terminal"


def test_free_text_never_reaches_exact() -> None:
    result = plain_outcome(agent_status="Jane Doe's chart did not change")
    assert result.technical_code == "fallback=unrecognized"
    assert result.exact.agent_status is None


def test_proof_must_be_a_known_level() -> None:
    with pytest.raises(ValueError):
        plain_outcome(transaction_outcome="VERIFIED", proof="notarized")


# ---------------------------------------------------------------- helpers


def test_run_report_helper_reads_report_json_fields_and_reports_local_proof() -> None:
    # The shape of the tutorial's break-it report.
    report = {
        "execution_outcome": "HALTED",
        "transaction_outcome": "RECONCILIATION_REQUIRED",
        "production_eligible": False,
        "success": False,
    }
    result = plain_outcome_for_run_report(report)
    assert result.result is PlainResultV1.CHECK_THE_RECORD
    assert result.proof is PlainProofV1.LOCAL
    assert result.exact.execution_outcome == "HALTED"


def test_run_report_helper_fails_closed_on_a_legacy_report() -> None:
    result = plain_outcome_for_run_report({"success": True})
    assert result.technical_code == "fallback=missing"
    assert result.may_have_changed is True
    with pytest.raises(TypeError):
        plain_outcome_for_run_report(["not", "a", "report"])  # type: ignore[arg-type]


def test_run_report_helper_uses_the_program_state_only_without_precise_outcomes() -> None:
    assert plain_outcome_for_run_report({"terminal_outcome": "success"}).result is (
        PlainResultV1.FINISHED_NOT_CHECKED
    )
    verified = {"terminal_outcome": "success", "execution_outcome": "VERIFIED", "transaction_outcome": "VERIFIED"}
    assert plain_outcome_for_run_report(verified).result is PlainResultV1.DONE


def test_execute_status_helper_returns_progress_until_the_run_ends() -> None:
    running = ExecuteStatusV1(
        execution_id="execution_12345678",
        state=ExecuteLifecycleStateV1.DECISION_REQUIRED,
        updated_at="2026-10-09T12:00:00Z",
    )
    progress = plain_outcome_for_execute_status(running)
    assert isinstance(progress, PlainProgressV1)
    assert progress.label == "Waiting for a person"
    assert progress.safe_to_retry is False

    ended = ExecuteStatusV1(
        execution_id="execution_12345678",
        state=ExecuteLifecycleStateV1.TERMINAL,
        terminal_outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
        evidence_receipt_id="receipt_12345678",
        updated_at="2026-10-09T12:00:00Z",
    )
    result = plain_outcome_for_execute_status(ended)
    assert isinstance(result, PlainOutcomeV1)
    assert result.result is PlainResultV1.CHECK_THE_RECORD


def test_plain_outcome_raises_for_a_run_that_has_not_ended() -> None:
    with pytest.raises(PlainOutcomeInProgress) as caught:
        plain_outcome(execute_state="queued")
    assert caught.value.progress.label == "Waiting to start"


def _receipt(**updates: Any) -> ExecuteEvidenceReceiptV1:
    fields: dict[str, Any] = {
        "receipt_id": "receipt_12345678",
        "execution_id": "execution_12345678",
        "workflow_digest": "sha256:" + "a" * 64,
        "workflow_version": "workflow_20261009",
        "qualification_id": "qualification_12345678",
        "environment_id": "environment_12345678",
        "runner_id": "runner:hosted",
        "nonce": "nonce:execution_12345678",
        "oracle_tier": 2,
        "outcome": ExecuteTerminalOutcomeV1.VERIFIED,
        "contracts": ExecuteEvidenceContractV1(
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=True,
            effect_passed=True,
            minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SESSION,
            observed_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
            model_used=False,
            external_network_used=False,
        ),
        "delivery_uncertain": False,
        "evidence_digest": "sha256:" + "b" * 64,
        "issued_at": "2026-10-09T12:00:00Z",
    }
    fields.update(updates)
    return ExecuteEvidenceReceiptV1.model_validate(fields)


def test_webhook_helper_reports_sealed_proof_only_after_the_signature_checks() -> None:
    key = b"k" * 32
    webhook = sign_execute_webhook_hmac(
        key=key,
        fields={
            "event_type": ExecuteWebhookEventTypeV1.EXECUTION_TERMINAL,
            "event_id": "event_12345678",
            "delivery_attempt": 1,
            "issued_at": "2026-10-09T12:00:00Z",
            "issuer_key_id": "webhook_key_12345678",
            "receipt": _receipt(),
        },
    )
    result = plain_outcome_for_execute_webhook(webhook, key=key)  # type: ignore[arg-type]
    assert result.result is PlainResultV1.DONE
    assert result.proof is PlainProofV1.SEALED
    with pytest.raises(ValueError, match="signature"):
        plain_outcome_for_execute_webhook(webhook, key=b"x" * 32)  # type: ignore[arg-type]


def test_receipt_helper_carries_uncertain_delivery() -> None:
    receipt = _receipt(
        outcome=ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT,
        delivery_uncertain=True,
        contracts=ExecuteEvidenceContractV1(
            authorization_passed=True,
            identity_passed=False,
            postcondition_passed=False,
            effect_passed=False,
            minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SESSION,
            observed_effect_strength=None,
            model_used=False,
            external_network_used=False,
        ),
        oracle_tier=0,
    )
    result = plain_outcome_for_execute_receipt(receipt, proof="none")
    assert result.result is PlainResultV1.CHECK_THE_RECORD
    assert result.technical_code == "fallback=delivery_uncertain"


# ------------------------------------------------------------- the schema


@pytest.mark.parametrize(
    ("model", "filename"),
    [(PlainOutcomeV1, "plain-outcome-v1.json"), (PlainProgressV1, "plain-progress-v1.json")],
)
def test_packaged_json_schemas_match_the_models(model: type, filename: str) -> None:
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    packaged = files("openadapt_types.schemas").joinpath(filename)
    assert json.loads(packaged.read_text()) == schema
