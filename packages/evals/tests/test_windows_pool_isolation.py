from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openadapt_evals.infrastructure.pool import (
    WAA_START_SCRIPT_TEMPLATE,
    WINDOWS_EGRESS_BOOTSTRAP_SCRIPT,
    PoolManager,
)
from openadapt_evals.infrastructure.ssh_tunnel import SSHTunnelManager, TunnelConfig
from openadapt_evals.infrastructure.windows_pool_isolation import (
    EGRESS_PROOF_SCHEMA,
    EGRESS_SCHEMA,
    MAX_GATE_AGE,
    RESET_SCHEMA,
    WORKER_IDENTITY_SCHEMA,
    EgressProof,
    ResetProof,
    WindowsPoolIsolationError,
    WindowsPoolIsolationManager,
    build_block_all_batch,
    build_nft_batch,
    dispatch_binding_sha256,
    egress_policy_sha256,
    reset_proof_sha256,
    validate_egress_policy,
    validate_reset_proof,
    validate_worker_identity,
    worker_identity_sha256,
)

NOW = datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc)


def _identity(**overrides):
    public_key = "ssh-ed25519 " + base64.b64encode(b"k" * 32).decode()
    value = {
        "schema_version": WORKER_IDENTITY_SCHEMA,
        "provider": "azure",
        "provider_account_id": "subscription-123",
        "resource_id": "/subscriptions/123/resourceGroups/oa/providers/Microsoft.Compute/virtualMachines/waa-pool-00",
        "instance_id": "waa-pool-00-instance",
        "ssh_host": "192.0.2.10",
        "ssh_host_public_key": public_key,
        "ssh_host_key_sha256": "sha256:" + hashlib.sha256(public_key.encode()).hexdigest(),
        "baseline_manifest_sha256": "sha256:" + "b" * 64,
        "admitted_image_sha256": "sha256:" + "b" * 64,
        "admitted_runtime_sha256": "sha256:" + "c" * 64,
        "host_bindings_sha256": "sha256:" + "a" * 64,
        "tls_bindings_sha256": "sha256:" + "d" * 64,
    }
    value.update(overrides)
    return validate_worker_identity(value)


def _policy(**overrides):
    value = {
        "schema_version": EGRESS_SCHEMA,
        "run_id": "run-123",
        "allowed_ipv4": ["10.20.30.40", "198.51.100.10"],
        "allowed_ipv6": ["2001:db8::10"],
        "host_bindings_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


def _reset_proof(identity=None, **overrides):
    identity = identity or _identity()
    value = {
        "schema_version": RESET_SCHEMA,
        "worker": "waa-pool-00",
        "run_id": "run-123",
        "baseline_sha256": "b" * 64,
        "container_state": "stopped",
        "worker_identity_sha256": worker_identity_sha256(identity),
        "admitted_image_sha256": identity.admitted_image_sha256,
        "admitted_runtime_sha256": identity.admitted_runtime_sha256,
        "host_bindings_sha256": identity.host_bindings_sha256,
        "tls_bindings_sha256": identity.tls_bindings_sha256,
        "issued_at": "2026-08-27T18:00:00Z",
        "expires_at": "2026-08-27T18:05:00Z",
    }
    value.update(overrides)
    return ResetProof(**value)


def _egress_proof(identity=None, reset=None, **overrides):
    identity = identity or _identity()
    reset = reset or _reset_proof(identity)
    value = {
        "schema_version": EGRESS_PROOF_SCHEMA,
        "worker": "waa-pool-00",
        "run_id": "run-123",
        "policy_sha256": egress_policy_sha256(_policy()),
        "reset_proof_sha256": reset_proof_sha256(reset),
        "worker_identity_sha256": worker_identity_sha256(identity),
        "host_bindings_sha256": identity.host_bindings_sha256,
        "tls_bindings_sha256": identity.tls_bindings_sha256,
        "conntrack_drained": True,
        "issued_at": "2026-08-27T18:00:00Z",
        "expires_at": "2026-08-27T18:05:00Z",
    }
    value.update(overrides)
    return EgressProof(**value)


def _manager(identity=None):
    return WindowsPoolIsolationManager(
        ssh_host="192.0.2.10",
        ssh_user="azureuser",
        worker="waa-pool-00",
        identity=identity or _identity(),
    )


def test_egress_policy_is_closed_unique_sorted_and_domain_bound():
    policy = _policy()
    assert validate_egress_policy(policy) == policy
    assert len(egress_policy_sha256(policy)) == 64
    assert egress_policy_sha256(_policy(allowed_ipv4=["10.20.30.41"])) != (
        egress_policy_sha256(policy)
    )
    with pytest.raises(WindowsPoolIsolationError, match="closed"):
        validate_egress_policy({**policy, "hidden": True})


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "224.0.0.1",
        "10.0.0.0/24",
        "169.254.169.254",
        "168.63.129.16",
        "100.100.100.200",
        "fe80::1",
        "fd00:ec2::254",
    ],
)
def test_egress_policy_rejects_unsafe_or_metadata_targets(address):
    key = "allowed_ipv6" if ":" in address else "allowed_ipv4"
    with pytest.raises(WindowsPoolIsolationError):
        validate_egress_policy(_policy(**{key: [address]}))


