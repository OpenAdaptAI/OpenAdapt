"""Service rotation with synthetic credentials and a process-local WinCred model."""

import hashlib
import json
import os
import threading
from unittest.mock import Mock

import httpx
import pytest

from openadapt_flow.connector import windows_service as ws
from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.windows_tokens import ServiceTokenRotation

BOOTSTRAP = "service-test-bootstrap"
CANDIDATE = "oaconn_" + "a" * 64


class CredentialNotFound(Exception):
    winerror = 1168


class CredentialSet:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self):
        self.entry = None

    def CredRead(self, target, kind):
        if self.entry is None:
            raise CredentialNotFound()
        assert self.entry["TargetName"] == target and self.entry["Type"] == kind
        return self.entry

    def CredWrite(self, record):
        assert record["Persist"] == self.CRED_PERSIST_LOCAL_MACHINE
        self.entry = {
            **record,
            "CredentialBlob": record["CredentialBlob"].encode("utf-16-le"),
        }

    def CredDelete(self, target, kind):
        self.CredRead(target, kind)
        self.entry = None


@pytest.fixture(autouse=True)
def service_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    profile = tmp_path / "deployment.yaml"
    profile.write_text("runtime: {profile: standard}\n", encoding="utf-8")
    monkeypatch.setenv("BYOC_PROFILE", str(profile))
    for name in ("BYOC_CONNECTOR_TOKEN", "BYOC_ORG_ID", "CONTROL_PLANE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_service_recovers_same_candidate_before_polling(tmp_path, monkeypatch):
    class SimulatedCrash(BaseException):
        pass

    config = tmp_path / "connector.toml"
    original = (
        f'[connector]\ncontrol_plane_url = "https://cloud.example"\ntoken = "{BOOTSTRAP}"\n'
    ).encode()
    config.write_bytes(original)
    monkeypatch.setattr("openadapt_flow.connector.client.mint_token", lambda: CANDIDATE)
    store = CredentialSet()
    logger, redactor = ws.build_logger(tmp_path, name="test.service-rotation")
    stop = threading.Event()
    paths = []
    digests = []

    def transport(request):
        path = request.url.path
        paths.append(path)
        if path.endswith("/rotate"):
            assert request.headers["Authorization"] == "Bearer " + BOOTSTRAP
            digests.append(json.loads(request.content)["new_token_sha256"])
            assert json.loads(store.entry["CredentialBlob"].decode("utf-16-le")) == [
                None,
                CANDIDATE,
            ]
            if len(digests) == 1:
                raise SimulatedCrash
            return httpx.Response(200, json={"already_rotated": True})
        if len(paths) == 1:
            return httpx.Response(200, json={"connector": {"rotate_recommended": True}})
        assert request.headers["Authorization"] == "Bearer " + CANDIDATE
        stop.set()
        return httpx.Response(204)

    def run():
        return ws.serve(
            tmp_path,
            stop,
            logger,
            redactor=redactor,
            credential_api=store,
            client_kwargs={"transport": httpx.MockTransport(transport)},
        )

    with pytest.raises(SimulatedCrash):
        run()
    assert paths == ["/api/connector/poll", "/api/connector/rotate"]
    assert run() == 0
    assert paths[2:] == ["/api/connector/rotate", "/api/connector/poll"]
    assert digests == [hashlib.sha256(CANDIDATE.encode()).hexdigest()] * 2
    assert json.loads(store.entry["CredentialBlob"].decode("utf-16-le")) == [
        CANDIDATE,
        None,
    ]
    assert config.read_bytes() == original
    logger.warning("%s %s", BOOTSTRAP, CANDIDATE)
    log = ws.log_path(tmp_path).read_text()
    assert BOOTSTRAP not in log and CANDIDATE not in log


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_plane_url", "https://another.example"),
        ("org_id", "another-org"),
        ("token", "another-bootstrap"),
    ],
)
def test_changed_enrollment_discards_previous_rotation(tmp_path, field, value):
    store = CredentialSet()
    settings = ConnectorSettings(token=BOOTSTRAP, org_id="old-org")
    ServiceTokenRotation(store, tmp_path, settings, lambda _: None).persist_token(
        CANDIDATE
    )
    settings = ConnectorSettings(token=BOOTSTRAP, org_id="old-org")
    setattr(settings, field, value)
    ServiceTokenRotation(store, tmp_path, settings, lambda _: None).restore()
    assert store.entry is None
    assert settings.token == (value if field == "token" else BOOTSTRAP)


