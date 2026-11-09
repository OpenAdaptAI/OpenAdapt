from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.interop.business_decision_cloud import BusinessDecisionCloudRefused
from openadapt_flow.interop.decision_relay_transport import RelayRefused
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.policy import Policy, policy_contract_sha256
from openadapt_flow.runner.business_decision_service import (
    KEY_SCHEMA,
    BusinessDecisionServiceLoop,
    _load_exact_run_workflow,
    _windows_descriptor_has_private_acl,
    load_business_decision_key_material,
)
from openadapt_flow.runner.config import RunnerConfig, RunnerConfigError, TrustedBundle
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CLAIM_FILENAME,
    MANIFEST_FILENAME,
    CheckpointStore,
    RunManifest,
)


def test_service_import_has_no_execution_path() -> None:
    script = """
import json
import sys

import openadapt_flow.runner.business_decision_service

prohibited = (
    "openadapt_flow.backend",
    "openadapt_flow.console.human_decisions",
    "openadapt_flow.runtime.durable.attended",
    "openadapt_flow.runtime.durable.attended_service",
    "openadapt_flow.runtime.durable.continuation",
    "openadapt_flow.runtime.replayer",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prohibited)
)
print(json.dumps(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []


def _key_file(path: Path) -> Path:
    encoded = base64.b64encode(b"k" * 32).decode("ascii")
    payload = {
        "schema_version": KEY_SCHEMA,
        "task_signing_key": encoded,
        "task_issuer_key_id": "task_key_01",
        "qualification_signing_key": encoded,
        "qualification_issuer_key_id": "qualification_key_01",
        "answer_signing_key": encoded,
        "answer_issuer_key_id": "answer_key_01",
        "receipt_signing_key": encoded,
        "receipt_issuer_key_id": "receipt_key_01",
        "role_mapping_key": encoded,
        "privacy_key": encoded,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_key_material_requires_private_current_user_file(tmp_path: Path) -> None:
    path = _key_file(tmp_path / "keys.json")

    material = load_business_decision_key_material(path)

    assert len(material.keys.task_signing_key) == 32
    path.chmod(0o644)
    with pytest.raises(RunnerConfigError, match="private regular"):
        load_business_decision_key_material(path)


def test_windows_key_file_replacement_race_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow.runner import business_decision_service as service

    path = _key_file(tmp_path / "keys.json")
    original_lstat = service.os.lstat
    before = original_lstat(path)
    replaced_values = list(before)
    replaced_values[1] = before.st_ino + 1
    replaced = service.os.stat_result(replaced_values)
    observations = iter((before, replaced))
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.os, "lstat", lambda _path: next(observations))
    monkeypatch.setattr(
        service, "_windows_descriptor_has_private_acl", lambda _fd: True
    )

    with pytest.raises(RunnerConfigError, match="private regular"):
        load_business_decision_key_material(path)


@pytest.mark.parametrize("foreign_allow", [False, True])
def test_windows_key_acl_allows_only_service_and_system_identities(
    monkeypatch, foreign_allow: bool
) -> None:
    current = "S-1-current"
    system = "S-1-5-18"
    administrators = "S-1-5-32-544"
    aces = [((0,), 1, current), ((0,), 1, system), ((0,), 1, administrators)]
    if foreign_allow:
        aces.append(((0,), 1, "S-1-foreign"))

    class Dacl:
        def GetAceCount(self):
            return len(aces)

        def GetAce(self, index):
            return aces[index]

    class Security:
        def GetSecurityDescriptorOwner(self):
            return current

        def GetSecurityDescriptorDacl(self):
            return Dacl()

    win32security = SimpleNamespace(
        SE_FILE_OBJECT=1,
        OWNER_SECURITY_INFORMATION=2,
        DACL_SECURITY_INFORMATION=4,
        TokenUser=5,
        GetSecurityInfo=lambda *_args: Security(),
        OpenProcessToken=lambda *_args: "token",
        GetTokenInformation=lambda *_args: (current,),
        EqualSid=lambda left, right: left == right,
        ConvertStringSidToSid=lambda value: value,
    )
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _fd: 1)
    )
    monkeypatch.setitem(
        sys.modules,
        "ntsecuritycon",
        SimpleNamespace(ACCESS_ALLOWED_ACE_TYPE=0, ACCESS_DENIED_ACE_TYPE=1),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(GetCurrentProcess=lambda: "process"),
    )
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(TOKEN_QUERY=8))
    monkeypatch.setitem(sys.modules, "win32security", win32security)

    assert _windows_descriptor_has_private_acl(7) is (not foreign_allow)


def _exact_run(
    tmp_path: Path,
    *,
    key: str | None,
    allow_unencrypted: bool = False,
    params: dict[str, str] | None = None,
    authorization_params: dict[str, str] | None = None,
    authorization_profile: str = "standard",
    authorization_policy_name: str | None = None,
) -> tuple[Path, RunnerConfig, Policy, str]:
    bundle = tmp_path / "bundle"
    workflow = Workflow(
        name="typed-decision-service",
        steps=[
            Step(
                id="open-record",
                intent="open the selected record",
                action=ActionKind.KEY,
                key="ENTER",
            )
        ],
    )
    workflow.save(bundle, encrypt=key is not None, key=key)
    assert workflow.manifest is not None
    policy = Policy(name="production")
    exact_params = params or {"record_id": "R-100"}
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(
            workflow,
            authorization_params or exact_params,
            {},
        ),
        admitted_policy_name=authorization_policy_name or policy.name,
        admitted_policy_contract_sha256=policy_contract_sha256(policy),
        execution_profile=authorization_profile,
    )
    run_dir = tmp_path / "run"
    store = CheckpointStore(run_dir, key=key)
    store.write_fresh_manifest(
        RunManifest(
            run_id="run-service-0001",
            namespace_id="namespace-service-0001",
            canonical_run_dir=str(run_dir.resolve()),
            workflow_name=workflow.name,
            bundle_dir=str(bundle.resolve()),
            params=exact_params,
            worklists={},
            governed_authorization=authorization,
        )
    )
    config = RunnerConfig(
        name="runner",
        bundles={
            workflow.manifest.content_digest: TrustedBundle(
                content_digest=workflow.manifest.content_digest,
                path=bundle,
                policy=policy.name,
                allow_unencrypted=allow_unencrypted,
            )
        },
    )
    return run_dir, config, policy, workflow.manifest.content_digest


def test_exact_run_workflow_loads_encrypted_bundle_and_checkpoint(
    tmp_path: Path,
) -> None:
    run_dir, config, policy, digest = _exact_run(tmp_path, key="deployment-secret")

    result = _load_exact_run_workflow(
        run_dir,
        checkpoint_key="deployment-secret",
        runner_config=config,
        policy=policy,
        execution_profile="standard",
    )

    assert result.encrypted is True
    assert result.manifest is not None
    assert result.manifest.content_digest == digest
    manifest_path = run_dir / "checkpoints" / MANIFEST_FILENAME
    assert not manifest_path.is_file()
    assert manifest_path.with_suffix(manifest_path.suffix + ".enc").is_file()


def test_exact_run_workflow_refuses_wrong_encryption_key(tmp_path: Path) -> None:
    run_dir, config, policy, _ = _exact_run(tmp_path, key="deployment-secret")

    with pytest.raises(BusinessDecisionCloudRefused, match="authorized local key"):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key="wrong-secret",
            runner_config=config,
            policy=policy,
            execution_profile="standard",
        )


def test_exact_run_workflow_refuses_plaintext_manifest_under_encrypted_trust(
    tmp_path: Path,
) -> None:
    key = "deployment-secret"
    run_dir, config, policy, _ = _exact_run(tmp_path, key=key)
    store = CheckpointStore(run_dir, key=key)
    manifest = store.read_manifest()
    assert manifest is not None
    manifest_path = store.checkpoints_dir / MANIFEST_FILENAME
    encrypted_path = manifest_path.with_name(manifest_path.name + ".enc")
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    encrypted_path.unlink()

    with pytest.raises(
        BusinessDecisionCloudRefused, match="manifest requires encryption"
    ):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key=key,
            runner_config=config,
            policy=policy,
            execution_profile="standard",
        )


def test_exact_run_workflow_refuses_plaintext_without_local_opt_in(
    tmp_path: Path,
) -> None:
    run_dir, config, policy, _ = _exact_run(tmp_path, key=None)

    with pytest.raises(BusinessDecisionCloudRefused, match="requires encryption"):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key=None,
            runner_config=config,
            policy=policy,
            execution_profile="standard",
        )


def test_exact_run_workflow_accepts_plaintext_only_with_local_opt_in(
    tmp_path: Path,
) -> None:
    run_dir, config, policy, _ = _exact_run(
        tmp_path,
        key=None,
        allow_unencrypted=True,
    )

    result = _load_exact_run_workflow(
        run_dir,
        checkpoint_key=None,
        runner_config=config,
        policy=policy,
        execution_profile="standard",
    )

    assert result.encrypted is False


def test_exact_run_workflow_refuses_changed_runtime_inputs(tmp_path: Path) -> None:
    run_dir, config, policy, _ = _exact_run(
        tmp_path,
        key=None,
        allow_unencrypted=True,
        params={"record_id": "R-200"},
        authorization_params={"record_id": "R-100"},
    )

    with pytest.raises(BusinessDecisionCloudRefused, match="exact runtime inputs"):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key=None,
            runner_config=config,
            policy=policy,
            execution_profile="standard",
        )


def test_exact_run_workflow_refuses_changed_durable_namespace(tmp_path: Path) -> None:
    run_dir, config, policy, _ = _exact_run(
        tmp_path,
        key=None,
        allow_unencrypted=True,
    )
    claim = run_dir / CLAIM_FILENAME
    raw = json.loads(claim.read_text(encoding="utf-8"))
    raw["run_id"] = "another-run"
    claim.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BusinessDecisionCloudRefused, match="namespace"):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key=None,
            runner_config=config,
            policy=policy,
            execution_profile="standard",
        )


@pytest.mark.parametrize(
    ("authorization_profile", "authorization_policy_name", "selected_profile"),
    [
        ("demo", None, "standard"),
        ("standard", "another-policy", "standard"),
    ],
)
def test_exact_run_workflow_refuses_profile_or_policy_mismatch(
    tmp_path: Path,
    authorization_profile: str,
    authorization_policy_name: str | None,
    selected_profile: str,
) -> None:
    run_dir, config, policy, _ = _exact_run(
        tmp_path,
        key=None,
        allow_unencrypted=True,
        authorization_profile=authorization_profile,
        authorization_policy_name=authorization_policy_name,
    )

    with pytest.raises(BusinessDecisionCloudRefused, match="profile or policy"):
        _load_exact_run_workflow(
            run_dir,
            checkpoint_key=None,
            runner_config=config,
            policy=policy,
            execution_profile=selected_profile,
        )


class _Supervisor:
    def __init__(self) -> None:
        self.calls = 0

    def serve_once(self, *, wait_s: float):
        self.calls += 1
        assert wait_s == 0
        return SimpleNamespace(
            publishes=SimpleNamespace(
                published=1,
                already_published=0,
                uncertain=0,
                not_projectable=0,
                refused=0,
            ),
            answer_recorded=True,
            receipt_confirmed=True,
            unmatched_refusal_confirmed=False,
        )


def test_service_loop_emits_phifree_health_after_relay_only_cycle() -> None:
    supervisor = _Supervisor()
    loop = BusinessDecisionServiceLoop(supervisor, wait_s=0)

    health = loop.serve_once()
    payload = json.loads(health.as_json())

    assert supervisor.calls == 1
    assert payload == {
        "active_tasks": 1,
        "already_published": 0,
        "answers_recorded": 1,
        "consecutive_failures": 0,
        "cycles": 1,
        "not_projectable": 0,
        "published": 1,
        "receipts_confirmed": 1,
        "refused": 0,
        "schema_version": "openadapt.business-decision-supervisor-health/v1",
        "state": "ready",
        "uncertain": 0,
        "unmatched_refusals_confirmed": 0,
    }


def test_service_loop_reports_transport_refusal_without_action() -> None:
    class RefusingSupervisor:
        def serve_once(self, *, wait_s: float):
            raise ValueError("refused")

    health = BusinessDecisionServiceLoop(RefusingSupervisor(), wait_s=0).serve_once()

    assert health.state == "degraded"
    assert health.consecutive_failures == 1


def test_cli_exposes_a_relay_only_service_command() -> None:
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(
        [
            "business-decisions",
            "serve",
            "--runs",
            "runs",
            "--profile",
            "production",
            "--once",
        ]
    )

    assert args.business_decisions_cmd == "serve"
    assert args.once is True
    assert args.poll_wait_seconds == 25.0


def _serve_args() -> SimpleNamespace:
    return SimpleNamespace(
        runner_config=None,
        cloud_origin=None,
        runs="runs",
        profile="production",
        poll_wait_seconds=0,
        once=True,
    )


def test_service_cli_reports_setup_refusal_without_traceback(
    monkeypatch, capsys
) -> None:
    from openadapt_flow.__main__ import _cmd_business_decisions_serve

    monkeypatch.setattr(
        "openadapt_flow.runner.config.load_runner_config",
        lambda _path=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.resolve_business_decision_origin",
        lambda _origin=None: "https://app.openadapt.ai",
    )

    def refuse_setup(**_kwargs):
        raise RelayRefused("runner token is invalid")

    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.build_business_decision_supervisor",
        refuse_setup,
    )

    assert _cmd_business_decisions_serve(_serve_args()) == 2
    captured = capsys.readouterr()
    assert "runner token is invalid" in captured.err
    assert "Traceback" not in captured.err


def test_service_cli_reports_missing_policy_without_traceback(
    monkeypatch, capsys
) -> None:
    from openadapt_flow.__main__ import _cmd_business_decisions_serve

    monkeypatch.setattr(
        "openadapt_flow.runner.config.load_runner_config",
        lambda _path=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.resolve_business_decision_origin",
        lambda _origin=None: "https://app.openadapt.ai",
    )

    def missing_policy(**_kwargs):
        raise FileNotFoundError("the configured policy file does not exist")

    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.build_business_decision_supervisor",
        missing_policy,
    )

    assert _cmd_business_decisions_serve(_serve_args()) == 2
    captured = capsys.readouterr()
    assert "configured policy file does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_service_cli_once_returns_nonzero_for_degraded_health(
    monkeypatch, capsys
) -> None:
    from openadapt_flow.__main__ import _cmd_business_decisions_serve

    class RefusingSupervisor:
        def serve_once(self, *, wait_s: float):
            raise RelayRefused("temporary relay refusal")

    monkeypatch.setattr(
        "openadapt_flow.runner.config.load_runner_config",
        lambda _path=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.resolve_business_decision_origin",
        lambda _origin=None: "https://app.openadapt.ai",
    )
    monkeypatch.setattr(
        "openadapt_flow.runner.business_decision_service.build_business_decision_supervisor",
        lambda **_kwargs: RefusingSupervisor(),
    )

    assert _cmd_business_decisions_serve(_serve_args()) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "degraded"
    assert payload["consecutive_failures"] == 1