def test_block_all_and_replacement_never_preserve_an_old_established_flow():
    allowed = build_nft_batch("waa-pool-00", _policy())
    blocked = build_block_all_batch("waa-pool-00", "run-123")
    assert "ct state established,related accept" not in blocked
    return_rules = [line for line in allowed.splitlines() if "ct state" in line]
    assert return_rules
    assert all(" saddr " in line and 'oifname "docker0"' in line for line in return_rules)
    assert " daddr " not in blocked
    assert blocked.rstrip().endswith("counter drop")


def test_worker_bootstrap_restores_block_all_and_conntrack_before_docker():
    script = WINDOWS_EGRESS_BOOTSTRAP_SCRIPT
    assert 'iifname "docker0" counter drop' in script
    assert "conntrack nftables" in script
    assert "nft -c -f /etc/openadapt/windows-egress.nft" in script
    assert "policy drop" in script
    assert "Before=network-pre.target docker.service" in script
    assert "windows-run-gates -mindepth 1 -maxdepth 1" in script


def test_worker_identity_pins_host_key_image_runtime_host_and_tls_bindings():
    identity = _identity()
    assert worker_identity_sha256(identity).startswith("sha256:")
    with pytest.raises(WindowsPoolIsolationError, match="host key digest"):
        _identity(ssh_host_key_sha256="sha256:" + "0" * 64)
    with pytest.raises(WindowsPoolIsolationError, match="SSH host differs"):
        WindowsPoolIsolationManager(
            ssh_host="192.0.2.11",
            ssh_user="azureuser",
            worker="waa-pool-00",
            identity=identity,
        )


def test_reset_blocks_egress_first_and_returns_identity_bound_fresh_proof():
    remote = {
        "schema_version": RESET_SCHEMA,
        "worker": "waa-pool-00",
        "run_id": "run-123",
        "baseline_sha256": "b" * 64,
        "container_state": "stopped",
    }
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = b""
        if kwargs.get("input") and b"openadapt-windows-baseline-manifest" in kwargs["input"]:
            stdout = json.dumps(remote).encode()
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    with (
        patch("openadapt_evals.infrastructure.windows_pool_isolation._now", return_value=NOW),
        patch("subprocess.run", side_effect=_run),
    ):
        proof = _manager().reset(run_id="run-123")
    validate_reset_proof(
        proof,
        worker="waa-pool-00",
        run_id="run-123",
        identity=_identity(),
        now=NOW,
    )
    assert proof.admitted_image_sha256 == _identity().admitted_image_sha256
    assert any("StrictHostKeyChecking=yes" in call[0] for call in calls)
    persist_program = next(
        argument
        for argv, _ in calls
        for argument in argv
        if isinstance(argument, str) and "conntrack" in argument
    )
    assert persist_program.count("conntrack', '-F") == 2
    reset_script = next(
        kwargs["input"].decode()
        for _, kwargs in calls
        if kwargs.get("input") and b"openadapt-windows-baseline-manifest" in kwargs["input"]
    )
    assert "windows-last-guest-addresses" in reset_script
    reset_gate_script = next(
        kwargs["input"].decode()
        for _, kwargs in calls
        if kwargs.get("input") and b"reset_gate/proof.sha256" in kwargs["input"]
    )
    assert 'reset_gate="$gate_root/${run_id}.reset"' in reset_gate_script
    assert "flock -n 9" in reset_gate_script
    assert reset_proof_sha256(proof) in next(
        argument for argv, _ in calls for argument in argv if argument == reset_proof_sha256(proof)
    )


