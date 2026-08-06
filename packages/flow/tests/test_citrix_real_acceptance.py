from __future__ import annotations

import base64
import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parents[1]
PATH = REPO / "benchmark/citrix_ica_hdx/run_real_acceptance.py"
spec = importlib.util.spec_from_file_location("citrix_real_acceptance", PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
CAMPAIGN_NONCE = "12" * 16


@dataclass
class Campaign:
    config: dict
    trust_path: Path
    trust_roots: dict
    keys: dict[str, Ed25519PrivateKey]


def _public_key(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def _signed(key: Ed25519PrivateKey, payload: dict) -> dict:
    signature = key.sign(mod._canonical_json(payload))
    return {"payload": payload, "signature": base64.b64encode(signature).decode()}


def _write(path: Path, value: str | dict, *, executable: bool = False) -> dict:
    path.write_text(
        json.dumps(value, sort_keys=True) if isinstance(value, dict) else value
    )
    if executable:
        path.chmod(0o700)
    return {"path": str(path), "sha256": mod._sha256(path)}


def _fingerprints(component_sha: dict[str, str]) -> dict:
    return {
        "citrix_workspace": {
            "product": "Citrix Workspace",
            "version": "1.2.3",
            "binary_sha256": SHA_A,
        },
        "ica_hdx": {
            "protocol": "ICA/HDX",
            "vda_version": "2603",
            "policy_sha256": SHA_A,
            "transport_sha256": SHA_B,
        },
        "application": {
            "name": "Synthetic App",
            "version": "7.4",
            "binary_sha256": SHA_B,
        },
        "session": {
            "session_id_sha256": SHA_A,
            "published_resource_sha256": SHA_B,
            "vda_sha256": SHA_C,
        },
        "display": {
            "width_px": 1280,
            "height_px": 800,
            "dpi_x": 96,
            "dpi_y": 96,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "monitor_topology_sha256": SHA_C,
            "window_mode": "published_application",
        },
        "runner": {
            "name": "OpenAdapt",
            "version": "1.0",
            "binary_sha256": component_sha["runner"],
            "principal_sha256": SHA_A,
            "host_sha256": SHA_A,
        },
        "bundle": {
            "workflow_version": "1",
            "bundle_sha256": SHA_B,
            "source_commit": "d" * 40,
        },
        "verifier": {
            "name": "Customer Oracle",
            "version": "1",
            "binary_sha256": component_sha["oracle"],
            "principal_sha256": SHA_C,
        },
        "collector": {
            "name": "Independent ICA Collector",
            "version": "1",
            "binary_sha256": component_sha["collector"],
            "principal_sha256": SHA_D,
        },
        "environment": {
            "environment_sha256": SHA_B,
            "os_build": "Windows 11",
            "network_zone_sha256": SHA_A,
        },
    }


def _campaign(tmp_path: Path) -> Campaign:
    executables = {}
    component_sha = {}
    for name in ("runner", "oracle", "collector"):
        path = tmp_path / name
        _write(path, f"#!/bin/sh\n# {name}\n", executable=True)
        executables[name] = path
        component_sha[name] = mod._sha256(path)
    keys = {
        name: Ed25519PrivateKey.generate()
        for name in ("customer", "upgrade", "collector", "oracle")
    }
    components = {
        "runner": {
            "executable_sha256": component_sha["runner"],
            "principal_sha256": SHA_A,
        },
        "oracle": {
            "executable_sha256": component_sha["oracle"],
            "principal_sha256": SHA_C,
        },
        "collector": {
            "executable_sha256": component_sha["collector"],
            "principal_sha256": SHA_D,
        },
    }
    trust = {
        "schema_version": mod.TRUST_ROOT_SCHEMA,
        "keys": {
            "customer_authority": _public_key(keys["customer"]),
            "upgrade_authority": _public_key(keys["upgrade"]),
            "collector_authority": _public_key(keys["collector"]),
            "oracle_authority": _public_key(keys["oracle"]),
        },
        "components": components,
    }
    trust_path = tmp_path / "external-trust-roots.json"
    trust_path.write_text(json.dumps(trust))
    trust_roots = mod.load_trust_roots(trust_path)
    fingerprints = _fingerprints(component_sha)

    def upgrade(name: str) -> dict:
        return _write(
            tmp_path / f"{name}-upgrade.json",
            _signed(
                keys["upgrade"],
                {
                    "schema_version": "openadapt.component-upgrade-attestation.v1",
                    "component": name,
                    **components[name],
                },
            ),
        )

    runner_approval = _write(
        tmp_path / "runner-approval.json",
        _signed(
            keys["customer"],
            {
                "schema_version": "openadapt.customer-runner-approval.v1",
                "authority": "customer_approved",
                "principal_sha256": SHA_A,
                "executable_sha256": component_sha["runner"],
                "session_id_sha256": fingerprints["session"]["session_id_sha256"],
                "campaign_nonce": CAMPAIGN_NONCE,
                "allowed_operation": "citrix_acceptance_trial",
                "infrastructure_lifecycle_authority": False,
            },
        ),
    )
    oracle_approval = _write(
        tmp_path / "oracle-approval.json",
        _signed(
            keys["customer"],
            {
                "schema_version": "openadapt.customer-oracle-approval.v1",
                "authority": "customer_approved",
                "principal_sha256": SHA_C,
                "executable_sha256": component_sha["oracle"],
                "environment_sha256": fingerprints["environment"]["environment_sha256"],
                "allowed_operation": "read_only_effect_observation",
                "separately_authenticated": True,
                "write_authority": False,
            },
        ),
    )
    trials = []
    for condition, expected in mod.EXPECTED_OUTCOMES.items():
        for number in range(3):
            trials.append(
                {
                    "id": f"{condition}-{number}",
                    "condition": condition,
                    "expected": expected,
                    "entity_sha256": SHA_A,
                    "effect_contract_sha256": SHA_B,
                }
            )
    config = {
        "schema_version": mod.SCHEMA,
        "campaign_nonce": CAMPAIGN_NONCE,
        "fingerprints": fingerprints,
        "runner_authority": {
            "mode": "customer_approved_session_runner",
            "pre_existing_session": True,
            "infrastructure_lifecycle_authority": False,
            "command": [str(executables["runner"])],
            "executable_sha256": component_sha["runner"],
            "principal_sha256": SHA_A,
            "approval_artifact": runner_approval,
            "upgrade_artifact": upgrade("runner"),
        },
        "independent_oracle": {
            "command": [str(executables["oracle"])],
            "executable_sha256": component_sha["oracle"],
            "authority": "authenticated_read_only",
            "principal_sha256": SHA_C,
            "approval_artifact": oracle_approval,
            "upgrade_artifact": upgrade("oracle"),
        },
        "collector_authority": {
            "command": [str(executables["collector"])],
            "executable_sha256": component_sha["collector"],
            "principal_sha256": SHA_D,
            "upgrade_artifact": upgrade("collector"),
        },
        "trials": trials,
    }
    return Campaign(config, trust_path, trust_roots, keys)


def _load(campaign: Campaign, tmp_path: Path) -> tuple[dict, Path]:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(campaign.config))
    return mod.load_config(path, campaign.trust_roots), path


def _evidence(tmp_path: Path, name: str) -> dict:
    return _write(tmp_path / name, name)


def _oracle(
    campaign: Campaign,
    tmp_path: Path,
    trial: dict,
    config_sha256: str,
    phase: str,
    *,
    execution_challenge: str,
    observation_challenge: str,
    status: str = "REFUTED",
    state_digest: str = SHA_A,
) -> dict:
    payload = {
        "schema_version": "openadapt.citrix-oracle-observation.v1",
        "campaign_nonce": campaign.config["campaign_nonce"],
        "config_sha256": config_sha256,
        "execution_challenge": execution_challenge,
        "observation_challenge": observation_challenge,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "trial_id": trial["id"],
        "entity_sha256": trial["entity_sha256"],
        "effect_contract_sha256": trial["effect_contract_sha256"],
        "principal_sha256": SHA_C,
        "authority": "authenticated_read_only",
        "effect_status": status,
        "state_digest": state_digest,
        "evidence": _evidence(tmp_path, f"oracle-{phase}-{trial['id']}.json"),
    }
    return _signed(campaign.keys["oracle"], payload)


def _collector(
    campaign: Campaign,
    tmp_path: Path,
    trial: dict,
    config_sha256: str,
    *,
    execution_challenge: str,
    observation_challenge: str,
    observed_at: datetime | None = None,
) -> dict:
    payload = {
        "schema_version": "openadapt.citrix-independent-collector.v1",
        "campaign_nonce": campaign.config["campaign_nonce"],
        "config_sha256": config_sha256,
        "execution_challenge": execution_challenge,
        "observation_challenge": observation_challenge,
        "trial_id": trial["id"],
        "protocol": "ICA/HDX",
        "standin": False,
        "session_id_sha256": SHA_A,
        "transport_sha256": SHA_B,
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "observed_components": campaign.trust_roots["components"],
        "diagnostic_evidence": _evidence(tmp_path, f"collector-{trial['id']}.json"),
    }
    return _signed(campaign.keys["collector"], payload)


def _receipt(trial: dict, collector_sha256: str) -> dict:
    delivery = {
        "healthy": "dispatched",
        "partial_effect": "dispatched",
        "commit_timeout": "uncertain",
    }.get(trial["condition"], "not_dispatched")
    return {
        "schema_version": "openadapt.citrix-trial-receipt.v1",
        "trial_id": trial["id"],
        "condition": trial["condition"],
        "delivery_state": delivery,
        "retry_count": 0,
        "reconciliation_required": trial["condition"] == "commit_timeout",
        "collector_evidence_sha256": collector_sha256,
    }


def _result(value: dict, *, returncode: int = 0) -> dict:
    return {"returncode": returncode, "stdout": json.dumps(value), "stderr": ""}


def test_complete_campaign_contract_passes_preflight(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    config, _ = _load(campaign, tmp_path)
    assert len(config["trials"]) == 24


def test_requires_three_trials_and_fixed_outcomes(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    campaign.config["trials"].pop()
    with pytest.raises(ValueError, match="three trials"):
        _load(campaign, tmp_path)
    campaign = _campaign(tmp_path)
    campaign.config["trials"][-1]["expected"] = "VERIFIED"
    with pytest.raises(ValueError, match="HALTED_UNCERTAIN"):
        _load(campaign, tmp_path)


def test_external_trust_roots_reject_self_asserted_component(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    campaign.config["runner_authority"]["principal_sha256"] = SHA_B
    campaign.config["fingerprints"]["runner"]["principal_sha256"] = SHA_B
    with pytest.raises(ValueError, match="not trusted"):
        _load(campaign, tmp_path)


def test_authority_keys_are_distinct_and_trust_roots_are_not_world_writable(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    trust = json.loads(campaign.trust_path.read_text())
    trust["keys"]["oracle_authority"] = trust["keys"]["collector_authority"]
    campaign.trust_path.write_text(json.dumps(trust))
    with pytest.raises(ValueError, match="must be distinct"):
        mod.load_trust_roots(campaign.trust_path)

    campaign = _campaign(tmp_path)
    campaign.trust_path.chmod(0o666)
    with pytest.raises(ValueError, match="world-writable"):
        mod.load_trust_roots(campaign.trust_path)


def test_world_writable_executable_is_rejected(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    runner = Path(campaign.config["runner_authority"]["command"][0])
    runner.chmod(0o702)
    with pytest.raises(ValueError, match="world-writable"):
        _load(campaign, tmp_path)


def test_customer_and_upgrade_signatures_reject_tampering(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    approval = Path(campaign.config["runner_authority"]["approval_artifact"]["path"])
    envelope = json.loads(approval.read_text())
    envelope["payload"]["infrastructure_lifecycle_authority"] = True
    approval.write_text(json.dumps(envelope))
    campaign.config["runner_authority"]["approval_artifact"]["sha256"] = mod._sha256(
        approval
    )
    with pytest.raises(ValueError, match="signature"):
        _load(campaign, tmp_path)

    campaign = _campaign(tmp_path)
    upgrade = Path(campaign.config["independent_oracle"]["upgrade_artifact"]["path"])
    envelope = json.loads(upgrade.read_text())
    envelope["payload"]["component"] = "runner"
    upgrade.write_text(json.dumps(envelope))
    campaign.config["independent_oracle"]["upgrade_artifact"]["sha256"] = mod._sha256(
        upgrade
    )
    with pytest.raises(ValueError, match="signature"):
        _load(campaign, tmp_path)


def test_observed_executable_is_rechecked_after_preflight(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    config, _ = _load(campaign, tmp_path)
    runner = Path(config["runner_authority"]["command"][0])
    runner.write_text("replaced")
    with pytest.raises(ValueError, match="changed after preflight"):
        mod._run_pinned(
            config["runner_authority"]["command"],
            config["runner_authority"]["executable_sha256"],
            ["execute-trial"],
        )


def test_signed_oracle_is_bound_and_tamper_evident(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    config, path = _load(campaign, tmp_path)
    trial = config["trials"][0]
    digest = mod._sha256(path)
    execution_challenge = "e" * 64
    observation_challenge = "f" * 64
    consumed: set[str] = set()
    envelope = _oracle(
        campaign,
        tmp_path,
        trial,
        digest,
        "before",
        execution_challenge=execution_challenge,
        observation_challenge=observation_challenge,
    )
    result = _result(envelope)
    assert (
        mod._validate_oracle_evidence(
            result,
            trial=trial,
            phase="before",
            oracle_principal_sha256=SHA_C,
            oracle_public_key=campaign.trust_roots["decoded_keys"]["oracle_authority"],
            campaign_nonce=config["campaign_nonce"],
            config_sha256=digest,
            execution_challenge=execution_challenge,
            observation_challenge=observation_challenge,
            consumed_challenges=consumed,
        )["payload"]["effect_status"]
        == "REFUTED"
    )
    with pytest.raises(ValueError, match="already consumed"):
        mod._validate_oracle_evidence(
            result,
            trial=trial,
            phase="before",
            oracle_principal_sha256=SHA_C,
            oracle_public_key=campaign.trust_roots["decoded_keys"]["oracle_authority"],
            campaign_nonce=config["campaign_nonce"],
            config_sha256=digest,
            execution_challenge=execution_challenge,
            observation_challenge=observation_challenge,
            consumed_challenges=consumed,
        )
    envelope["payload"]["entity_sha256"] = SHA_B
    with pytest.raises(ValueError, match="signature"):
        mod._validate_oracle_evidence(
            _result(envelope),
            trial=trial,
            phase="before",
            oracle_principal_sha256=SHA_C,
            oracle_public_key=campaign.trust_roots["decoded_keys"]["oracle_authority"],
            campaign_nonce=config["campaign_nonce"],
            config_sha256=digest,
            execution_challenge=execution_challenge,
            observation_challenge="1" * 64,
            consumed_challenges=set(),
        )


def test_collector_is_signed_fresh_and_campaign_bound(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    config, path = _load(campaign, tmp_path)
    trial = config["trials"][0]
    digest = mod._sha256(path)
    execution_challenge = "e" * 64
    observation_challenge = "f" * 64
    envelope = _collector(
        campaign,
        tmp_path,
        trial,
        digest,
        execution_challenge=execution_challenge,
        observation_challenge=observation_challenge,
    )
    assert (
        mod._validate_collector_evidence(
            _result(envelope),
            trial=trial,
            config=config,
            config_sha256=digest,
            trust_roots=campaign.trust_roots,
            execution_challenge=execution_challenge,
            observation_challenge=observation_challenge,
            consumed_challenges=set(),
        )["payload"]["protocol"]
        == "ICA/HDX"
    )
    stale = _collector(
        campaign,
        tmp_path,
        trial,
        digest,
        execution_challenge=execution_challenge,
        observation_challenge="1" * 64,
        observed_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="stale"):
        mod._validate_collector_evidence(
            _result(stale),
            trial=trial,
            config=config,
            config_sha256=digest,
            trust_roots=campaign.trust_roots,
            execution_challenge=execution_challenge,
            observation_challenge="1" * 64,
            consumed_challenges=set(),
        )
    wrong_campaign = _collector(
        campaign,
        tmp_path,
        trial,
        digest,
        execution_challenge=execution_challenge,
        observation_challenge="2" * 64,
    )
    wrong_campaign["payload"]["campaign_nonce"] = "34" * 16
    wrong_campaign = _signed(campaign.keys["collector"], wrong_campaign["payload"])
    with pytest.raises(ValueError, match="campaign_nonce"):
        mod._validate_collector_evidence(
            _result(wrong_campaign),
            trial=trial,
            config=config,
            config_sha256=digest,
            trust_roots=campaign.trust_roots,
            execution_challenge=execution_challenge,
            observation_challenge="2" * 64,
            consumed_challenges=set(),
        )


def test_runner_receipt_binds_collector_and_never_retries(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    config, _ = _load(campaign, tmp_path)
    trial = config["trials"][0]
    receipt = _receipt(trial, SHA_C)
    assert (
        mod._validate_runner_receipt(
            _result(receipt), trial=trial, collector_evidence_sha256=SHA_C
        )["retry_count"]
        == 0
    )
    receipt["retry_count"] = 1
    with pytest.raises(ValueError, match="must not retry"):
        mod._validate_runner_receipt(
            _result(receipt), trial=trial, collector_evidence_sha256=SHA_C
        )
    receipt = _receipt(trial, SHA_C)
    receipt["outcome"] = "VERIFIED"
    with pytest.raises(ValueError, match="unknown fields"):
        mod._validate_runner_receipt(
            _result(receipt), trial=trial, collector_evidence_sha256=SHA_C
        )


def _scripted_run(
    campaign: Campaign,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    before_status: str = "REFUTED",
    runner_value: dict | None = None,
    after_status: str = "CONFIRMED",
    corrupt_after: bool = False,
    corrupt_runner: str | None = None,
    first_condition: str = "healthy",
) -> tuple[dict, Path, list[str]]:
    config, config_path = _load(campaign, tmp_path)
    selected = next(
        trial for trial in config["trials"] if trial["condition"] == first_condition
    )
    config["trials"].remove(selected)
    config["trials"].insert(0, selected)
    digest = mod._sha256(config_path)
    trial = selected
    calls = []
    collector_envelope: dict | None = None

    def fake_run(command, expected_sha256, args, timeout_s=300):
        nonlocal collector_envelope
        del command, expected_sha256, timeout_s
        calls.append(args[0])
        execution_challenge = args[args.index("--execution-challenge") + 1]
        observation_challenge = (
            args[args.index("--observation-challenge") + 1]
            if "--observation-challenge" in args
            else ""
        )
        if args[0] == "collect":
            collector_envelope = _collector(
                campaign,
                tmp_path,
                trial,
                digest,
                execution_challenge=execution_challenge,
                observation_challenge=observation_challenge,
            )
            return _result(collector_envelope)
        if args[0] == "execute-trial":
            if corrupt_runner == "command":
                return {"returncode": 1, "stdout": "", "stderr": "failed"}
            if corrupt_runner == "receipt":
                return _result({"unexpected": True})
            assert collector_envelope is not None
            receipt = runner_value or _receipt(
                trial, mod._object_sha256(collector_envelope)
            )
            return _result(receipt)
        phase = args[args.index("--phase") + 1]
        if phase == "before":
            return _result(
                _oracle(
                    campaign,
                    tmp_path,
                    trial,
                    digest,
                    "before",
                    execution_challenge=execution_challenge,
                    observation_challenge=observation_challenge,
                    status=before_status,
                )
            )
        if corrupt_after:
            return {"returncode": 0, "stdout": "not-json", "stderr": ""}
        return _result(
            _oracle(
                campaign,
                tmp_path,
                trial,
                digest,
                "after",
                execution_challenge=execution_challenge,
                observation_challenge=observation_challenge,
                status=after_status,
                state_digest=SHA_B,
            )
        )

    monkeypatch.setattr(mod, "_run_pinned", fake_run)
    output = tmp_path / "terminal-report.json"
    nonce_registry = tmp_path / "nonce-registry"
    nonce_registry.mkdir()
    nonce_registry.chmod(0o700)
    report = mod.run_campaign(
        config,
        trust_roots=campaign.trust_roots,
        config_sha256=digest,
        output=output,
        nonce_registry=nonce_registry,
    )
    return report, output, calls


def test_unsafe_baseline_stops_before_dispatch_and_retains_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    report, output, calls = _scripted_run(
        campaign, tmp_path, monkeypatch, before_status="CONFIRMED"
    )
    assert calls == ["observe"]
    assert report["trials"][0]["delivery_state"] == "not_dispatched"
    assert report["terminal_reason"] == "pre_dispatch_safety_refusal"
    assert output.is_file()


def test_commit_timeout_also_requires_refuted_baseline_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    report, _, calls = _scripted_run(
        campaign,
        tmp_path,
        monkeypatch,
        before_status="INDETERMINATE",
        first_condition="commit_timeout",
    )
    assert calls == ["observe"]
    assert report["trials"][0]["outcome"] == "HALTED"
    assert report["trials"][0]["delivery_state"] == "not_dispatched"


def test_durable_journal_precedes_dispatch_and_is_nonce_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    report, output, calls = _scripted_run(campaign, tmp_path, monkeypatch)
    records = [
        json.loads(line)
        for line in Path(report["journal"]["path"]).read_text().splitlines()
    ]
    events = [record["event"] for record in records]
    assert events.index("PRE_DISPATCH_DURABLE") < events.index("DISPATCH_ATTEMPT")
    assert mod._sha256(Path(report["journal"]["path"])) == report["journal"]["sha256"]
    config, path = _load(campaign, tmp_path)
    call_count = len(calls)
    recovered = mod.run_campaign(
        config,
        trust_roots=campaign.trust_roots,
        config_sha256=mod._sha256(path),
        output=output,
        nonce_registry=tmp_path / "nonce-registry",
    )
    assert recovered["terminal"] is True
    assert len(calls) == call_count
    with pytest.raises(ValueError, match="already bound to another"):
        mod.run_campaign(
            config,
            trust_roots=campaign.trust_roots,
            config_sha256=mod._sha256(path),
            output=tmp_path / "another-report.json",
            nonce_registry=tmp_path / "nonce-registry",
        )


def test_dispatch_attempt_crash_recovers_as_uncertain_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    config, path = _load(campaign, tmp_path)
    digest = mod._sha256(path)
    registry = tmp_path / "nonce-registry"
    registry.mkdir(mode=0o700)
    output = tmp_path / "terminal-report.json"
    binding, reserved = mod._reserve_campaign_nonce(
        registry,
        campaign_nonce=config["campaign_nonce"],
        config_sha256=digest,
        trust_roots_sha256=campaign.trust_roots["path_sha256"],
        output=output,
        execution_challenge="e" * 64,
    )
    assert reserved is True
    journal = mod.DurableJournal(
        Path(binding["journal"]),
        campaign_nonce=config["campaign_nonce"],
        config_sha256=digest,
    )
    trial = config["trials"][0]
    journal.append("PRE_DISPATCH_DURABLE", {"trial_id": trial["id"]})
    journal.append("DISPATCH_ATTEMPT", {"trial_id": trial["id"], "retry_count": 0})

    def refuse_dispatch(*args, **kwargs):
        raise AssertionError("recovery must not dispatch")

    monkeypatch.setattr(mod, "_run_pinned", refuse_dispatch)
    report = mod.run_campaign(
        config,
        trust_roots=campaign.trust_roots,
        config_sha256=digest,
        output=output,
        nonce_registry=registry,
    )
    row = report["trials"][0]
    assert row["outcome"] == "HALTED_UNCERTAIN"
    assert row["retry_count"] == 0
    assert row["reconciliation_required"] is True
    assert report["terminal_reason"] == "recovered_dispatch_attempt"
    assert (
        mod.DurableJournal.read_verified(Path(binding["journal"]))[-1]["event"]
        == "CAMPAIGN_TERMINAL"
    )


def test_completed_nonce_returns_accepted_terminal_result_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    config, path = _load(campaign, tmp_path)
    digest = mod._sha256(path)
    registry = tmp_path / "nonce-registry"
    registry.mkdir(mode=0o700)
    output = tmp_path / "terminal-report.json"
    binding, _ = mod._reserve_campaign_nonce(
        registry,
        campaign_nonce=config["campaign_nonce"],
        config_sha256=digest,
        trust_roots_sha256=campaign.trust_roots["path_sha256"],
        output=output,
        execution_challenge="e" * 64,
    )
    journal = mod.DurableJournal(
        Path(binding["journal"]),
        campaign_nonce=config["campaign_nonce"],
        config_sha256=digest,
    )
    journal.append("CAMPAIGN_COMPLETE", {"trial_count": 24, "accepted": True})
    original = {
        "schema_version": mod.REPORT_SCHEMA,
        "campaign_nonce": config["campaign_nonce"],
        "config_sha256": digest,
        "trust_roots_sha256": campaign.trust_roots["path_sha256"],
        "accepted": True,
        "terminal": True,
        "terminal_reason": "campaign_complete",
        "trials": [{"id": "healthy-0", "outcome": "VERIFIED", "passed": True}],
    }
    mod._write_terminal_report(
        output,
        Path(binding["terminal_fallback"]),
        original,
        journal,
    )

    def refuse_dispatch(*args, **kwargs):
        raise AssertionError("completed recovery must not dispatch")

    monkeypatch.setattr(mod, "_run_pinned", refuse_dispatch)
    recovered = mod.run_campaign(
        config,
        trust_roots=campaign.trust_roots,
        config_sha256=digest,
        output=output,
        nonce_registry=registry,
    )
    assert recovered["accepted"] is True
    assert recovered["terminal_reason"] == "campaign_complete"
    assert recovered["trials"] == original["trials"]


def test_recovery_rejects_a_tampered_journal_hash_chain(tmp_path: Path) -> None:
    journal_path = tmp_path / "campaign.journal.jsonl"
    journal = mod.DurableJournal(
        journal_path,
        campaign_nonce=CAMPAIGN_NONCE,
        config_sha256=SHA_A,
    )
    journal.append("DISPATCH_ATTEMPT", {"trial_id": "healthy-0", "retry_count": 0})
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    records[-1]["payload"]["retry_count"] = 1
    journal_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(ValueError, match="digest is invalid"):
        mod.DurableJournal.reopen(journal_path)


def test_report_destinations_are_reserved_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    config, path = _load(campaign, tmp_path)
    registry = tmp_path / "nonce-registry"
    registry.mkdir(mode=0o700)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    def refuse_dispatch(*args, **kwargs):
        raise AssertionError("output preflight must precede dispatch")

    monkeypatch.setattr(mod, "_run_pinned", refuse_dispatch)
    with pytest.raises(OSError):
        mod.run_campaign(
            config,
            trust_roots=campaign.trust_roots,
            config_sha256=mod._sha256(path),
            output=blocked_parent / "report.json",
            nonce_registry=registry,
        )
    fallback = registry / f"{CAMPAIGN_NONCE}.terminal.json"
    assert fallback.is_file()
    assert json.loads(fallback.read_text())["executed"] is True


@pytest.mark.parametrize("failure", ["runner_command", "receipt", "oracle_after"])
def test_every_post_dispatch_error_is_uncertain_and_stops_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    campaign = _campaign(tmp_path)
    report, output, calls = _scripted_run(
        campaign,
        tmp_path,
        monkeypatch,
        corrupt_after=failure == "oracle_after",
        corrupt_runner={"runner_command": "command", "receipt": "receipt"}.get(failure),
    )
    row = report["trials"][0]
    assert row["outcome"] == "HALTED_UNCERTAIN"
    assert row["retry_count"] == 0
    assert row["reconciliation_required"] is True
    assert len(report["trials"]) == 1
    assert calls == ["observe", "collect", "execute-trial", "observe"]
    assert Path(row["failure_evidence"]["path"]).is_file()
    assert json.loads(output.read_text())["terminal"] is True


def test_first_effect_failure_stops_campaign_and_retains_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    report, output, calls = _scripted_run(
        campaign, tmp_path, monkeypatch, after_status="REFUTED"
    )
    assert report["terminal_reason"] == "first_failed_safety_identity_or_effect_trial"
    assert len(report["trials"]) == 1
    assert report["trials"][0]["outcome"] == "HALTED"
    assert report["trials"][0]["passed"] is False
    assert len(calls) == 4
    assert json.loads(output.read_text())["accepted"] is False


def test_commit_timeout_requires_conclusive_reconciliation() -> None:
    trial = {
        "condition": "commit_timeout",
        "expected": "HALTED_UNCERTAIN",
    }
    receipt = {"delivery_state": "uncertain"}
    before = {"payload": {"effect_status": "REFUTED", "state_digest": SHA_A}}
    after = {"payload": {"effect_status": "INDETERMINATE", "state_digest": SHA_B}}
    assert not mod._trial_passed(trial, receipt, before, after)
