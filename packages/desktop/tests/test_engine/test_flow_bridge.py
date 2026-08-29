"""Tests for the openadapt-flow CLI wrapper."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from engine.flow_bridge import (
    EMBEDDED_FLOW_MODE,
    BrowserRuntimeError,
    FlowBridge,
    FlowNotAvailableError,
    HostedRunnerAdapterUnavailableError,
    _safe_command_for_log,
    flow_available,
)
from engine.targets import ExecutionTarget


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakePopen:
    def __init__(self, recorder, command, **kwargs):
        recorder.append((command, kwargs))
        self.returncode = None

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.returncode = 0
        return ("recorded", "")

    def terminate(self):
        self.returncode = 1


def _runner(recorder, returncode=0, stdout="", stderr=""):
    def run(cmd, capture_output=True, text=True, timeout=None, env=None):
        recorder.append((cmd, env))
        return FakeProc(returncode, stdout, stderr)

    return run


class TestFlowBridgeInvocation:
    def test_hosted_adapter_absence_fails_closed(self, monkeypatch) -> None:
        def missing(_name: str):
            raise ModuleNotFoundError("adapter is not installed")

        monkeypatch.setattr("engine.flow_bridge.import_module", missing)

        with pytest.raises(HostedRunnerAdapterUnavailableError, match="newer bundled"):
            FlowBridge.hosted_runner_contract()

    def test_compile_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="ok"))
        result = bridge.compile(tmp_path / "rec", tmp_path / "bundle")
        assert result.ok
        command, env = calls[0]
        assert command[1] == "compile"
        assert "--out" in command
        assert env is not None
        # openadapt-flow compile requires --name; default it to the bundle name.
        assert "--name" in command
        assert command[command.index("--name") + 1] == "bundle"

    def test_qualify_from_demo_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="{}"))
        rec = tmp_path / "rec"
        bundle = tmp_path / "bundle"
        result = bridge.qualify_from_demo(bundle, rec, admit_local=True)
        assert result.ok
        command, _env = calls[0]
        assert command[1:3] == ["qualify", "from-demo"]
        assert "--recording" in command
        assert "--admit-local" in command
        assert "--policy-pack" in command

    def test_report_break_keeps_token_out_of_argv(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="Nothing emitted: no halt"))

        bridge.report_break(
            tmp_path / "run",
            workflow_id="workflow-1",
            host="https://app.openadapt.ai",
            env_overrides={"OPENADAPT_INGEST_TOKEN": "secret-value"},
        )

        command, env = calls[0]
        assert "secret-value" not in command
        assert "--token" not in command
        assert env["OPENADAPT_INGEST_TOKEN"] == "secret-value"

    def test_push_keeps_token_and_local_name_out_of_argv(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="--json\nok"))

        bridge.push(
            tmp_path / "bundle",
            kind="bundle",
            host="https://app.openadapt.ai",
            name="Jane Doe patient transfer",
            token="secret-value",
        )

        command, env = calls[1]
        assert "secret-value" not in command
        assert "Jane Doe patient transfer" not in command
        assert "--token" not in command
        assert "--name" not in command
        assert env["OPENADAPT_INGEST_TOKEN"] == "secret-value"

    def test_demo_record_uses_canonical_bundled_flow_command(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "engine.flow_bridge.shutil.which",
            lambda _: "/usr/bin/openadapt-flow",
        )
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="ok"))

        result = bridge.demo_record(tmp_path / "recording")

        assert result.ok
        command, _env = calls[0]
        assert command == [
            "/usr/bin/openadapt-flow",
            "demo-record",
            "--out",
            str(tmp_path / "recording"),
        ]

    def test_run_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        bridge.run(tmp_path / "bundle", tmp_path / "cfg.yaml", out_dir=tmp_path / "run")
        command, _ = calls[0]
        assert command[1] == "run"
        assert "--config" in command
        # The run directory is passed via --run-dir (not --out).
        assert "--run-dir" in command

    def test_qualify_run_case_uses_flow_owned_authorization(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))

        bridge.qualify_run_case(
            tmp_path / "bundle",
            tmp_path / "cfg.yaml",
            case_id="representative-1",
            inputs_file=tmp_path / "runtime-inputs.json",
            campaign_id="campaign-1",
            run_id="run-1",
            out_dir=tmp_path / "run",
        )

        command, _ = calls[0]
        assert command[1:3] == ["qualify", "run-case"]
        assert command[command.index("--case-id") + 1] == "representative-1"
        assert command[command.index("--inputs") + 1] == str(tmp_path / "runtime-inputs.json")
        assert command[command.index("--campaign-id") + 1] == "campaign-1"
        assert command[command.index("--run-id") + 1] == "run-1"

    def test_qualification_inputs_and_bundle_key_stay_out_of_argv(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        params = tmp_path / "case-params.json"
        bridge.run(
            tmp_path / "bundle",
            tmp_path / "cfg.yaml",
            out_dir=tmp_path / "run",
            params_file=params,
            env_overrides={"OPENADAPT_BUNDLE_KEY": "protected-key"},
        )
        run_command, run_env = calls[0]
        assert run_command[run_command.index("--params-file") + 1] == str(params)
        assert "protected-key" not in run_command
        assert run_env["OPENADAPT_BUNDLE_KEY"] == "protected-key"

    def test_replay_uses_run_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        bridge.replay(tmp_path / "bundle", out_dir=tmp_path / "run")
        command, _ = calls[0]
        assert command[1] == "replay"
        assert "--run-dir" in command
        assert "--out" not in command

    def test_config_only_replay_never_injects_web_backend(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        deployment = tmp_path / "deployment.yaml"
        bridge.replay(
            tmp_path / "bundle",
            out_dir=tmp_path / "run",
            config=deployment,
        )

        command, _ = calls[0]
        assert command == [
            "/usr/bin/openadapt-flow",
            "replay",
            str(tmp_path / "bundle"),
            "--run-dir",
            str(tmp_path / "run"),
            "--config",
            str(deployment),
        ]
        assert "--backend" not in command

    def test_run_uses_only_private_config_path_for_target_details(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        deployment = tmp_path / ".deployment-private.yaml"
        bridge.run(
            tmp_path / "bundle",
            deployment,
            out_dir=tmp_path / "run",
        )

        command, _ = calls[0]
        assert command == [
            "/usr/bin/openadapt-flow",
            "run",
            str(tmp_path / "bundle"),
            "--config",
            str(deployment),
            "--run-dir",
            str(tmp_path / "run"),
        ]
        assert "--backend" not in command

    def test_record_process_argv_contains_only_private_request_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.find_spec", lambda _name: object())
        calls: list = []

        def popen(command, **kwargs):
            return FakePopen(calls, command, **kwargs)

        bridge = FlowBridge(popen=popen)
        request = tmp_path / ".record-request-private.yaml"
        request.write_text("Patient Jane Doe at rdp.internal")
        stop_path = tmp_path / ".stop"
        ready_path = tmp_path / ".ready"
        ready_path.touch()

        session = bridge.start_record(
            tmp_path / "recording",
            request=request,
            stop_path=stop_path,
            ready_path=ready_path,
        )

        command, _kwargs = calls[0]
        assert command == [
            sys.executable,
            "-m",
            "engine.flow_record_entry",
            str(request),
            "--watch-parent-stdin",
        ]
        assert _kwargs["stdin"] is subprocess.PIPE
        assert "Jane Doe" not in repr(command)
        assert "rdp.internal" not in repr(command)
        assert session.out_dir == tmp_path / "recording"

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (
                ExecutionTarget(backend="windows", agent_url="http://localhost:5001"),
                {"kind": "windows", "agent_url": "http://localhost:5001"},
            ),
            (
                ExecutionTarget(
                    backend="macos",
                    macos_app="TextEdit",
                    macos_window_title="Notes",
                ),
                {
                    "kind": "macos",
                    "macos_app": "TextEdit",
                    "macos_window_title": "Notes",
                },
            ),
            (
                ExecutionTarget(
                    backend="linux",
                    linux_app="gedit",
                    linux_window_title="Notes",
                    linux_allow_physical_input=True,
                ),
                {
                    "kind": "linux",
                    "linux_app": "gedit",
                    "linux_window_title": "Notes",
                    "linux_allow_physical_input": True,
                },
            ),
        ],
    )
    def test_native_targets_render_flow_deployment_overrides(
        self, target: ExecutionTarget, expected: dict[str, object]
    ) -> None:
        assert target.deployment_overrides() == expected

    @pytest.mark.parametrize(
        ("target", "expected_tail"),
        [
            (
                ExecutionTarget(
                    backend="windows",
                    agent_url="http://localhost:5001",
                ),
                [
                    "--agent-url",
                    "http://localhost:5001",
                ],
            ),
            (
                ExecutionTarget(
                    backend="macos",
                    macos_app="Clinical Notes",
                    macos_window_title="Patient Jane Doe",
                ),
                [
                    "--macos-app",
                    "Clinical Notes",
                    "--macos-window-title",
                    "Patient Jane Doe",
                    "--window",
                    "Clinical Notes",
                    "--window-title",
                    "Patient Jane Doe",
                ],
            ),
            (
                ExecutionTarget(
                    backend="linux",
                    linux_app="gedit",
                    linux_window_title="Clinical Notes",
                    linux_allow_physical_input=True,
                ),
                [
                    "--linux-app",
                    "gedit",
                    "--linux-window-title",
                    "Clinical Notes",
                    "--linux-allow-physical-input",
                ],
            ),
            (
                ExecutionTarget(
                    backend="rdp",
                    rdp_host="10.0.0.5",
                    rdp_readiness_text="Patient Search",
                ),
                [
                    "--rdp-host",
                    "10.0.0.5",
                    "--rdp-readiness-text",
                    "Patient Search",
                ],
            ),
            (
                ExecutionTarget(
                    backend="rdp",
                    rdp_window="Microsoft Remote Desktop",
                    rdp_window_title="Patient Jane Doe",
                    rdp_readiness_text="MRN 12345",
                ),
                [
                    "--rdp-window",
                    "Microsoft Remote Desktop",
                    "--rdp-window-title",
                    "Patient Jane Doe",
                    "--rdp-readiness-text",
                    "MRN 12345",
                ],
            ),
            (
                ExecutionTarget(
                    backend="citrix",
                    rdp_window="Citrix Viewer",
                    rdp_window_title="Patient Jane Doe",
                    rdp_readiness_text="MRN 12345",
                ),
                [
                    "--rdp-window",
                    "Citrix Viewer",
                    "--rdp-window-title",
                    "Patient Jane Doe",
                    "--rdp-readiness-text",
                    "MRN 12345",
                ],
            ),
        ],
    )
    def test_authoring_target_builds_exact_canonical_flow_record_arguments(
        self,
        tmp_path: Path,
        target: ExecutionTarget,
        expected_tail: list[str],
    ) -> None:
        args = target.record_args(tmp_path / "recording", task="Review workflow")

        assert args[:7] == [
            "record",
            "--out",
            str(tmp_path / "recording"),
            "--backend",
            target.backend,
            "--task",
            "Review workflow",
        ]
        assert args[7:] == expected_tail

    def test_browser_authoring_preserves_flow_bundled_demo_default(self, tmp_path: Path) -> None:
        assert ExecutionTarget(backend="web").record_args(tmp_path / "recording") == [
            "demo-record",
            "--out",
            str(tmp_path / "recording"),
        ]

    def test_browser_authoring_with_url_uses_exact_interactive_record_args(
        self, tmp_path: Path
    ) -> None:
        assert ExecutionTarget(
            backend="web",
            url="https://app.example",
        ).record_args(tmp_path / "recording") == [
            "record",
            "--out",
            str(tmp_path / "recording"),
            "--backend",
            "web",
            "--url",
            "https://app.example",
        ]

    def test_secret_flag_values_are_redacted_from_debug_command(self) -> None:
        rendered = _safe_command_for_log(
            ["openadapt-flow", "push", "--token", "oar_secret", "--kind", "bundle"]
        )
        assert "oar_secret" not in rendered
        assert rendered == "openadapt-flow push --token [REDACTED] --kind bundle"

    def test_host_is_redacted_from_debug_command(self) -> None:
        rendered = _safe_command_for_log(
            [
                "openadapt-flow",
                "push",
                "artifact",
                "--host",
                "https://customer-private.example",
            ]
        )
        assert "customer-private" not in rendered
        assert "--host [REDACTED]" in rendered

    def test_egress_local_paths_are_redacted_from_debug_command(self) -> None:
        rendered = _safe_command_for_log(
            [
                "openadapt-flow",
                "push",
                "/captures/Jane-Doe-12345.scrubbed",
                "--kind",
                "recording",
            ]
        )
        assert "Jane-Doe" not in rendered
        assert rendered == "openadapt-flow push [LOCAL_PATH] --kind recording"

    def test_phi_capable_selector_values_are_redacted_from_debug_command(self) -> None:
        rendered = _safe_command_for_log(
            [
                "openadapt-flow",
                "replay",
                "--rdp-window-title",
                "Patient Jane Doe",
                "--rdp-readiness-text",
                "MRN 12345",
            ]
        )
        assert "Jane Doe" not in rendered
        assert "12345" not in rendered

    def test_nonzero_returncode(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        bridge = FlowBridge(runner=_runner([], returncode=1, stderr="boom"))
        result = bridge.replay(tmp_path / "bundle")
        assert not result.ok
        assert result.stderr == "boom"

    def test_frozen_runtime_uses_own_executable_not_path(self, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge._is_frozen", lambda: True)
        monkeypatch.setattr("engine.flow_bridge.sys.executable", "/signed/openadapt-engine")
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/tmp/shadowed")
        calls: list = []

        FlowBridge(runner=_runner(calls)).replay(Path("bundle"))

        command, env = calls[0]
        assert command[:3] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "replay",
        ]
        assert env is not None
        assert flow_available()

    def test_optional_commands_use_same_bundled_runtime(self, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge._is_frozen", lambda: True)
        monkeypatch.setattr("engine.flow_bridge.sys.executable", "/signed/openadapt-engine")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="--json\nwf_123"))

        assert bridge.supports_command("push")
        result = bridge.push(
            Path("bundle"),
            kind="bundle",
            host="https://app.openadapt.ai",
        )

        assert result.ok
        commands = [command for command, _env in calls]
        assert commands[0] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "push",
            "--help",
        ]
        assert commands[1][:3] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "push",
        ]

    def test_push_refuses_before_upload_when_structured_result_is_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="legacy push help"))

        with pytest.raises(FlowNotAvailableError, match="structured push-result"):
            bridge.push(
                tmp_path / "bundle",
                kind="bundle",
                host="https://app.openadapt.ai",
            )

        assert [command[1:] for command, _env in calls] == [["push", "--help"]]


class TestReportParsing:
    def test_read_report_missing(self, tmp_path: Path) -> None:
        assert FlowBridge.read_report(tmp_path) == {}

    def test_read_halt_nested(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(
            json.dumps({"halt": {"reason": "ambiguous", "step_intent": "click X"}})
        )
        halt = FlowBridge.read_halt(tmp_path)
        assert halt is not None
        assert halt["reason"] == "ambiguous"

    def test_read_halt_top_level_status(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(json.dumps({"status": "halt", "reason": "drift"}))
        halt = FlowBridge.read_halt(tmp_path)
        assert halt is not None
        assert halt["reason"] == "drift"

    def test_read_halt_none_when_ok(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(json.dumps({"status": "ok"}))
        assert FlowBridge.read_halt(tmp_path) is None

    @pytest.mark.parametrize("payload", ["[]", '"success"', "true", "42", "null"])
    def test_non_object_json_report_is_explicit_unknown(self, tmp_path: Path, payload: str) -> None:
        (tmp_path / "report.json").write_text(payload)

        report = FlowBridge.read_report(tmp_path)

        assert report == {}
        assert FlowBridge.classify_outcome(0, report) == "unknown"

    @pytest.mark.parametrize(
        ("returncode", "report", "expected"),
        [
            (0, {"success": True}, "success"),
            (0, {"success": True, "terminal_outcome": "success"}, "success"),
            (1, {"success": False, "halt": {"outcome": "halt"}}, "halt"),
            (1, {"terminal_outcome": "halt"}, "halt"),
            (1, {"halt": {"outcome": "escalate"}}, "halt"),
            (1, {"success": True}, "unknown"),
            (0, {"success": False}, "unknown"),
            (0, {}, "unknown"),
            (2, {"results": []}, "unknown"),
            (0, {"success": True, "halt": {"outcome": "halt"}}, "unknown"),
            (0, {"success": "yes"}, "unknown"),
            (1, {"halt": "not-structured"}, "unknown"),
            (
                0,
                {
                    "success": True,
                    "execution_outcome": "VERIFIED",
                    "execution_profile": "demo",
                    "production_eligible": True,
                    "execution_completed": True,
                    "outcome_envelope": {
                        "version": "openadapt.execution-outcome/v1",
                        "outcome": "VERIFIED",
                        "profile": "demo",
                        "production_eligible": True,
                        "execution_completed": True,
                        "required_contracts": {
                            "authorization": 1,
                            "identity": 0,
                            "postcondition": 0,
                            "effect": 1,
                        },
                        "passed_contracts": {
                            "authorization": 1,
                            "identity": 0,
                            "postcondition": 0,
                            "effect": 0,
                        },
                        "evidence_classes": ["authorization"],
                        "model_calls": 0,
                        "external_network_calls": "none",
                        "compensation_actions": 0,
                    },
                },
                "unknown",
            ),
        ],
    )
    def test_classifies_only_explicit_consistent_flow_outcomes(
        self, returncode: int, report: dict, expected: str
    ) -> None:
        assert FlowBridge.classify_outcome(returncode, report) == expected

    def test_verified_outcome_rejects_mismatched_network_observation(self) -> None:
        report = {
            "success": True,
            "model_calls": 0,
            "external_network_calls": "none",
            "execution_outcome": "VERIFIED",
            "execution_profile": "standard",
            "production_eligible": True,
            "execution_completed": True,
            "outcome_envelope": {
                "version": "openadapt.execution-outcome/v1",
                "outcome": "VERIFIED",
                "profile": "standard",
                "production_eligible": True,
                "execution_completed": True,
                "required_contracts": {
                    "authorization": 1,
                    "identity": 0,
                    "postcondition": 0,
                    "effect": 0,
                },
                "passed_contracts": {
                    "authorization": 1,
                    "identity": 0,
                    "postcondition": 0,
                    "effect": 0,
                },
                "evidence_classes": ["authorization"],
                "model_calls": 0,
                "external_network_calls": "observed",
                "compensation_actions": 0,
            },
        }

        assert FlowBridge.classify_outcome(0, report) == "unknown"

    def test_accepts_hash_bound_postcondition_evidence_from_flow_1271(self) -> None:
        workflow_digest = "a" * 64
        step_payload = {
            "domain": "openadapt.postcondition-step/v1",
            "workflow_contract_sha256": workflow_digest,
            "step_index": 0,
            "action_kind": "type",
        }
        step_digest = hashlib.sha256(
            json.dumps(
                step_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        contract_payload = {
            "domain": "openadapt.postcondition-contract/v1",
            "workflow_contract_sha256": workflow_digest,
            "step_contract_sha256": step_digest,
            "action_kind": "type",
            "actuation_path": "gui",
            "contract_kind": "intrinsic_input_readback",
            "contract_index": 0,
        }
        contract_digest = hashlib.sha256(
            json.dumps(
                contract_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        report = {
            "success": True,
            "model_calls": 0,
            "external_network_calls": "none",
            "execution_outcome": "COMPLETED_UNVERIFIED",
            "execution_profile": "demo",
            "production_eligible": False,
            "execution_completed": True,
            "outcome_envelope": {
                "version": "openadapt.execution-outcome/v1",
                "outcome": "COMPLETED_UNVERIFIED",
                "profile": "demo",
                "production_eligible": False,
                "execution_completed": True,
                "required_contracts": {
                    "authorization": 0,
                    "identity": 0,
                    "postcondition": 1,
                    "effect": 0,
                },
                "passed_contracts": {
                    "authorization": 0,
                    "identity": 0,
                    "postcondition": 1,
                    "effect": 0,
                },
                "workflow_contract_sha256": workflow_digest,
                "postcondition_evidence": [
                    {
                        "result_index": 0,
                        "workflow_contract_sha256": workflow_digest,
                        "step_index": 0,
                        "step_contract_sha256": step_digest,
                        "action_kind": "type",
                        "actuation_path": "gui",
                        "contract_kind": "intrinsic_input_readback",
                        "contract_index": 0,
                        "contract_sha256": contract_digest,
                        "verdict": "passed",
                    }
                ],
                "evidence_classes": ["postcondition"],
                "model_calls": 0,
                "external_network_calls": "none",
                "compensation_actions": 0,
            },
        }

        assert FlowBridge.classify_outcome(0, report) == "COMPLETED_UNVERIFIED"

        report["outcome_envelope"]["postcondition_evidence"][0]["contract_sha256"] = "b" * 64
        assert FlowBridge.classify_outcome(0, report) == "unknown"


class TestBrowserRuntime:
    def test_provisions_once_and_reports_progress(self, monkeypatch) -> None:
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        presence = iter((False, True))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: next(presence))
        states: list[tuple[str, str]] = []

        bridge.ensure_browser_runtime(lambda state, detail: states.append((state, detail)))

        command, _ = calls[0]
        assert command[1:] == ["-m", "playwright", "install", "chromium"]
        assert [state for state, _ in states] == ["checking", "installing", "ready"]

    def test_existing_offline_prebundle_skips_install(self, monkeypatch) -> None:
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: True)
        states: list[str] = []

        bridge.ensure_browser_runtime(lambda state, _detail: states.append(state))

        assert calls == []
        assert states == ["checking", "ready"]

    def test_install_failure_is_explicit_and_retryable(self, monkeypatch) -> None:
        bridge = FlowBridge(runner=_runner([], returncode=1, stderr="network refused"))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: False)
        states: list[str] = []

        with pytest.raises(BrowserRuntimeError, match="select Replay to retry"):
            bridge.ensure_browser_runtime(lambda state, _detail: states.append(state))

        assert states == ["checking", "installing", "error"]