def test_reset_proof_refuses_expiry_identity_drift_and_run_reuse():
    proof = _reset_proof()
    with pytest.raises(WindowsPoolIsolationError, match="stale"):
        validate_reset_proof(
            proof,
            worker="waa-pool-00",
            run_id="run-123",
            identity=_identity(),
            now=NOW + MAX_GATE_AGE,
        )
    with pytest.raises(WindowsPoolIsolationError, match="baseline_sha256 differs"):
        validate_reset_proof(
            _reset_proof(baseline_sha256="0" * 64),
            worker="waa-pool-00",
            run_id="run-123",
            identity=_identity(),
            now=NOW,
        )
    with pytest.raises(WindowsPoolIsolationError, match="run_id differs"):
        validate_reset_proof(
            proof,
            worker="waa-pool-00",
            run_id="run-456",
            identity=_identity(),
            now=NOW,
        )
    with pytest.raises(WindowsPoolIsolationError, match="worker_identity_sha256"):
        validate_reset_proof(
            proof,
            worker="waa-pool-00",
            run_id="run-123",
            identity=_identity(resource_id="/other/instance"),
            now=NOW,
        )


def test_apply_egress_binds_reset_identity_host_tls_and_drains_conntrack():
    reset = _reset_proof()
    completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    with (
        patch("openadapt_evals.infrastructure.windows_pool_isolation._now", return_value=NOW),
        patch("subprocess.run", return_value=completed) as run,
    ):
        proof = _manager().apply_egress(_policy(), reset_proof=reset)
    assert proof.policy_sha256 == egress_policy_sha256(_policy())
    assert proof.reset_proof_sha256 == reset_proof_sha256(reset)
    assert proof.conntrack_drained is True
    sent = next(
        call.kwargs["input"].decode()
        for call in run.call_args_list
        if call.kwargs.get("input") and b"flush chain" in call.kwargs["input"]
    )
    assert sent == build_nft_batch("waa-pool-00", _policy())
    remote_program = next(
        argument
        for call in run.call_args_list
        for argument in call.args[0]
        if isinstance(argument, str) and "gate_created" in argument
    )
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in remote_program
    assert "os.O_CREAT | os.O_EXCL" in remote_program
    assert "guest is not stopped" in remote_program
    assert "remote reset proof differs" in remote_program
    assert "reset proof already consumed" in remote_program
    assert "os.replace(reset_gate, reset_consumed_gate)" in remote_program
    assert remote_program.count("['conntrack', '-F']") == 2
    assert "guest conntrack state remains after drain" in remote_program
    assert "/run/openadapt/windows-egress-active.nft" in remote_program


