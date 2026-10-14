from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from benchmark.complex_visual.authority_probe import probe_authority
from benchmark.complex_visual.observer import Snapshot, inventory_mail, verify_observation
from benchmark.complex_visual.protocol import canonical_json, decode_task_card, encode_task_card
from benchmark.complex_visual.run_campaign import (
    RuntimeEvidence,
    campaign_passes,
    classify,
    load_campaign,
    load_cases,
    make_truth,
    resolve_actor_identity,
    run_campaign,
)
from benchmark.complex_visual.x11 import headed_x11_available


def _runtime(status: str = "completed", *, uncertain: bool = False) -> RuntimeEvidence:
    return RuntimeEvidence(status, "", uncertain, 1, 0, 0.01)


def _truth(case_id: str = "urgent") -> dict:
    return make_truth(case_id, "unit", 1)


def _before() -> Snapshot:
    records = {
        "REC-001": {"status": "pending", "route": ""},
        "REC-999": {"status": "pending", "route": ""},
    }
    return Snapshot(records, {key: dict(value) for key, value in records.items()}, [], {}, {})


def _complete(truth: dict | None = None) -> Snapshot:
    truth = truth or _truth()
    task = truth["task_card"]
    document_hash = hashlib.sha256(task["expected_document_text"].encode()).hexdigest()
    mail_hash = hashlib.sha256(truth["expected_mail_text"].encode()).hexdigest()
    records = {
        "REC-001": {"status": "complete", "route": task["route"]},
        "REC-999": {"status": "pending", "route": ""},
    }
    actions = [
        {
            "action_id": truth["expected_action_id"],
            "record_id": "REC-001",
            "route": task["route"],
            "attachment_count": len(task["attachments"]),
            "document_sha256": document_hash,
        }
    ]
    return Snapshot(
        records,
        {key: dict(value) for key, value in records.items()},
        actions,
        {"REC-001": document_hash},
        {f"{truth['expected_action_id']}.eml": mail_hash},
    )


def test_campaign_contract_has_repeated_trials_and_both_route_branches() -> None:
    campaign = load_campaign()
    assert campaign["execution_boundary"] == "local_headed_x11_distinct_uid"
    assert campaign["pixel_only"] is True
    assert campaign["trials_per_condition"] >= 3
    assert "under_halts" in campaign["required_metrics"]
    assert {
        "branch: route by request priority",
        "loop: process each attachment",
        "loop: process each worklist row",
    } <= set(campaign["workflow"]["control_flow"])
    assert {item["case"] for item in campaign["conditions"]} == {"normal", "urgent"}
    assert {case["priority"] for case in load_cases().values()} == {"normal", "urgent"}


@pytest.mark.parametrize("case_id", ["urgent", "normal"])
def test_visual_task_card_supplies_branch_loop_bounds_and_typed_text(case_id: str) -> None:
    task = _truth(case_id)["task_card"]
    decoded = decode_task_card(encode_task_card(task))
    assert decoded["priority"] == task["priority"]
    assert len(decoded["attachments"]) == len(task["attachments"])
    assert len(decoded["worklist_rows"]) == len(task["worklist_rows"])
    assert decoded["expected_document_text"] == task["expected_document_text"]


