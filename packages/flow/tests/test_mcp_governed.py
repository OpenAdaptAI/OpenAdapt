"""One governed origin/surface model; no browser, keys or real qualification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import runpy
import subprocess
from pathlib import Path

import pytest

from openadapt_flow.emit import emit_mcp_server
from openadapt_flow.emit import mcp_governed as adapter
from openadapt_flow.ir import RegistryOrigin, Step, Workflow
from openadapt_flow.qualification import workflow_contract_sha256
from openadapt_flow.runtime.authorization import runtime_inputs_digest
from tests._mcp_report import bind_synthetic_invocation, synthetic_production_report
from tests._mcp_tools import call_generated_tool


def setup(tmp_path, surface="web", origin="local", profile="standard"):
    wf = Workflow(
        name="Synthetic MCP",
        surface=surface,
        params={"note": "recorded"},
        steps=[Step(id="type", intent="type note", action="type", param="note")],
    )
    if origin == "pulled":
        # Synthetic provenance is data, not a real signed pull or qualification.
        wf.registry = RegistryOrigin(
            id="synthetic/task",
            index_url="https://synthetic.invalid/index.json",
            entry_sha256="a" * 64,
            published_digest="b" * 64,
            pulled_at="2026-11-20T00:00:00Z",
        )
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    wf = Workflow.load(bundle)
    backend = {"kind": surface}
    backend.update(
        {
            "web": {"url": "https://synthetic.invalid/"},
            "windows": {"agent_url": "http://127.0.0.1:1"},
            "macos": {"macos_app": "Synthetic", "macos_window_title": "Synthetic"},
            "linux": {"linux_app": "Synthetic", "linux_window_title": "Synthetic"},
            "rdp": {"rdp_window_title": "Synthetic", "rdp_readiness_text": "Synthetic"},
            "citrix": {
                "rdp_window_title": "Synthetic",
                "rdp_readiness_text": "Synthetic",
            },
        }[surface]
    )
    config = tmp_path / "deploy.yaml"
    config.write_text(json.dumps({"backend": backend, "runtime": {"profile": profile}}))
    authority = tmp_path / "not-an-authority.json"
    authority.write_text('{"schema":"synthetic.transport-fixture-not-authority"}')
    options = dict(
        config=str(config),
        profile=profile,
        authority_file=str(authority),
        expected_bundle_digest=wf.manifest.content_digest,
        expected_workflow_contract=workflow_contract_sha256(wf),
        surface=surface,
    )
    return wf, bundle, options


def report_for(wf, params, profile="standard"):
    return synthetic_production_report(
        profile=profile,
        workflow_name=wf.name,
        bundle_content_digest=wf.manifest.content_digest,
        workflow_contract_sha256=workflow_contract_sha256(wf),
        governed_runtime_inputs_digest=runtime_inputs_digest(wf, params, {}),
        execution_target_kind=wf.surface,
        execution_origin="https://synthetic.invalid" if wf.surface == "web" else None,
        execution_entry_url="https://synthetic.invalid/"
        if wf.surface == "web"
        else None,
        params=params,
    )


def runner(monkeypatch, report, *, returncode=0, mutate=None):
    calls = []
    first_context = []

    def run(argv, **kwargs):
        calls.append(argv)
        params_path = Path(argv[argv.index("--params-file") + 1])
        params = json.loads(params_path.read_text())
        assert params_path.stat().st_mode & 0o077 == 0
        assert all(value not in argv for value in params.values())
        run_dir = Path(argv[argv.index("--run-dir") + 1])
        run_dir.mkdir(mode=0o700, exist_ok=True)
        value = (
            report.model_dump(mode="json") if hasattr(report, "model_dump") else report
        )
        if isinstance(value, dict):
            if not first_context:
                first_context.append(
                    bind_synthetic_invocation(
                        value, argv
                    ).invocation_context.model_dump(mode="json")
                )
            value["invocation_context"] = dict(first_context[0])
        if mutate:
            value = mutate(value)
        if value is not None:
            (run_dir / "report.json").write_text(
                value if isinstance(value, str) else json.dumps(value)
            )
        return subprocess.CompletedProcess(
            argv,
            returncode,
            "PRIVATE_STDOUT_NEVER_RETURN",
            "PRIVATE_STDERR_NEVER_RETURN",
        )

    monkeypatch.setattr(adapter.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "surface", ["web", "windows", "macos", "linux", "rdp", "citrix"]
)
@pytest.mark.parametrize("origin", ["local", "pulled"])
@pytest.mark.parametrize("profile", ["standard", "regulated"])
def test_every_origin_and_surface_uses_one_pinned_child(
    tmp_path, monkeypatch, surface, origin, profile
):
    wf, bundle, options = setup(tmp_path, surface, origin, profile)
    params = {"note": "SYNTHETIC_CALLER_VALUE"}
    calls = runner(monkeypatch, report_for(wf, params, profile))
    result = adapter.run_governed(bundle, params, **options)
    if profile == "regulated":
        assert result["outcome"] == "REFUSED" and calls == []
        return
    assert result["success"] is False and result["outcome"] == "UNCERTAIN"
    assert result["production_eligible"] is False and result["retry_allowed"] is False
    assert result["runtime_reported"]["outcome"] == "VERIFIED"
    assert result["producer_assurance"] == "LOCAL_OWNED_PROCESS_UNAUTHENTICATED"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[1:5] == ["-m", "openadapt_flow", "run", str(bundle)]
    for key, value in {
        "--profile": profile,
        "--config": options["config"],
        "--qualification-authority-file": options["authority_file"],
        "--pin-digest": options["expected_bundle_digest"],
        "--config-sha256": hashlib.sha256(
            Path(options["config"]).read_bytes()
        ).hexdigest(),
    }.items():
        assert argv[argv.index(key) + 1] == value
    assert "--url" not in argv
    assert not Path(argv[argv.index("--params-file") + 1]).exists()
    assert Path(result["run_dir"]).parent.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "fault",
    [
        "config",
        "profile",
        "demo",
        "authority",
        "missing_authority",
        "override",
        "surface",
        "profile_mismatch",
        "bundle",
        "contract",
    ],
)
def test_prelaunch_refusals_never_start_child(tmp_path, monkeypatch, fault):
    wf, bundle, options = setup(tmp_path)
    if fault in {"config", "profile", "authority"}:
        options[{"authority": "authority_file"}.get(fault, fault)] = None
    elif fault == "demo":
        options["profile"] = "demo"
    elif fault == "missing_authority":
        options["authority_file"] += ".missing"
    elif fault == "override":
        options["legacy_url"] = "https://other.invalid/"
    elif fault == "surface":
        options["surface"] = "windows"
    elif fault == "profile_mismatch":
        options["profile"] = "regulated"
    elif fault == "bundle":
        options["expected_bundle_digest"] = "0" * 64
    elif fault == "contract":
        options["expected_workflow_contract"] = "0" * 64
    monkeypatch.setattr(
        adapter.subprocess, "run", lambda *_a, **_k: pytest.fail("no child may start")
    )
    result = adapter.run_governed(bundle, {"note": "private"}, **options)
    assert result["outcome"] == "REFUSED" and not result["success"]
    assert result["transaction_outcome"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "sparse",
        "duplicate",
        "nonfinite",
        "overflow",
        "nonzero",
        "wrong_profile",
        "wrong_bundle",
        "wrong_semantics",
        "wrong_envelope_semantics",
        "wrong_inputs",
        "wrong_target",
        "wrong_origin",
        "wrong_entry",
        "no_envelope",
        "no_authorization",
        "qualification_only",
        "coerced_bool",
    ],
)
def test_postlaunch_failures_preserve_uncertainty(tmp_path, monkeypatch, fault):
    wf, bundle, options = setup(tmp_path)
    params = {"note": "SYNTHETIC_PRIVATE_VALUE"}

    def mutate(value):
        if fault == "missing":
            return None
        if fault == "malformed":
            return "not json PRIVATE_ERROR_VALUE"
        if fault == "sparse":
            return {
                "execution_outcome": "VERIFIED",
                "transaction_outcome": "VERIFIED",
                "production_eligible": True,
            }
        if fault == "duplicate":
            return '{"success":false,' + json.dumps(value)[1:]
        if fault in {"nonfinite", "overflow"}:
            return (
                '{"unknown":'
                + ("NaN" if fault == "nonfinite" else "1e9999")
                + ","
                + json.dumps(value)[1:]
            )
        keys = {
            "wrong_profile": ("execution_profile", "regulated"),
            "wrong_bundle": ("bundle_content_digest", "0" * 64),
            "wrong_semantics": ("workflow_contract_sha256", "0" * 64),
            "wrong_inputs": ("governed_runtime_inputs_digest", "0" * 64),
            "wrong_target": ("execution_target_kind", "windows"),
            "wrong_origin": ("execution_origin", "https://other.invalid"),
            "wrong_entry": ("execution_entry_url", "https://other.invalid/"),
            "no_envelope": ("outcome_envelope", None),
            "no_authorization": ("governed_authorization_id", None),
            "coerced_bool": ("success", "true"),
        }
        if fault in keys:
            key, new = keys[fault]
            value[key] = new
        if fault == "wrong_envelope_semantics":
            value["outcome_envelope"]["workflow_contract_sha256"] = "0" * 64
        if fault == "qualification_only":
            value["governed_qualification_case_id_sha256"] = "d" * 64
            value["qualification_evidence_only"] = True
            value["outcome_envelope"]["qualification_evidence_only"] = True
        return value

    calls = runner(
        monkeypatch,
        report_for(wf, params),
        returncode=1 if fault == "nonzero" else 0,
        mutate=mutate,
    )
    result = adapter.run_governed(bundle, params, **options)
    assert len(calls) == 1
    assert result["success"] is False and result["production_eligible"] is False
    assert result["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    assert result["outcome"] == "UNCERTAIN"
    text = json.dumps(result)
    assert "PRIVATE" not in text and "nothing ran" not in text.lower()


@pytest.mark.parametrize("origin", ["local", "pulled"])
def test_stateful_params_and_target_changes_cannot_reuse_old_completion(
    tmp_path, monkeypatch, origin
):
    wf, bundle, options = setup(tmp_path, origin=origin)
    first = {"note": "first"}
    calls = runner(monkeypatch, report_for(wf, first))
    assert adapter.run_governed(bundle, first, **options)["outcome"] == "UNCERTAIN"
    assert (
        adapter.run_governed(bundle, {"note": "second"}, **options)[
            "transaction_outcome"
        ]
        == "RECONCILIATION_REQUIRED"
    )
    config = Path(options["config"])
    changed = json.loads(config.read_text())
    changed["backend"]["url"] = "https://other.invalid/"
    config.write_text(json.dumps(changed))
    assert (
        adapter.run_governed(bundle, first, **options)["transaction_outcome"]
        == "RECONCILIATION_REQUIRED"
    )
    assert len({argv[argv.index("--run-dir") + 1] for argv in calls}) == 3
    assert (
        calls[0][calls[0].index("--config-sha256") + 1]
        != calls[2][calls[2].index("--config-sha256") + 1]
    )
    changed["runtime"]["profile"] = "regulated"
    config.write_text(json.dumps(changed))
    assert adapter.run_governed(bundle, first, **options)["outcome"] == "REFUSED"
    assert len(calls) == 3
    changed["runtime"]["profile"] = "standard"
    changed["backend"]["url"] = "https://synthetic.invalid/"
    config.write_text(json.dumps(changed))
    assert adapter.run_governed(bundle, first, **options)["runtime_reported"] is None
    assert len(calls) == 4


def test_launch_exception_is_uncertain(tmp_path, monkeypatch):
    _wf, bundle, options = setup(tmp_path)

    def failure(*_args, **_kwargs):
        raise OSError("private synthetic process error")

    monkeypatch.setattr(adapter.subprocess, "run", failure)
    result = adapter.run_governed(bundle, {"note": "caller"}, **options)
    assert (
        result["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        and not result["success"]
    )
    assert "private synthetic" not in json.dumps(result)


@pytest.mark.parametrize("surface", ["web", "windows"])
def test_genuine_sdk_legacy_url_refuses_before_child_launch(
    tmp_path, monkeypatch, surface
):
    wf, bundle, options = setup(tmp_path, surface)
    emitted = emit_mcp_server(bundle, tmp_path / "server.py")
    namespace = runpy.run_path(str(emitted))
    fn = namespace["run_synthetic_mcp"]
    fn.__globals__.update(
        DEPLOYMENT=options["config"],
        PROFILE=options["profile"],
        AUTHORITY_FILE=options["authority_file"],
    )
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("override must never launch"),
    )
    result = asyncio.run(
        call_generated_tool(
            namespace, {"note": "data", "url": "https://other.invalid/"}
        )
    )
    assert result["outcome"] == "REFUSED" and not result["success"]
    text = emitted.read_text()
    assert "Replayer" not in text and "playwright" not in text


@pytest.mark.parametrize("origin", ["local", "pulled"])
def test_actual_governed_cli_refuses_unqualified_before_backend(
    tmp_path, monkeypatch, origin
):
    from openadapt_flow import __main__ as cli

    _wf, bundle, options = setup(tmp_path, origin=origin)
    # No fake positive authority: intentionally omit it at the actual CLI.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(
        cli,
        "_cmd_replay",
        lambda *_a, **_k: pytest.fail("qualification must precede backend"),
    )
    try:
        result = cli.main(
            [
                "run",
                str(bundle),
                "--config",
                options["config"],
                "--profile",
                "standard",
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
    except SystemExit as exc:
        assert "REFUSED" in str(exc) or exc.code == 2
    else:
        assert result == 2


def test_real_cli_config_pin_refuses_resolved_target_substitution(
    tmp_path, monkeypatch
):
    from openadapt_flow import __main__ as cli

    _wf, bundle, options = setup(tmp_path)
    config = Path(options["config"])
    original_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    data = json.loads(config.read_text())
    data["backend"]["url"] = "https://other.invalid/"
    config.write_text(json.dumps(data))

    def forbidden(*_args, **_kwargs):
        pytest.fail("changed deployment must refuse before workflow load or delivery")

    monkeypatch.setattr(Workflow, "load", forbidden)
    monkeypatch.setattr(cli, "_cmd_replay", forbidden)
    assert (
        cli.main(
            [
                "run",
                str(bundle),
                "--config",
                str(config),
                "--config-sha256",
                original_hash,
                "--profile",
                "standard",
            ]
        )
        == 2
    )


@pytest.mark.parametrize("failure", ["sealed", "oversized", "symlink"])
def test_unreadable_report_cannot_fall_back_to_success(tmp_path, monkeypatch, failure):
    from openadapt_flow import artifacts

    wf, bundle, options = setup(tmp_path)
    params = {"note": "value"}
    calls = runner(monkeypatch, report_for(wf, params))
    if failure == "oversized":
        monkeypatch.setattr(adapter, "_MAX_REPORT_BYTES", 10)
    elif failure == "sealed":
        monkeypatch.setattr(artifacts, "is_sealed", lambda _p: True)

        def unavailable(*_args):
            raise OSError("sealed reader unavailable; must not try plaintext")

        monkeypatch.setattr(artifacts, "open_read", unavailable)
    else:
        read = adapter._report_bytes

        def replace(run, identity):
            path = run / "report.json"
            path.rename(run / "untrusted-other-report.json")
            path.symlink_to(run / "untrusted-other-report.json")
            return read(run, identity)

        monkeypatch.setattr(adapter, "_report_bytes", replace)
    result = adapter.run_governed(bundle, params, **options)
    assert (
        len(calls) == 1 and result["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    )
    assert not result["success"]


def test_report_link_refuses_when_nofollow_flag_is_unavailable(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "other.json").write_text('{"synthetic":"not authority"}')
    (run / "report.json").symlink_to(run / "other.json")
    monkeypatch.delattr(adapter.os, "O_NOFOLLOW", raising=False)
    with pytest.raises((OSError, ValueError)):
        adapter._report_bytes(run)


def test_report_directory_link_is_not_a_new_observation(tmp_path):
    other = tmp_path / "old-run"
    other.mkdir()
    (other / "report.json").write_text('{"synthetic":"old observation"}')
    run = tmp_path / "new-run"
    run.symlink_to(other, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        adapter._report_bytes(run)


def test_replaced_child_output_directory_cannot_borrow_completion(
    tmp_path, monkeypatch
):
    wf, bundle, options = setup(tmp_path)
    params = {"note": "same inputs"}
    runner(monkeypatch, report_for(wf, params))
    original = adapter.subprocess.run

    def replacing(argv, **kwargs):
        proc = original(argv, **kwargs)
        run = Path(argv[argv.index("--run-dir") + 1])
        old = run.with_name("old-incarnation")
        run.rename(old)
        run.mkdir()
        (run / "report.json").write_bytes((old / "report.json").read_bytes())
        return proc

    monkeypatch.setattr(adapter.subprocess, "run", replacing)
    result = adapter.run_governed(bundle, params, **options)
    assert (
        not result["success"]
        and result["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    )


def test_report_replaced_between_census_and_open_refuses(tmp_path, monkeypatch):
    from openadapt_flow import private_file

    run = tmp_path / "run"
    run.mkdir()
    report = run / "report.json"
    report.write_text('{"synthetic":"old"}')
    old = private_file.open_binary

    def replacing(path, *args, **kwargs):
        report.rename(run / "old.json")
        report.write_text('{"synthetic":"replacement"}')
        return old(path, *args, **kwargs)

    monkeypatch.setattr(private_file, "open_binary", replacing)
    with pytest.raises(ValueError):
        adapter._report_bytes(run)


@pytest.mark.parametrize("private", [False, True])
def test_existing_windows_private_file_contract_precedes_param_delivery(
    tmp_path, monkeypatch, private
):
    from openadapt_flow import private_file

    wf, bundle, options = setup(tmp_path)
    params = {"note": "synthetic data"}
    calls = runner(monkeypatch, report_for(wf, params))
    inspected = []

    def check(fd):
        inspected.append(fd)
        return private

    monkeypatch.setattr(private_file, "windows_descriptor_has_private_acl", check)
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    result = adapter.run_governed(bundle, params, **options)
    assert len(inspected) == 1
    assert len(calls) == int(private)
    assert result["success"] is False
    assert result["outcome"] == ("UNCERTAIN" if private else "REFUSED")


def test_unknown_directory_identity_is_not_accepted(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace

    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "lstat",
            lambda _self: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=0),
        )
        with pytest.raises(ValueError):
            adapter._directory_identity(tmp_path)