def test_start_gate_consumes_once_and_checks_exact_policy_image_and_bindings():
    reset = _reset_proof()
    egress = _egress_proof(reset=reset)
    starts = 0

    def _run(argv, **kwargs):
        nonlocal starts
        if kwargs.get("input") and b"windows-run-gates" in kwargs["input"]:
            starts += 1
            return subprocess.CompletedProcess(
                argv,
                0 if starts == 1 else 1,
                stdout=b"",
                stderr=b"already consumed" if starts > 1 else b"",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    manager = _manager()
    with (
        patch("openadapt_evals.infrastructure.windows_pool_isolation._now", return_value=NOW),
        patch("subprocess.run", side_effect=_run),
    ):
        proof = manager.start(
            policy=_policy(),
            reset_proof=reset,
            egress_proof=egress,
            start_script="echo STARTED",
        )
        assert proof.one_use_consumed is True
        with pytest.raises(WindowsPoolIsolationError, match="already consumed"):
            manager.start(
                policy=_policy(),
                reset_proof=reset,
                egress_proof=egress,
                start_script="echo STARTED",
            )


def test_start_command_uses_the_admitted_image_digest_instead_of_a_mutable_tag():
    rendered = WAA_START_SCRIPT_TEMPLATE.format(
        home_dir="/home/azureuser",
        ssh_username="azureuser",
        admitted_image="sha256:" + "b" * 64,
    )
    assert "waa-auto:latest" not in rendered
    assert "sha256:" + "b" * 64 in rendered


def test_benchmark_dispatch_refuses_a_responsive_legacy_worker():
    worker = SimpleNamespace(
        waa_ready=True,
        status="ready",
        qualified_run_id=None,
        qualified_worker_identity_sha256=None,
        qualified_egress_policy_sha256=None,
        qualified_start_proof_sha256=None,
    )
    manager = object.__new__(PoolManager)
    manager.registry = SimpleNamespace(get_pool=lambda: SimpleNamespace(workers=[worker]))
    with pytest.raises(RuntimeError, match="No qualified workers"):
        manager.run(api_key="test-only")


def test_benchmark_dispatch_refuses_start_proof_reuse_for_a_task_batch():
    worker = SimpleNamespace(
        name="waa-pool-00",
        waa_ready=True,
        status="qualified-ready",
        qualified_run_id="run-123",
        qualified_worker_identity_sha256="sha256:" + "1" * 64,
        qualified_egress_policy_sha256="2" * 64,
        qualified_start_proof_sha256="sha256:" + "3" * 64,
    )
    manager = object.__new__(PoolManager)
    manager.registry = SimpleNamespace(get_pool=lambda: SimpleNamespace(workers=[worker]))
    with pytest.raises(RuntimeError, match="exactly one task"):
        manager.run(tasks=2, api_key="test-only", qualification_dir=Path("."))


def test_live_start_and_dispatch_rechecks_use_the_pinned_boundary():
    calls = []
    dispatches = 0

    def _run(argv, **kwargs):
        nonlocal dispatches
        calls.append((argv, kwargs))
        if kwargs.get("input") and b"dispatch_gate=" in kwargs["input"]:
            dispatches += 1
            if dispatches > 1:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=b"",
                    stderr=b"dispatch gate already consumed",
                )
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n", stderr=b"")

    binding = dispatch_binding_sha256(
        {
            "schema_version": "openadapt.windows-qualified-dispatch/v1",
            "worker": "waa-pool-00",
            "qualified_run_id": "run-123",
            "qualified_start_proof_sha256": "sha256:" + "3" * 64,
            "task_id": "domain:task-1",
            "agent": "navi",
            "model": "model-1",
        }
    )
    with patch("subprocess.run", side_effect=_run):
        manager = _manager()
        manager.verify_started(
            run_id="run-123",
            policy_sha256="e" * 64,
        )
        manager.consume_dispatch(
            run_id="run-123",
            policy_sha256="e" * 64,
            task_binding_sha256=binding,
        )
        with pytest.raises(WindowsPoolIsolationError, match="already consumed"):
            manager.consume_dispatch(
                run_id="run-123",
                policy_sha256="e" * 64,
                task_binding_sha256=binding,
            )
        manager.run_command(["docker", "exec", "winarena", "true"])

    assert calls
    assert all("StrictHostKeyChecking=yes" in argv for argv, _ in calls)
    programs = [
        kwargs["input"].decode()
        for _, kwargs in calls
        if kwargs.get("input") is not None
    ]
    assert any("docker inspect" in program and "reset-consumed" in program for program in programs)
    assert any("task-binding.sha256" in program and "flock -n" in program for program in programs)


