"""Owned hosted Windows authoring and native configuration boundary proofs.

These four opt-in cases do not claim admitted compile-agent/Execute success.
Only fixed summaries and JUnit may be exported, never the fixture root.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.test_windows_hosted_replay import DEMO_SEARCH, TARGET_ID, _Host, _points

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32" or os.environ.get("OAFLOW_WINDOWS_HOSTED_E2E") != "1",
        reason="requires the explicitly enabled hosted Windows fixture",
    ),
    pytest.mark.timeout(180),
]


class _QuietHost(_Host):
    def db(self, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, str(self.root / "pn_db.py"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout


def _private_json(path: Path, value) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream)


def _foreground(host, backend) -> None:
    from openadapt_flow.backends.win32_window_client import Win32WindowClient

    app = host.app
    assert app is not None and app.poll() is None
    executable = shutil.which("powershell.exe")
    assert executable
    owner = Path(executable).stem
    client = Win32WindowClient()
    windows = [w for w in client.find_windows(owner, None) if w.pid == app.pid]
    assert len(windows) == 1
    target = windows[0]
    for before_activation in (True, False):
        fresh = client.find_pinned_window(owner, target.window_id)
        assert len(fresh) == 1 and fresh[0].pid == app.pid
        assert app.poll() is None
        if before_activation:
            client.activate(app.pid)
    assert client.key_window_id(app.pid) == target.window_id
    assert app.poll() is None
    identity = backend.qualification_environment_identity()
    assert identity is not None and identity[0] == "powershell"


def _safe_case(function):
    @wraps(function)
    def run(owned):
        try:
            function(owned)
            owned.summary["status"] = "PASS"
        except BaseException as error:
            owned.summary.update(status="FAIL", error_type=type(error).__name__)
            # Config/backend exceptions may contain a generated token. Never
            # expose their text, chained traceback, or raw CLI capture to JUnit.
            raise AssertionError(
                f"hosted authoring case failed ({type(error).__name__})"
            ) from None

    return run


@pytest.fixture
def owned(tmp_path, request, monkeypatch):
    from openadapt_flow.backends import factory
    from openadapt_flow.deployment import load_deployment

    root = tmp_path / "hosted-authoring"
    root.mkdir()
    summary = {
        "schema": "openadapt.hosted-authoring-proof/v1",
        "case": request.node.name,
        "status": "UNRUN",
        "cleanup": False,
        "admitted": False,
    }
    host = _QuietHost(root)
    backends = []
    original_factory = factory.build_backend

    def tracked_factory(*args, **kwargs):
        # Observe lifecycle only; invoke the actual factory with unchanged args.
        backend = original_factory(*args, **kwargs)
        backends.append(backend)
        return backend

    monkeypatch.setattr(factory, "build_backend", tracked_factory)
    try:
        try:
            prototype = host.start_agent()
            backends.append(prototype)
            host.launch_app()
            _points(prototype)
            _foreground(host, prototype)
            config = root / "deployment.yaml"
            _private_json(
                config,
                {
                    "backend": {
                        "kind": "windows",
                        "agent_url": prototype.server_url,
                        "agent_token": prototype._auth_token,
                        "agent_tls_pin": prototype._pin_fingerprint,
                    }
                },
            )
            backend = factory.build_backend(load_deployment(config).backend)
            _points(backend)
            _foreground(host, backend)
            yield SimpleNamespace(
                host=host,
                root=root,
                backend=backend,
                config=config,
                summary=summary,
                backends=backends,
            )
        except BaseException as error:
            summary.update(status="FAIL", error_type=type(error).__name__)
            raise AssertionError(
                f"hosted authoring setup failed ({type(error).__name__})"
            ) from None
    finally:
        try:
            try:
                for backend in backends:
                    backend._session.close()
            finally:
                host.close()
            assert host.app is None
            assert host.agent is None or host.agent.poll() is not None
            summary["cleanup"] = True
        except BaseException as error:
            summary.update(status="FAIL", error_type=type(error).__name__)
            raise AssertionError(
                f"owned authoring cleanup failed ({type(error).__name__})"
            ) from None
        finally:
            destination = Path(
                os.environ.get("OAFLOW_WINDOWS_AUTHORING_EVIDENCE_DIR", root)
            )
            destination.mkdir(parents=True, exist_ok=True)
            _private_json(destination / f"{request.node.name}.json", summary)


def _node(session, automation_id: str) -> str:
    observation = session.observe()
    assert observation["backend"] == "windows"
    assert observation["provider"] == "windows_uia"
    assert observation["agent_drive"] and not observation["truncated"]
    nodes = [n for n in observation["tree"] if n.get("automation_id") == automation_id]
    assert len(nodes) == 1
    return nodes[0]["node_id"]


def _rows(host):
    return {row["id"]: row for row in host.rows()}


@_safe_case
def test_windows_authoring_observed_controls_write_only_target(owned):
    from openadapt_flow.adapters.desktop_recorder import structural_armed_coverage
    from openadapt_flow.authoring import AuthoringSession
    from openadapt_flow.compiler import compile_recording

    note = "Synthetic hosted authoring note"
    before = _rows(owned.host)
    session = AuthoringSession(
        owned.backend, owned.root / "recording", backend_kind="windows"
    )
    try:
        session.start_record()
        session.click(node_id=_node(session, "searchBox"))
        session.type_text(DEMO_SEARCH, param="search")
        session.click(node_id=_node(session, "searchButton"))
        session.click(node_id=_node(session, "noteBox"))
        session.type_text(note, param="note")
        session.click(node_id=_node(session, "saveButton"))
        recording = session.stop_record()
        after = _rows(owned.host)
        assert set(before) == set(after)
        assert [key for key in before if before[key] != after[key]] == [TARGET_ID]
        assert after[TARGET_ID]["note"] == note
        expected = {key: dict(row) for key, row in before.items()}
        expected[TARGET_ID]["note"] = note
        assert after == expected
        events = [
            json.loads(line)
            for line in (recording / "events.jsonl").read_text().splitlines()
        ]
        assert [event["kind"] for event in events] == [
            "click",
            "type",
            "click",
            "click",
            "type",
            "click",
        ]
        assert all(event["actor"] == "agent" for event in events)
        assert {
            event["structural"]["automation_id"]
            for event in events
            if event["kind"] == "click"
        } == {"searchBox", "searchButton", "noteBox", "saveButton"}
        for index in range(len(events)):
            for phase in ("before", "after"):
                assert (
                    recording / "frames" / f"{index:04d}_{phase}.png"
                ).stat().st_size > 0
        workflow = compile_recording(
            recording, owned.root / "bundle", name="hosted-authoring"
        )
        coverage = structural_armed_coverage(workflow)
        assert coverage["armed_coverage"] == 1.0 and coverage["click_steps"] == 4
        owned.summary.update(
            recorded_actions=6,
            changed_rows=1,
            target_note_exact=True,
            native_structural_clicks=4,
        )
    finally:
        session.halt()


@_safe_case
def test_windows_authoring_replaced_window_refuses_old_node(owned):
    from openadapt_flow.authoring import AuthoringError, AuthoringSession

    session = AuthoringSession(
        owned.backend, owned.root / "recording", backend_kind="windows"
    )
    try:
        session.start_record()
        node = _node(session, "saveButton")
        original_process = owned.host.app
        owned.host.launch_app()
        assert original_process.poll() is not None
        assert owned.host.app.pid != original_process.pid
        _points(owned.backend)
        _foreground(owned.host, owned.backend)
        before = _rows(owned.host)
        with pytest.raises(AuthoringError) as refused:
            session.click(node_id=node)
        assert refused.value.code == "stale_node"
        recording = session.stop_record()
        assert _rows(owned.host) == before
        assert (recording / "events.jsonl").read_text() == ""
        owned.summary.update(refusal="stale_node", changed_rows=0, recorded_actions=0)
    finally:
        session.halt()


@_safe_case
def test_windows_compile_agent_config_reaches_native_hold_refusal(owned):
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.cli import cmd_compile_agent

    before = _rows(owned.host)
    verify = owned.root / "verify.yaml"
    _private_json(
        verify,
        {
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(owned.root / "patients.db"),
                "sql_query": "SELECT * FROM patients",
            }
        },
    )
    steps = owned.root / "steps.json"
    _private_json(
        steps,
        {"steps": [{"type": "{note}", "into": "noteBox"}, {"click": "saveButton"}]},
    )
    args = build_parser().parse_args(
        [
            "compile-agent",
            "Save {note}",
            "--config",
            str(owned.config),
            "--verify",
            str(verify),
            "--expect",
            f"id={TARGET_ID}",
            "--param",
            "note=Synthetic pending note",
            "--on-screen",
            "Synthetic hosted fixture",
            "--agent",
            str(steps),
            "--cache",
            str(owned.root / "compile-cache"),
            "--key-dir",
            str(owned.root / "compile-keys"),
            "--json",
        ]
    )
    opened_before = len(owned.backends)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(io.StringIO()):
        status = cmd_compile_agent(args, interactive=lambda: False)
    result = json.loads(capture.getvalue())
    assert status == 1 and result["status"] == "refused_wrong_record"
    assert len(owned.backends) > opened_before
    run = result["run1"]
    assert run["gates"][-1]["decision"] == "refused_no_hold"
    # ScriptedAgent stops at the real gate before returning its ledger.
    assert run["agent"] is None
    assert run["agent_error"].startswith("GateRefused:")
    assert run["harness"]["actions"] >= 2
    assert _rows(owned.host) == before
    owned.summary.update(refusal="refused_no_hold",
        factory_opened=True,
        changed_rows=0,
        scripted_driver="local_step_file",)


@_safe_case
def test_windows_execute_config_requires_real_qualification(owned):
    from openadapt_types.execute import ExecuteRequestV1

    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.authoring import AuthoringSession
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.execute.dispatch import DispatchError
    from openadapt_flow.execute.models import AdmittedBundle
    from openadapt_flow.execute.service import ExecuteService

    before = _rows(owned.host)
    session = AuthoringSession(
        owned.backend, owned.root / "recording", backend_kind="windows"
    )
    try:
        session.start_record()
        session.click(node_id=_node(session, "noteBox"))
        recording = session.stop_record()
        bundle = owned.root / "bundle"
        workflow = compile_recording(
            recording, bundle, name="uncertified-hosted-fixture"
        )
        digest = "sha256:" + workflow.manifest.content_digest.removeprefix("sha256:")
        request = ExecuteRequestV1(
            qualification_id="qualification_fixture",
            workflow_version="workflow_fixture",
            workflow_digest=digest,
            environment_id="environment_fixture",
            parameters={},
            idempotency_key="fixture_request_key",
            authorization_context={
                "actor_id": "fixture_operator",
                "authorization_reference": "fixture_request_scope",
            },
            effect_strength_schema_version="1",
            minimum_effect_strength="independent_system_of_record",
        )
        # An uncertified fixture descriptor, never a forged authority or registered
        # qualification. The real dispatcher must refuse it before spawning run.
        descriptor = AdmittedBundle(
            qualification_id=request.qualification_id,
            workflow_version=request.workflow_version,
            workflow_digest=digest,
            environment_id=request.environment_id,
            minimum_effect_strength=request.minimum_effect_strength.value,
            bundle_dir=str(bundle),
        )
        args = build_parser().parse_args(
            [
                "serve-execute",
                "--config",
                str(owned.config),
                "--data-dir",
                str(owned.root / "execute"),
            ]
        )
        service = ExecuteService(args.data_dir, deployment=args.config)
        with pytest.raises(DispatchError) as refused:
            service.runner(descriptor, request, owned.root / "execute-run")
        assert refused.value.pre_effect and "signed qualification" in str(refused.value)
        mismatch = owned.root / "wrong-deployment.yaml"
        _private_json(
            mismatch,
            {
                "backend": {
                    "kind": "linux",
                    "linux_app": "synthetic",
                    "linux_window_title": "synthetic",
                }
            },
        )
        wrong_service = ExecuteService(
            owned.root / "execute-wrong", deployment=mismatch
        )
        with pytest.raises(DispatchError) as wrong:
            wrong_service.runner(descriptor, request, owned.root / "execute-wrong-run")
        assert wrong.value.pre_effect and "deployment drives" in str(wrong.value)
        assert _rows(owned.host) == before
        owned.summary.update(
            refusal="qualification_authority_missing",
            mismatch_refused=True,
            changed_rows=0,
            governed_execution=False,
        )
    finally:
        session.halt()