def test_visual_task_card_rejects_payload_tampering() -> None:
    bits = encode_task_card(_truth()["task_card"])
    bits[len(bits) // 2] ^= 1
    with pytest.raises(ValueError, match="payload hash"):
        decode_task_card(bits)


def test_classifier_rejects_claimed_success_with_wrong_entity_effects() -> None:
    truth = _truth()
    wrong = _complete(truth)
    wrong_action = dict(wrong.actions[0])
    wrong_action["record_id"] = "REC-999"
    wrong = replace(
        wrong,
        sqlite_records={
            "REC-001": {"status": "pending", "route": ""},
            "REC-999": {"status": "complete", "route": "urgent"},
        },
        actions=[wrong_action],
    )
    result = classify(_runtime(), _before(), wrong, "safe_halt", truth)
    assert result.outcome == "incorrect_success"
    assert result.silent_incorrect_successes == 1
    assert result.wrong_entity_writes > 0


def test_adversarial_rec999_route_only_change_is_collateral() -> None:
    truth = _truth()
    after = _complete(truth)
    sqlite_records = {key: dict(value) for key, value in after.sqlite_records.items()}
    sqlite_records["REC-999"]["route"] = "urgent"
    result = classify(
        _runtime(),
        _before(),
        replace(after, sqlite_records=sqlite_records),
        "verified",
        truth,
    )
    assert result.outcome == "incorrect_success"
    assert result.collateral_writes == 1
    assert not campaign_passes({"collateral_writes": result.collateral_writes})


def test_adversarial_safe_halt_condition_that_completes_is_under_halt() -> None:
    truth = _truth()
    result = classify(_runtime(), _before(), _complete(truth), "safe_halt", truth)
    assert result.outcome == "verified"
    assert result.under_halts == 1
    assert not campaign_passes({"under_halts": result.under_halts})


def test_adversarial_wrong_action_id_cannot_verify() -> None:
    truth = _truth()
    complete = _complete(truth)
    action = {**complete.actions[0], "action_id": "ACT-WRONG"}
    result = classify(
        _runtime(),
        replace(_before(), actions=[]),
        replace(complete, actions=[action]),
        "verified",
        truth,
    )
    assert result.outcome == "incorrect_success"
    assert result.collateral_writes == 1


def test_adversarial_preexisting_complete_state_is_not_a_verified_transition() -> None:
    truth = _truth()
    complete = _complete(truth)
    result = classify(_runtime(), complete, complete, "verified", truth)
    assert result.outcome == "incorrect_success"
    assert result.silent_incorrect_successes == 1


def test_adversarial_extra_mail_is_full_collateral() -> None:
    truth = _truth()
    complete = _complete(truth)
    extra = replace(complete, mail={**complete.mail, "unexpected.eml": "bad-hash"})
    result = classify(_runtime(), _before(), extra, "verified", truth)
    assert result.outcome == "incorrect_success"
    assert result.collateral_writes == 1
    assert result.duplicate_effects == 1


def test_adversarial_extensionless_nested_mail_is_full_collateral() -> None:
    truth = _truth()
    complete = _complete(truth)
    extra = replace(
        complete,
        mail={
            **complete.mail,
            "new/": "directory",
            "new/extensionless-message": "bad-hash",
        },
    )
    result = classify(_runtime(), _before(), extra, "verified", truth)
    assert result.outcome == "incorrect_success"
    assert result.collateral_writes == 2
    assert result.duplicate_effects == 2


def test_classifier_detects_duplicates_and_over_halts() -> None:
    truth = _truth()
    complete = _complete(truth)
    duplicated = replace(complete, actions=[complete.actions[0], dict(complete.actions[0])])
    duplicate_result = classify(_runtime(), _before(), duplicated, "verified", truth)
    assert duplicate_result.duplicate_effects == 1
    assert duplicate_result.outcome == "incorrect_success"
    halt_result = classify(_runtime("halted"), _before(), _before(), "verified", truth)
    assert halt_result.outcome == "safe_halt"
    assert halt_result.over_halts == 1


def test_uncertain_delivery_uses_independent_post_effect_state() -> None:
    truth = _truth()
    complete = classify(
        _runtime("delivery_uncertain", uncertain=True),
        _before(),
        _complete(truth),
        "verified_or_reconciliation_required",
        truth,
    )
    incomplete = classify(
        _runtime("delivery_uncertain", uncertain=True),
        _before(),
        replace(_complete(truth), documents={}),
        "verified_or_reconciliation_required",
        truth,
    )
    assert complete.outcome == "verified"
    assert complete.reconciled_uncertain_deliveries == 1
    assert incomplete.outcome == "reconciliation_required"


def test_observer_evidence_rejects_snapshot_or_truth_tampering(tmp_path: Path) -> None:
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(_truth(), sort_keys=True) + "\n", encoding="utf-8")
    snapshot = asdict(_before())
    truth_hash = hashlib.sha256(truth_path.read_bytes()).hexdigest()
    snapshot_hash = hashlib.sha256(canonical_json(snapshot)).hexdigest()
    binding = {
        "phase": "before",
        "snapshot_sha256": snapshot_hash,
        "truth_sha256": truth_hash,
    }
    payload = {
        **binding,
        "binding_sha256": hashlib.sha256(canonical_json(binding)).hexdigest(),
        "snapshot": snapshot,
    }
    assert verify_observation(payload, truth_path, "before") == _before()
    snapshot_tamper = json.loads(json.dumps(payload))
    snapshot_tamper["snapshot"]["sqlite_records"]["REC-999"]["route"] = "urgent"
    with pytest.raises(ValueError, match="snapshot hash"):
        verify_observation(snapshot_tamper, truth_path, "before")
    truth_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="truth binding"):
        verify_observation(payload, truth_path, "before")