def test_qualified_tunnel_rejects_unknown_ports_and_pins_the_admitted_host_key():
    popen_calls = []
    process = SimpleNamespace(pid=4321, poll=lambda: None)

    def _popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return process

    tunnel = SSHTunnelManager(
        tunnels=[TunnelConfig(name="waa", local_port=15001, remote_port=5000)],
        host_key_alias="waa-pool-00-instance",
        host_public_key=_identity().ssh_host_public_key,
    )
    with (
        patch.object(tunnel, "_is_port_in_use", return_value=False),
        patch("subprocess.Popen", side_effect=_popen),
        patch("time.sleep"),
    ):
        status = tunnel.start_tunnels_for_vm("192.0.2.10")
    assert status["waa"].active is True
    command = popen_calls[0][0]
    assert "StrictHostKeyChecking=yes" in command
    assert "StrictHostKeyChecking=no" not in command
    assert "HostKeyAlias=waa-pool-00-instance" in command
    tunnel._active_tunnels.clear()
    tunnel.stop_all_tunnels()

    occupied = SSHTunnelManager(
        tunnels=[TunnelConfig(name="waa", local_port=15001, remote_port=5000)],
        host_key_alias="waa-pool-00-instance",
        host_public_key=_identity().ssh_host_public_key,
    )
    with patch.object(occupied, "_is_port_in_use", return_value=True):
        refused = occupied.start_tunnels_for_vm("192.0.2.10")
    assert refused["waa"].active is False
    assert "already in use" in refused["waa"].error


def test_builtin_dispatch_binds_one_exact_task_and_never_places_the_api_key_in_argv():
    worker = SimpleNamespace(
        name="waa-pool-00",
        ip="192.0.2.10",
        waa_ready=True,
        status="qualified-ready",
        qualified_run_id="run-123",
        qualified_worker_identity_sha256="sha256:" + "1" * 64,
        qualified_egress_policy_sha256="2" * 64,
        qualified_start_proof_sha256="sha256:" + "3" * 64,
        qualified_task_binding_sha256=None,
        current_task=None,
    )
    pool = SimpleNamespace(workers=[worker], total_tasks=0)

    class Registry:
        def get_pool(self):
            return pool

        def save(self):
            return None

        def update_worker(self, _name, **values):
            for key, value in values.items():
                setattr(worker, key, value)

        def update_pool_progress(self, **_values):
            return None

    commands = []
    secret_inputs = []
    consumed = []

    class Isolation:
        def consume_dispatch(self, **values):
            consumed.append(values)

        def run_qualified_command(self, argv, **kwargs):
            assert kwargs["run_id"] == "run-123"
            assert kwargs["policy_sha256"] == "2" * 64
            commands.append(argv)
            if kwargs.get("input_bytes") is not None:
                secret_inputs.append(kwargs["input_bytes"])
            joined = " ".join(argv)
            if "evaluation_examples_windows/test_all.json" in joined:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=b'{"domain":"chrome","task_id":"task-123"}\n',
                    stderr=b"",
                )
            if "if pgrep" in joined:
                return subprocess.CompletedProcess(argv, 0, stdout=b"DONE\n", stderr=b"")
            if "benchmark.exit" in joined:
                return subprocess.CompletedProcess(argv, 0, stdout=b"0\n", stderr=b"")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    manager = PoolManager(
        vm_manager=SimpleNamespace(ssh_username="azureuser"),
        registry=Registry(),
        log_fn=lambda *_args, **_kwargs: None,
    )
    with (
        patch.object(manager, "_qualified_isolation_manager", return_value=(Isolation(), {})),
        patch("time.sleep"),
    ):
        result = manager.run(
            tasks=1,
            agent="navi",
            model="model-1",
            api_key="secret-test-key",
            qualification_dir=Path("."),
        )

    assert result.completed == 1
    assert consumed[0]["run_id"] == "run-123"
    assert consumed[0]["task_binding_sha256"] == worker.qualified_task_binding_sha256
    assert worker.current_task == "chrome:task-123"
    assert worker.status == "qualified-dispatched"
    assert secret_inputs == [b"secret-test-key\n"]
    assert all("secret-test-key" not in argument for command in commands for argument in command)


def test_oa_vm_cli_exposes_the_three_step_qualified_start():
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_evals.benchmarks.vm_cli", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "pool-reset" in result.stdout
    assert "pool-egress" in result.stdout
    assert "pool-start" in result.stdout

    auto_help = subprocess.run(
        [sys.executable, "-m", "openadapt_evals.benchmarks.vm_cli", "pool-auto", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert auto_help.returncode == 0, auto_help.stderr
    assert "--qualification-dir" in auto_help.stdout
    assert "--run-evidence-dir" in auto_help.stdout
    assert "--baseline-sha256" not in auto_help.stdout