def test_store_write_failure_retains_durable_pending(tmp_path, monkeypatch):
    settings = ConnectorSettings(token=BOOTSTRAP)
    store = CredentialSet()
    rotation = ServiceTokenRotation(store, tmp_path, settings, lambda _: None)
    rotation.persist_pending(CANDIDATE)

    def fail(record):
        raise OSError("store offline")

    monkeypatch.setattr(store, "CredWrite", fail)
    with pytest.raises(OSError):
        rotation.persist_token(CANDIDATE)
    assert settings.token == BOOTSTRAP and settings.pending_token == CANDIDATE
    restarted = ConnectorSettings(token=BOOTSTRAP)
    ServiceTokenRotation(store, tmp_path, restarted, lambda _: None).restore()
    assert restarted.token == BOOTSTRAP and restarted.pending_token == CANDIDATE


def test_clearing_file_pending_keeps_rotated_credential(tmp_path):
    stale = "oaconn_" + "b" * 64
    store = CredentialSet()
    enrollment = ConnectorSettings(token=BOOTSTRAP, pending_token=stale)
    journal = ServiceTokenRotation(store, tmp_path, enrollment, lambda _: None)
    journal.persist_token(stale)
    journal.persist_pending(CANDIDATE)
    journal.persist_token(CANDIDATE)

    # The administrator removes only the stale pending field from the file.
    restarted = ConnectorSettings(token=BOOTSTRAP)
    ServiceTokenRotation(store, tmp_path, restarted, lambda _: None).restore()
    assert restarted.token == CANDIDATE and restarted.pending_token is None
    assert store.entry is not None


def test_confirmation_retries_same_candidate_until_success(tmp_path, monkeypatch):
    config = tmp_path / "connector.toml"
    config.write_text(
        f'[connector]\ncontrol_plane_url = "https://cloud.example"\ntoken = "{BOOTSTRAP}"\n'
        f'pending_token = "{CANDIDATE}"\n'
    )
    # A transient outage lasts longer than the original three-attempt limit.
    monkeypatch.setattr("openadapt_flow.connector.daemon.time.sleep", lambda _: None)
    monkeypatch.setattr("openadapt_flow.connector.daemon.MAX_BACKOFF_S", 0.01)
    stop = threading.Event()
    paths = []
    digests = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/rotate"):
            digests.append(json.loads(request.content)["new_token_sha256"])
            assert request.headers["Authorization"] == "Bearer " + BOOTSTRAP
            return httpx.Response(503 if len(digests) <= 3 else 200, json={})
        assert request.headers["Authorization"] == "Bearer " + CANDIDATE
        stop.set()
        return httpx.Response(204)

    logger, redactor = ws.build_logger(tmp_path, name="test.service-rotation-retry")
    assert (
        ws.serve(
            tmp_path,
            stop,
            logger,
            redactor=redactor,
            credential_api=CredentialSet(),
            client_kwargs={"transport": httpx.MockTransport(handler)},
        )
        == 0
    )
    assert digests == [hashlib.sha256(CANDIDATE.encode()).hexdigest()] * 4
    assert paths == ["/api/connector/rotate"] * 4 + ["/api/connector/poll"]