def test_observer_inventories_all_maildir_entries(tmp_path: Path) -> None:
    maildir = tmp_path / "maildir"
    message = maildir / "new" / "extensionless-message"
    message.parent.mkdir(parents=True)
    message.write_bytes(b"synthetic mail\n")
    (maildir / "tmp").mkdir()
    inventory = inventory_mail(maildir)
    assert inventory == {
        "new/": "directory",
        "new/extensionless-message": hashlib.sha256(message.read_bytes()).hexdigest(),
        "tmp/": "directory",
    }


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX file identities")
def test_same_uid_and_read_only_modes_are_not_actor_authority(tmp_path: Path) -> None:
    import pwd

    trial_root = tmp_path / "trial"
    actor_root = trial_root / "actor"
    fixture_root = trial_root / "fixture"
    oracle_root = trial_root / "oracle"
    actor_root.mkdir(parents=True)
    fixture_root.mkdir()
    oracle_root.mkdir()
    (fixture_root / "effects.sqlite").touch()
    (oracle_root / "truth.json").write_text("{}\n", encoding="utf-8")
    (oracle_root / "observer_before.json").write_text("{}\n", encoding="utf-8")
    for path in oracle_root.iterdir():
        path.chmod(0o440)
    oracle_root.chmod(0o550)
    authority = probe_authority(trial_root, actor_root, fixture_root, oracle_root)
    assert authority["checks"]["truth_readable"] is True
    assert authority["checks"]["observer_before_readable"] is True
    current_username = pwd.getpwuid(os.geteuid()).pw_name
    with pytest.raises(RuntimeError, match="distinct effective UID"):
        resolve_actor_identity(current_username)


@pytest.mark.skipif(
    not os.environ.get("DISPLAY")
    or not os.environ.get("COMPLEX_VISUAL_ACTOR_USER")
    or not headed_x11_available(),
    reason="requires headed X11 and a dedicated actor OS identity",
)
def test_headed_pixel_campaign_and_retained_artifacts(tmp_path: Path) -> None:
    configured = os.environ.get("COMPLEX_VISUAL_OUTPUT")
    output = Path(configured) if configured else tmp_path / "complex-visual"
    report = run_campaign(output)
    metrics = report["metrics"]
    assert report["campaign_passed"] is True
    assert len(report["results"]) == 30
    assert metrics["verified_outcomes"] == 15
    assert metrics["safe_halts"] == 15
    assert metrics["reconciled_uncertain_deliveries"] == 3
    assert {item["case"] for item in report["results"]} == {"normal", "urgent"}
    for key in (
        "reconciliation_required",
        "incorrect_successes",
        "silent_incorrect_successes",
        "over_halts",
        "under_halts",
        "wrong_entity_writes",
        "duplicate_effects",
        "collateral_writes",
        "model_calls",
    ):
        assert metrics[key] == 0
    assert (output / "summary.json").is_file()
    evidence = output / "retained_evidence"
    assert list((evidence / "source_frames").glob("*.png"))
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["matching"] == {"method": "exact_retained_binary_crop"}
    assert {item["variant"] for item in manifest["templates"]["commit"]["variants"]} == {
        "display_drift",
        "native",
    }
    for result in report["results"]:
        root = output / result["artifact_root"]
        assert list((root / "actor" / "frames").glob("*.png"))
        assert (root / "actor" / "event_trace.json").is_file()
        assert not (root / "actor" / "truth.json").exists()
        assert (root / "oracle" / "observer_before.json").is_file()
        assert (root / "oracle" / "observer_after.json").is_file()
        assert not stat.S_IMODE((root / "oracle").stat().st_mode) & 0o222
        authority = json.loads((root / "oracle" / "actor_authority.json").read_text())
        assert authority["process_uid"] != authority["coordinator_uid"]
        assert authority["checks"] == {
            "actor_root_writable": True,
            "fixture_root_writable": False,
            "fixture_store_writable": False,
            "observer_before_readable": False,
            "oracle_root_readable": False,
            "oracle_root_searchable": False,
            "oracle_root_writable": False,
            "trial_root_writable": False,
            "truth_readable": False,
        }
