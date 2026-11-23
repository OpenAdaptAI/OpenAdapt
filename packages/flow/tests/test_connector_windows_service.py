"""The Windows service wrapper's OS-independent parts.

The pywin32 service itself runs only in .github/workflows/connector-windows-service.yml
on a GitHub-hosted Windows runner. These tests cover what runs everywhere: the
CLI dispatch, the config and log paths, token redaction, the stop hook around
the unmodified ``run_loop``, and the stub control plane the Windows job uses.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from openadapt_flow.__main__ import build_parser
from openadapt_flow.connector import windows_service as ws

STUB = Path(__file__).parent / "connector_stub" / "stub_server.py"
TOKEN = "tok_test_connector_service_0123456789"


def _load_stub():
    spec = importlib.util.spec_from_file_location("stub_server", STUB)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(
    config_dir: Path, url: str, *, token: str = TOKEN, **extra: object
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "[connector]",
        f'control_plane_url = "{url}"',
        f'token = "{token}"',
        'org_id = "org_stub"',
        "poll_wait_s = 1",
    ]
    profile = config_dir / "deployment.yaml"
    profile.write_text("runtime: {profile: standard}\n", encoding="utf-8")
    lines.append(f"profile = {json.dumps(str(profile))}")
    lines.append(f"storage_root = {json.dumps(str(config_dir / 'storage'))}")
    lines += [f"{k} = {json.dumps(v)}" for k, v in extra.items()]
    (config_dir / "connector.toml").write_text("\n".join(lines) + "\n", "utf-8")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # serve() sets OPENADAPT_HOME; setenv first so teardown restores it.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    for key in (
        "CONTROL_PLANE_URL",
        "BYOC_CONNECTOR_TOKEN",
        "BYOC_ORG_ID",
        "BYOC_POLL_WAIT_S",
        "BYOC_PROFILE",
        "BYOC_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)


# --- CLI dispatch -------------------------------------------------------------
@pytest.mark.parametrize("action", ws.SERVICE_ACTIONS)
def test_cli_parses_every_service_action(action: str) -> None:
    parser = build_parser()
    args = parser.parse_args(["connector", "service", action])
    assert args.func is ws.cmd_connector_service
    assert args.service_action == action
    assert args.config_dir is None
    explicit = parser.parse_args(
        [
            "connector",
            "service",
            action,
            "--config-dir",
            "own config path",
            "--timeout",
            "7.5",
        ]
    )
    assert explicit.func is ws.cmd_connector_service
    assert explicit.service_action == action
    assert explicit.config_dir == "own config path"
    assert explicit.timeout == 7.5


@pytest.mark.parametrize("verb", ["enroll", "run"])
def test_legacy_connector_parser_keeps_flags_and_dispatch(verb: str) -> None:
    from openadapt_flow.__main__ import _cmd_connector

    shared = [
        "--control-plane-url",
        "https://cp.example",
        "--org-id",
        "own-org",
        "--profile",
        "own-profile.yaml",
        "--policy",
        "own-policy",
        "--storage-backend",
        "local",
        "--storage-root",
        "own storage",
    ]
    extra = (
        ["--enrollment-secret", "own-synthetic-secret", "--name", "own connector"]
        if verb == "enroll"
        else ["--token", "own-synthetic-token", "--poll-wait", "7", "--once"]
    )
    args = build_parser().parse_args(["connector", verb, *shared, *extra])
    assert args.func is _cmd_connector
    assert args.connector_cmd == verb
    assert args.control_plane_url == "https://cp.example"
    assert args.org_id == "own-org"
    assert args.profile == "own-profile.yaml"
    assert args.policy == "own-policy"
    assert args.storage_backend == "local"
    assert args.storage_root == "own storage"
    if verb == "enroll":
        assert args.enrollment_secret == "own-synthetic-secret"
        assert args.name == "own connector"
    else:
        assert args.token == "own-synthetic-token"
        assert args.poll_wait == 7
        assert args.once is True


def test_cli_rejects_an_unknown_action() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["connector", "service", "restart"])


@pytest.mark.parametrize("action", ws.SERVICE_ACTIONS)
def test_service_commands_exit_2_off_windows(
    action: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ws, "is_windows", lambda: False)
    args = build_parser().parse_args(["connector", "service", action])
    assert args.func(args) == ws.EXIT_NOT_WINDOWS
    err = capsys.readouterr().err
    assert "only available on Windows" in err
    assert "connector run" in err


def test_run_verb_still_dispatches_to_the_daemon() -> None:
    from openadapt_flow.__main__ import _cmd_connector

    args = build_parser().parse_args(["connector", "run", "--once"])
    assert args.func is _cmd_connector


# --- Paths --------------------------------------------------------------------
def test_default_config_dir_is_under_program_data() -> None:
    env = {"ProgramData": "D:\\PD"}
    assert ws.default_config_dir(env) == Path("D:\\PD") / "OpenAdapt" / "connector"
    assert ws.default_config_dir({}) == (
        Path("C:\\ProgramData") / "OpenAdapt" / "connector"
    )


def test_config_dir_flag_wins_and_paths_derive_from_it(tmp_path: Path) -> None:
    config = ws.resolve_config_dir(str(tmp_path), {"ProgramData": "X"})
    assert config == tmp_path
    assert ws.config_file(config) == tmp_path / "connector.toml"
    assert ws.log_path(config) == tmp_path / "logs" / "connector.log"


def test_service_command_line_quotes_paths(tmp_path: Path) -> None:
    line = ws.service_command_line("C:\\Py 312\\python.exe", tmp_path / "a b")
    assert line.startswith('"C:\\Py 312\\python.exe" -m ')
    assert ws.SERVICE_MODULE in line and ws.HOST_FLAG in line
    assert f'--config-dir "{tmp_path / "a b"}"' in line


def test_install_refuses_a_venv_interpreter() -> None:
    with pytest.raises(RuntimeError, match="virtual environment"):
        ws.service_python(
            "C:/venv/Scripts/python.exe", prefix="C:/venv", base_prefix="C:/Py"
        )
    assert (
        ws.service_python("C:/Py/python.exe", prefix="C:/Py", base_prefix="C:/Py")
        == "C:/Py/python.exe"
    )


def test_children_inherit_the_protected_acl_and_only_logs_get_write_access() -> None:
    config = Path("C:/cfg")
    commands = ws.acl_commands(config)
    assert commands[0] == [
        "icacls",
        str(config / "*"),
        "/setowner",
        "*S-1-5-32-544",
        "/T",
        "/Q",
    ]
    assert commands[1] == ["icacls", str(config / "*"), "/reset", "/T", "/C", "/Q"]
    assert commands[2] == [
        "icacls",
        str(config / "logs"),
        "/grant",
        "NT SERVICE\\OpenAdaptConnector:(OI)(CI)M",
    ]
    flat = " ".join(" ".join(c) for c in commands)
    assert "Users" not in flat and "Everyone" not in flat


def test_recovery_restarts_and_never_touches_the_firewall() -> None:
    commands = ws.recovery_commands()
    assert commands[0][:3] == ["sc.exe", "failure", ws.SERVICE_NAME]
    assert commands[1] == ["sc.exe", "failureflag", ws.SERVICE_NAME, "1"]
    source = Path(ws.__file__).read_text("utf-8").lower()
    assert "netsh" not in source and "new-netfirewallrule" not in source


# --- Logging ------------------------------------------------------------------
def test_log_rotates_and_never_contains_the_token(tmp_path: Path) -> None:
    logger, redactor = ws.build_logger(tmp_path, name="test.ws.redact")
    redactor.add(TOKEN)
    logger.info("settings token=%s", TOKEN)
    logger.info(f"inline {TOKEN} end")
    for handler in logger.handlers:
        handler.flush()
    handler = logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == ws.LOG_MAX_BYTES
    assert handler.backupCount == ws.LOG_BACKUP_COUNT
    text = ws.log_path(tmp_path).read_text("utf-8")
    assert TOKEN not in text
    assert text.count("<redacted>") == 2


# --- The service body over the real run_loop ------------------------------------
def _refusing_runner(*_args, **_kwargs):  # pragma: no cover - must not run
    raise AssertionError("an ungoverned job reached the governed runner")


def test_serve_refuses_the_stub_job_then_stops_on_the_event(tmp_path: Path) -> None:
    stub = _load_stub()
    log = tmp_path / "requests.json"
    server, _state = stub.serve(log, empty_polls=1, empty_sleep=0.05)
    config_dir = tmp_path / "connector"
    _write_config(config_dir, f"http://127.0.0.1:{server.server_address[1]}")
    logger, redactor = ws.build_logger(config_dir, name="test.ws.serve")
    stop = threading.Event()
    result: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "code",
            ws.serve(
                config_dir, stop, logger, redactor=redactor, runner=_refusing_runner
            ),
        )
    )
    try:
        thread.start()
        deadline = time.monotonic() + 30
        paths: list[str] = []
        while time.monotonic() < deadline:
            paths = [r["path"] for r in json.loads(log.read_text("utf-8"))]
            if "/api/connector/ack" in paths:
                break
            time.sleep(0.1)
        stop.set()
        thread.join(timeout=15)
    finally:
        stop.set()
        server.shutdown()
    assert not thread.is_alive()
    assert result["code"] == 0
    assert (
        paths.index("/api/connector/poll")
        < paths.index("/api/internal/run-callback")
        < paths.index("/api/connector/ack")
    )
    records = json.loads(log.read_text("utf-8"))
    callback = next(r for r in records if r["path"] == "/api/internal/run-callback")
    assert callback["body"]["status"] == "failed"
    assert callback["body"]["run_id"] == stub.RUN_ID
    ack = next(r for r in records if r["path"] == "/api/connector/ack")
    assert ack["body"]["job_id"] == stub.JOB_ID
    assert ack["body"]["status"] == "failed"
    assert ack["body"]["error_code"] == "governance_refused"
    assert "error" not in ack["body"]
    assert TOKEN not in log.read_text("utf-8")
    text = ws.log_path(config_dir).read_text("utf-8")
    assert TOKEN not in text and "service stopped" in text


def test_serve_retries_after_a_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws, "_RETRY_START_S", 0.05)
    calls = {"n": 0}
    stop = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        if calls["n"] >= 3:
            stop.set()
        return httpx.Response(204)

    config_dir = tmp_path / "connector"
    _write_config(config_dir, "https://cp.example")
    logger, _ = ws.build_logger(config_dir, name="test.ws.retry")
    code = ws.serve(
        config_dir,
        stop,
        logger,
        runner=_refusing_runner,
        client_kwargs={"transport": httpx.MockTransport(handler)},
    )
    assert code == 0
    assert calls["n"] >= 3
    assert "ConnectError" in ws.log_path(config_dir).read_text("utf-8")


def test_serve_without_a_token_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BYOC_CONNECTOR_TOKEN", raising=False)
    config_dir = tmp_path / "connector"
    config_dir.mkdir()
    (config_dir / "connector.toml").write_text(
        '[connector]\ncontrol_plane_url = "https://cp.example"\n', "utf-8"
    )
    logger, _ = ws.build_logger(config_dir, name="test.ws.notoken")
    assert ws.serve(config_dir, threading.Event(), logger) == 2
    assert "no connector token" in ws.log_path(config_dir).read_text("utf-8")


@pytest.mark.parametrize("reply", [None, {"id": "leased-after-stop", "payload": {}}])
def test_stop_during_poll_never_returns_a_new_lease(reply):
    stop = threading.Event()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        stop.set()
        return (
            httpx.Response(204)
            if reply is None
            else httpx.Response(200, json={"job": reply})
        )

    with ws._stoppable_client(
        "https://cp.example", TOKEN, stop, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(KeyboardInterrupt):
            client.poll(1)
        with pytest.raises(KeyboardInterrupt):
            client.poll(1)
    assert calls == ["/api/connector/poll"]


def test_log_redacts_tokens_in_exception_and_stack_text(tmp_path):
    logger, redactor = ws.build_logger(tmp_path, name="test.ws.exception")
    redactor.add(TOKEN)
    try:
        raise RuntimeError("server echoed " + TOKEN)
    except RuntimeError:
        logger.exception("service failure", stack_info=True)
    for handler in logger.handlers:
        handler.flush()
    text = ws.log_path(tmp_path).read_text()
    assert TOKEN not in text
    assert "RuntimeError: server echoed <redacted>" in text


@pytest.mark.parametrize("token_length", [1, 64, 301, 1024])
def test_auth_rejection_is_retried_without_execution_or_token_in_logs(
    tmp_path, monkeypatch, token_length
):
    monkeypatch.setattr(ws, "_RETRY_START_S", 0.001)
    token = "Q" * token_length
    stop = threading.Event()
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 2:
            stop.set()
        return httpx.Response(401, json={"error": "Bearer " + token})

    config = tmp_path / "connector"
    _write_config(config, "https://cp.example", token=token)
    logger, redactor = ws.build_logger(config, name="test.ws.revoked")
    assert (
        ws.serve(
            config,
            stop,
            logger,
            redactor=redactor,
            runner=_refusing_runner,
            client_kwargs={"transport": httpx.MockTransport(handler)},
        )
        == 0
    )
    assert requests == ["/api/connector/poll", "/api/connector/poll"]
    text = ws.log_path(config).read_text()
    assert token not in text
    assert token[:24] not in text
    assert "ConnectorClientError" in text


def test_stop_during_an_existing_job_reports_and_acks_before_exit(
    tmp_path, monkeypatch
):
    import hashlib
    import zipfile

    from openadapt_flow.connector.executor import RunOutcome

    config = tmp_path / "connector"
    # One worker: the service must not lease a second job while the first runs.
    _write_config(config, "https://cp.example", max_workers=1)
    bundle = config / "storage/bundles/wf_stub/bundle.zip"
    bundle.parent.mkdir(parents=True)
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("workflow.json", "{}")
    envelope = _load_stub().ungoverned_job()
    envelope["payload"].update(
        {
            "target_kind": "web",
            "safety": {"halt.on_ambiguous": True},
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
    )
    stop = threading.Event()
    requests = []
    runners = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/api/connector/poll":
            return httpx.Response(200, json={"job": envelope})
        return httpx.Response(200, json={"ok": True})

    def runner(*args):
        runners.append(True)
        stop.set()
        return RunOutcome(
            0, {"execution_target_kind": "web", "success": True, "model_calls": 0}
        )

    from openadapt_flow.connector import daemon
    from openadapt_flow.connector.executor import ExecutionResult

    def execute_job(job, settings, storage, *, runner, **_kwargs):
        # Managed authority is covered by the connector contract tests. Only
        # execution is stood in here: the production handle_job still builds
        # the callback and ack, so the stop control runs through real
        # report/ack code while a job is in flight.
        runner()
        return ExecutionResult("success", {}, None, job.report_ref())

    monkeypatch.setattr(daemon, "execute_job", execute_job)
    logger, redactor = ws.build_logger(config, name="test.ws.finish")
    assert (
        ws.serve(
            config,
            stop,
            logger,
            redactor=redactor,
            runner=runner,
            client_kwargs={"transport": httpx.MockTransport(handler)},
        )
        == 0
    )
    assert len(runners) == 1
    assert requests == [
        "/api/connector/poll",
        "/api/internal/run-callback",
        "/api/connector/ack",
    ]


@pytest.mark.parametrize("action", ["start", "stop", "remove"])
@pytest.mark.parametrize("reaches_state", [False, True])
def test_lifecycle_timeout_never_reports_success_or_removes_running_service(
    action, reaches_state, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(ws, "_require_pywin32", lambda: None)
    monkeypatch.setattr(ws, "_service_pid", lambda: 42)
    monkeypatch.setattr(
        ws,
        "win32service",
        SimpleNamespace(SERVICE_STOPPED=1, SERVICE_RUNNING=4),
        raising=False,
    )
    monkeypatch.setattr(
        ws,
        "win32serviceutil",
        SimpleNamespace(
            QueryServiceStatus=lambda _name: (None, 4),
            StartService=lambda _name: calls.append("start"),
            StopService=lambda _name: calls.append("stop"),
            RemoveService=lambda _name: calls.append("remove"),
        ),
    )
    monkeypatch.setattr(ws, "_wait_for", lambda *_args: reaches_state)

    assert ws._act(action, tmp_path, 0.01) == (0 if reaches_state else 1)
    assert ("remove" in calls) == (action == "remove" and reaches_state)