@pytest.mark.parametrize("same_enrollment", [True, False])
def test_legacy_record_requires_its_original_binding(tmp_path, same_enrollment):
    old_file_pending = "oaconn_" + "b" * 64
    original = ConnectorSettings(token=BOOTSTRAP, pending_token=old_file_pending)
    store = CredentialSet()
    journal = ServiceTokenRotation(store, tmp_path, original, lambda _: None)
    journal.persist_token(CANDIDATE)
    v1_fields = [
        os.path.normcase(str(tmp_path.resolve())),
        original.control_plane_url,
        original.org_id,
        BOOTSTRAP,
        old_file_pending,
    ]
    store.entry["UserName"] = hashlib.sha256(json.dumps(v1_fields).encode()).hexdigest()
    entry = store.entry.copy()
    restarted = ConnectorSettings(
        token=BOOTSTRAP, pending_token=old_file_pending if same_enrollment else None
    )
    journal = ServiceTokenRotation(store, tmp_path, restarted, lambda _: None)
    if same_enrollment:
        journal.restore()
        assert restarted.token == CANDIDATE and restarted.pending_token is None
        assert store.entry["UserName"] == journal.binding
    else:
        with pytest.raises(RuntimeError, match="enrollment binding"):
            journal.restore()
        assert restarted.token == BOOTSTRAP and store.entry == entry


def test_operator_stop_interrupts_confirmation_backoff(tmp_path):
    (tmp_path / "connector.toml").write_text(
        f'[connector]\ncontrol_plane_url = "https://cloud.example"\ntoken = "{BOOTSTRAP}"\n'
        f'pending_token = "{CANDIDATE}"\n'
    )
    stop = threading.Event()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        stop.set()
        return httpx.Response(503)

    logger, redactor = ws.build_logger(tmp_path, name="test.service-rotation-stop")
    assert (
        ws.serve(
            tmp_path,
            stop,
            logger,
            redactor=redactor,
            credential_api=CredentialSet(),
            client_kwargs={"transport": httpx.MockTransport(handler)},
        )
        == 0
    )
    assert paths == ["/api/connector/rotate"]


def test_environment_token_keeps_its_external_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOC_CONNECTOR_TOKEN", BOOTSTRAP)
    stop = threading.Event()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer " + BOOTSTRAP
        if len(paths) == 2:
            stop.set()
        return httpx.Response(200, json={"connector": {"rotate_recommended": True}})

    logger, redactor = ws.build_logger(tmp_path, name="test.service-rotation-env")
    assert (
        ws.serve(
            tmp_path,
            stop,
            logger,
            redactor=redactor,
            credential_api=object(),
            client_kwargs={"transport": httpx.MockTransport(handler)},
        )
        == 0
    )
    assert paths == ["/api/connector/poll", "/api/connector/poll"]


@pytest.mark.parametrize(
    "missing_profile,expected_reads,reason",
    [
        (True, 0, "runtime.profile"),
        (False, 1, "service rotation state unavailable"),
    ],
)
def test_failed_startup_stops_before_polling(
    tmp_path, monkeypatch, missing_profile, expected_reads, reason
):
    if missing_profile:
        monkeypatch.setenv("BYOC_PROFILE", str(tmp_path / "missing.yaml"))
    (tmp_path / "connector.toml").write_text(f'[connector]\ntoken = "{BOOTSTRAP}"\n')
    store = CredentialSet()

    read = Mock(side_effect=OSError(f"store locked {BOOTSTRAP}"))
    monkeypatch.setattr(store, "CredRead", read)
    monkeypatch.setattr(
        ws,
        "_stoppable_client",
        lambda *a, **kw: pytest.fail("startup created a client"),
    )
    logger, redactor = ws.build_logger(tmp_path, name="test.service-rotation-locked")
    assert (
        ws.serve(
            tmp_path, threading.Event(), logger, redactor=redactor, credential_api=store
        )
        == 2
    )
    assert read.call_count == expected_reads
    log = ws.log_path(tmp_path).read_text()
    assert reason in log and BOOTSTRAP not in log
