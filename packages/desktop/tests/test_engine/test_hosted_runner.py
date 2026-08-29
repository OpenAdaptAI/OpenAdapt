"""Contract tests for Desktop's strict hosted-runner shell."""

from __future__ import annotations

import json
import stat
import threading
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

import engine.hosted_runner as hosted_runner
from engine.config import EngineConfig
from engine.hosted_runner import (
    HttpHostedRunnerTransport,
    RunnerService,
    RunnerTransportError,
)


class WireModel:
    """Small Pydantic-shaped strict-model fake supplied by Flow in production."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        value = {key: item for key, item in self.__dict__.items() if not key.startswith("_")}
        return json.loads(json.dumps(value))

    @classmethod
    def model_validate(cls, value: Any) -> WireModel:
        if not isinstance(value, dict):
            raise ValueError("wire value is not an object")
        return cls(**value)


class DeliveryAuthority:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token


def registration_renewal_headers(current_runner_token: str | None) -> dict[str, str]:
    if current_runner_token is None or current_runner_token == "":
        return {}
    if hosted_runner._RUNNER_TOKEN.fullmatch(current_runner_token) is None:
        raise ValueError("current runner renewal credential is invalid")
    return {"x-openadapt-runner-renewal-token": current_runner_token}


CONTRACT = SimpleNamespace(
    CallbackRequest=WireModel,
    CallbackResponse=WireModel,
    DeliveryAuthority=DeliveryAuthority,
    HostedDispatch=WireModel,
    HostedDispatchRefusal=WireModel,
    HostedRecoveryBinding=WireModel,
    HostedRunResult=WireModel,
    HostedRunnerAdapter=object,
    HostedRunnerTransport=object,
    PollRequest=WireModel,
    RegisterRequest=WireModel,
    RegisterResponse=WireModel,
    RUNNER_RENEWAL_HEADER="x-openadapt-runner-renewal-token",
    registration_renewal_headers=registration_renewal_headers,
)


def _local_runtime_release() -> dict[str, dict[str, str]]:
    return {
        target: {
            "target": target,
            "admission_id": str(uuid5(NAMESPACE_URL, f"admission:{target}")),
            "admission_sha256": character * 64,
            "release_version": "1.0.0",
            "release_artifact_sha256": character * 64,
        }
        for target, character in (("flow", "a"), ("desktop", "b"), ("capture", "c"))
    }


def _registration_request() -> WireModel:
    return WireModel(
        schema_version="openadapt.hosted-runner-registration/v1",
        name="test-runner",
        platform="linux",
        agent_version="1.0.0",
        engine_version="1.0.0",
        mode="attended",
        capabilities={
            "backends": ["web", "linux"],
            "attended": True,
            "effects_substrates": ["web", "linux"],
        },
        local_runtime_release=_local_runtime_release(),
    )


def _registration_response() -> WireModel:
    return WireModel(
        schema_version="openadapt.hosted-runner-registration-result/v1",
        runner_id="11111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        runner_session_id="33333333-3333-4333-8333-333333333333",
        runner_token="oar_" + "f" * 64,
        token_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )


def _admission_artifact(label: str) -> dict[str, str]:
    raw = json.dumps({"fixture": label}, separators=(",", ":")).encode("utf-8")
    return {
        "artifact_bytes_base64": b64encode(raw).decode("ascii"),
        "artifact_sha256": sha256(raw).hexdigest(),
    }


def _dispatch(fault: str) -> WireModel:
    dispatch_id = str(uuid5(NAMESPACE_URL, f"dispatch:{fault}"))
    run_id = str(uuid5(NAMESPACE_URL, f"run:{fault}"))
    delivery_token = sha256(f"delivery:{fault}".encode()).hexdigest()
    return WireModel(
        schema_version="openadapt.hosted-runner/v1",
        dispatch_id=dispatch_id,
        dispatch_session_id=str(uuid5(NAMESPACE_URL, f"dispatch-session:{fault}")),
        tenant_id="22222222-2222-4222-8222-222222222222",
        runner_id="11111111-1111-4111-8111-111111111111",
        runner_session_id="33333333-3333-4333-8333-333333333333",
        run_id=run_id,
        workflow_id="44444444-4444-4444-8444-444444444444",
        workflow_version_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="hosted-" + sha256(fault.encode()).hexdigest(),
        lease_token="oal_" + sha256(f"lease:{fault}".encode()).hexdigest(),
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        product_release_admission=_admission_artifact("product"),
        workflow_admission=_admission_artifact("workflow"),
        managed_delivery_authority_url=(
            "https://app.openadapt.ai/api/internal/managed-delivery-permit"
        ),
        delivery_authority_token=delivery_token,
        payload={"schema_version": "openadapt.runner-dispatch-payload/v1"},
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.results: dict[str, WireModel] = {}
        self.execute_calls: list[WireModel] = []
        self.actuations: list[str] = []
        self.authorities: list[DeliveryAuthority] = []
        self.reconciliation_calls: list[WireModel] = []
        self.callback_build_calls: list[WireModel] = []

    def protected_runner_origin(self, _runner_config: Path) -> str:
        return "https://app.openadapt.ai"

    def registration_request(self, **values: Any) -> WireModel:
        capabilities = values["capabilities"]
        assert capabilities["backends"]
        assert capabilities["effects_substrates"] == capabilities["backends"]
        assert capabilities["attended"] is True
        return WireModel(
            schema_version="openadapt.hosted-runner-registration/v1",
            name=values["name"],
            platform=values["platform"],
            agent_version=values["agent_version"],
            engine_version=values["engine_version"],
            mode=values["mode"],
            capabilities=capabilities,
            local_runtime_release=_local_runtime_release(),
        )

    def recovery_binding(self, dispatch: WireModel) -> WireModel:
        return WireModel(
            schema_version="openadapt.hosted-runner-recovery/v1",
            dispatch_id=dispatch.dispatch_id,
            dispatch_session_id=dispatch.dispatch_session_id,
            runner_session_id=dispatch.runner_session_id,
            run_id=dispatch.run_id,
            workflow_id=dispatch.workflow_id,
            idempotency_key=dispatch.idempotency_key,
            lease_token=dispatch.lease_token,
            product_release_admission_sha256=(
                dispatch.product_release_admission["artifact_sha256"]
            ),
            workflow_admission_sha256=dispatch.workflow_admission["artifact_sha256"],
            bundle_content_digest="f" * 64,
            authorization_id="authorization-fixture",
        )

    def reconciliation_required(
        self,
        binding: WireModel,
        *,
        code: str = "runner_result_lost",
    ) -> WireModel:
        self.reconciliation_calls.append(binding)
        return WireModel(
            dispatch_id=binding.dispatch_id,
            run_id=binding.run_id,
            outcome="RECONCILIATION_REQUIRED",
            evidence_batch=({"fault": code, "phi_free": True},),
            terminal_verification=None,
            started=True,
            uncertain_delivery=True,
        )

    def execute(
        self,
        dispatch: WireModel,
        *,
        runner_config: Path,
        run_dir: Path,
        authority: DeliveryAuthority,
    ) -> WireModel:
        assert runner_config == run_dir.parents[2] / "runner.toml"
        assert run_dir.name == str(dispatch.run_id)
        self.execute_calls.append(dispatch)
        self.authorities.append(authority)
        dispatch_id = str(dispatch.dispatch_id)
        if dispatch_id not in self.results:
            self.actuations.append(dispatch_id)
            fault = dispatch_id.removeprefix("dispatch-")
            self.results[dispatch_id] = WireModel(
                dispatch_id=dispatch_id,
                run_id=str(dispatch.run_id),
                outcome="RECONCILIATION_REQUIRED",
                evidence_batch=({"fault": fault, "phi_free": True},),
                terminal_verification=None,
                started=True,
                uncertain_delivery=True,
            )
        return self.results[dispatch_id]

    def callback_request(self, dispatch: WireModel, result: WireModel) -> WireModel:
        self.callback_build_calls.append(dispatch)
        product_digest = getattr(dispatch, "product_release_admission_sha256", None)
        if product_digest is None:
            product_digest = dispatch.product_release_admission["artifact_sha256"]
        workflow_digest = getattr(dispatch, "workflow_admission_sha256", None)
        if workflow_digest is None:
            workflow_digest = dispatch.workflow_admission["artifact_sha256"]
        return WireModel(
            schema_version="openadapt.hosted-runner-callback/v1",
            dispatch_id=dispatch.dispatch_id,
            runner_session_id=dispatch.runner_session_id,
            idempotency_key=dispatch.idempotency_key,
            lease_token=dispatch.lease_token,
            product_release_admission_sha256=product_digest,
            workflow_admission_sha256=workflow_digest,
            events=(
                *result.evidence_batch,
                {
                    "terminal_verification": result.terminal_verification,
                    "outcome": result.outcome,
                },
            ),
        )


class FakeFlowBridge:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter

    def hosted_runner_contract(self) -> Any:
        return CONTRACT

    def hosted_runner_adapter(self, ledger_path: Path) -> FakeAdapter:
        assert ledger_path.name == "flow-one-use-ledger.sqlite3"
        return self.adapter


class FakeTransport:
    def __init__(self, host: str = "https://app.openadapt.ai") -> None:
        self.host = host
        self.queue: list[WireModel] = []
        self.register_requests: list[WireModel] = []
        self.poll_requests: list[WireModel] = []
        self.callback_requests: list[tuple[str, dict[str, Any]]] = []
        self.callback_failures = 0
        self.runner_token = ""
        self.closed = False

    def register(self, request: WireModel) -> WireModel:
        self.register_requests.append(request)
        return _registration_response()

    def set_runner_token(self, token: str) -> None:
        self.runner_token = token

    def poll(self, request: WireModel) -> WireModel | None:
        self.poll_requests.append(request)
        return self.queue.pop(0) if self.queue else None

    def callback_target(self, run_id: str) -> str:
        return hosted_runner.callback_url(self.host, run_id)

    def callback(self, run_id: str, request: WireModel) -> WireModel:
        body = request.model_dump(mode="json")
        self.callback_requests.append((run_id, body))
        if self.callback_failures:
            self.callback_failures -= 1
            raise RunnerTransportError("The callback response was lost.")
        return WireModel(
            schema_version="openadapt.hosted-runner-callback-result/v1",
            status="accepted",
            run_id=run_id,
            outcome=body["events"][-1]["outcome"],
            dispatch_state="closed",
            accepted_events=1,
        )

    def close(self) -> None:
        self.closed = True


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def log(self, event: str, **values: Any) -> None:
        self.entries.append((event, values))


class FakeDB:
    def __init__(self) -> None:
        self.runs: dict[str, str] = {}
        self.halts: list[str] = []

    def insert_run(self, run_id: str, _run_path: str, *, bundle_id: Any) -> None:
        assert bundle_id is None
        self.runs[run_id] = "pending"

    def update_run(self, run_id: str, *, status: str) -> None:
        self.runs[run_id] = status

    def insert_halt(self, halt_id: str, run_id: str, **_values: Any) -> None:
        self.halts.append(f"{halt_id}:{run_id}")


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: FakeTransport,
    adapter: FakeAdapter,
    registered: bool = False,
) -> RunnerService:
    registration: dict[str, Any] = (
        {
            **_registration_response().model_dump(mode="json"),
            "local_runtime_release": _local_runtime_release(),
        }
        if registered
        else {}
    )

    def load_registration(_host: str) -> dict[str, Any] | None:
        return dict(registration) if registration else None

    def store_registration(_host: str, value: dict[str, Any]) -> bool:
        registration.clear()
        registration.update(value)
        return True

    monkeypatch.setattr(hosted_runner, "load_runner_credential", load_registration)
    monkeypatch.setattr(hosted_runner, "store_runner_registration_secure", store_registration)
    monkeypatch.setattr(
        hosted_runner,
        "auth_header",
        lambda _host: {"Authorization": "Bearer oai_ingest_enrollment"},
    )
    config = EngineConfig(
        data_dir=tmp_path / ".openadapt",
        storage_mode="enterprise",
        hosted_host="https://app.openadapt.ai",
        runner_enabled=True,
    )
    services = SimpleNamespace(
        flow_bridge=FakeFlowBridge(adapter),
        audit=FakeAudit(),
        db=FakeDB(),
    )
    service = RunnerService(
        config,
        services,
        transport_factory=lambda **_values: transport,
    )
    if registered:
        service._registration = dict(registration)
    return service


def test_disable_reports_stopping_until_current_tick_reaches_a_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)
    entered = threading.Event()
    release = threading.Event()

    def blocking_tick() -> None:
        entered.set()
        release.wait(timeout=5)
        return None

    monkeypatch.setattr(hosted_runner, "STOP_JOIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(service, "tick", blocking_tick)
    service._transport = transport
    service.start()
    assert entered.wait(timeout=1)
    active_thread = service._thread

    status = service.disable()

    assert status["enabled"] is False
    assert status["state"] == "stopping"
    assert transport.closed is False
    release.set()
    assert active_thread is not None
    active_thread.join(timeout=1)
    assert service.status()["state"] == "disabled"
    assert service._thread is None
    assert transport.closed is True


def test_disable_during_poll_refuses_stale_dispatch_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    dispatch = _dispatch("disable-during-poll")
    entered = threading.Event()
    release = threading.Event()

    def blocking_poll(request: WireModel) -> WireModel:
        transport.poll_requests.append(request)
        entered.set()
        assert release.wait(timeout=5)
        return dispatch

    monkeypatch.setattr(hosted_runner, "STOP_JOIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(transport, "poll", blocking_poll)
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)
    service.start()
    assert entered.wait(timeout=1)
    active_thread = service._thread

    status = service.disable()

    assert status["enabled"] is False
    assert status["state"] == "stopping"
    release.set()
    assert active_thread is not None
    active_thread.join(timeout=1)
    assert adapter.execute_calls == []
    assert adapter.actuations == []
    assert transport.callback_requests == []
    assert service.journal.get(str(dispatch.dispatch_id)) is None
    assert service.services.audit.entries[-1] == (
        "hosted_runner_stale_dispatch_refused",
        {
            "dispatch_id": str(dispatch.dispatch_id),
            "run_id": str(dispatch.run_id),
        },
    )
    assert service.status()["state"] == "disabled"
    assert service._thread is None
    assert transport.closed is True


@pytest.mark.parametrize(
    "fault",
    ["backend-response-lost", "delivery-ack-lost", "receipt-unavailable"],
)
def test_three_uncertain_delivery_trials_require_reconciliation_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """Each expected uncertainty keeps one actuation and one exact callback."""

    adapter = FakeAdapter()
    transport = FakeTransport()
    dispatch = _dispatch(fault)
    transport.queue.extend([dispatch, dispatch])
    transport.callback_failures = 1
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)

    first_delay = service.tick()
    assert first_delay is not None and first_delay > 0
    registration = transport.register_requests[0].model_dump(mode="json")
    assert set(registration["capabilities"]) == {
        "backends",
        "attended",
        "effects_substrates",
    }
    assert registration["capabilities"]["backends"]
    assert (
        registration["capabilities"]["effects_substrates"]
        == registration["capabilities"]["backends"]
    )
    assert dispatch.dispatch_session_id
    pending = service.journal.get(str(dispatch.dispatch_id))
    assert pending is not None
    assert pending["phase"] == "callback_pending"
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"
    assert pending["uncertain_delivery"] is True
    assert dispatch.delivery_authority_token not in json.dumps(pending)
    journal_path = service.journal._path(str(dispatch.dispatch_id))
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert dispatch.lease_token not in json.dumps(service.status())
    assert dispatch.lease_token not in json.dumps(service.services.audit.entries)

    assert service.tick() == 0.0  # exact callback recovery; no poll and no execute
    assert service.tick() is None  # replayed terminal lease stops before Flow

    assert len(adapter.execute_calls) == 1
    assert adapter.execute_calls[0] is dispatch
    assert adapter.actuations == [str(dispatch.dispatch_id)]
    assert all(
        authority.url.endswith("/api/internal/managed-delivery-permit")
        for authority in adapter.authorities
    )
    assert {authority.token for authority in adapter.authorities} == {
        dispatch.delivery_authority_token
    }
    callback_bodies = [body for _run_id, body in transport.callback_requests]
    assert len(callback_bodies) == 2
    assert callback_bodies[0] == callback_bodies[1]
    assert all(run_id == str(dispatch.run_id) for run_id, _body in transport.callback_requests)
    assert set(callback_bodies[0]) == {
        "schema_version",
        "dispatch_id",
        "runner_session_id",
        "idempotency_key",
        "lease_token",
        "product_release_admission_sha256",
        "workflow_admission_sha256",
        "events",
    }
    assert callback_bodies[0]["events"][-1]["outcome"] == "RECONCILIATION_REQUIRED"
    assert service.status()["last_runs"][0]["phase"] == "finished"
    assert service.status()["state"] == "reauth_required"
    assert dispatch.lease_token not in journal_path.read_text()


def test_dispatch_params_preserve_finite_json_scalar_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    dispatch = _dispatch("json-scalars")
    values = {
        "text": "42",
        "integer": 42,
        "decimal": 4.25,
        "enabled": True,
    }
    dispatch.payload = {
        "schema_version": "openadapt.runner-dispatch-payload/v1",
        "params": {"kind": "inline", "values": values},
    }
    transport.queue.append(dispatch)
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)

    assert service.tick() == 0.0

    executed = adapter.execute_calls[0].payload["params"]["values"]
    assert executed == values
    assert type(executed["text"]) is str
    assert type(executed["integer"]) is int
    assert type(executed["decimal"]) is float
    assert type(executed["enabled"]) is bool


def test_http_transport_uses_exact_routes_credentials_and_bodies() -> None:
    seen: list[httpx.Request] = []
    callback_run_id = "88888888-8888-4888-8888-888888888888"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/runners/register":
            return httpx.Response(
                201,
                headers={"Cache-Control": "no-store"},
                json=_registration_response().model_dump(mode="json"),
            )
        if request.url.path == "/api/runners/poll":
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        return httpx.Response(
            202,
            headers={"Cache-Control": "no-store"},
            json={
                "schema_version": "openadapt.hosted-runner-callback-result/v1",
                "status": "accepted",
                "run_id": callback_run_id,
                "outcome": "RECONCILIATION_REQUIRED",
                "dispatch_state": "closed",
                "accepted_events": 1,
            },
        )

    audit = FakeAudit()
    client = httpx.Client(
        base_url="https://app.openadapt.ai",
        transport=httpx.MockTransport(handler),
    )
    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=audit,
        client=client,
    )
    transport.register(_registration_request())
    assert (
        transport.poll(
            WireModel(
                schema_version="openadapt.hosted-runner-poll/v1",
                runner_session_id="33333333-3333-4333-8333-333333333333",
                wait_seconds=25,
                lease_seconds=900,
            )
        )
        is None
    )
    callback = WireModel(
        schema_version="openadapt.hosted-runner-callback/v1",
        dispatch_id="77777777-7777-4777-8777-777777777777",
        runner_session_id="33333333-3333-4333-8333-333333333333",
        idempotency_key="fixture-" + "0" * 32,
        lease_token="oal_" + "a" * 64,
        product_release_admission_sha256="d" * 64,
        workflow_admission_sha256="e" * 64,
        events=({"outcome": "RECONCILIATION_REQUIRED"},),
    )
    transport.callback(callback_run_id, callback)

    assert [request.url.path for request in seen] == [
        "/api/runners/register",
        "/api/runners/poll",
        f"/api/runners/runs/{callback_run_id}/callback",
    ]
    assert seen[0].headers["Authorization"] == "Bearer oai_ingest_enrollment"
    assert seen[0].headers["x-openadapt-runner-renewal-token"] == "oar_" + "f" * 64
    assert all(
        request.headers["Authorization"] == "Bearer " + "oar_" + "f" * 64 for request in seen[1:]
    )
    assert all("x-openadapt-runner-renewal-token" not in request.headers for request in seen[1:])
    assert all(request.headers["Content-Type"] == "application/json" for request in seen)
    assert json.loads(seen[2].content) == callback.model_dump(mode="json")
    assert "run_id" not in json.loads(seen[2].content)
    audit_json = json.dumps(audit.entries)
    assert "secret" not in audit_json
    assert "oar_" + "f" * 64 not in audit_json


def test_http_registration_omits_malformed_retained_renewal_credential() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            headers={"Cache-Control": "no-store"},
            json=_registration_response().model_dump(mode="json"),
        )

    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "g" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            transport=httpx.MockTransport(handler),
        ),
    )

    transport.register(_registration_request())

    assert seen[0].headers["Authorization"] == "Bearer oai_ingest_enrollment"
    assert "x-openadapt-runner-renewal-token" not in seen[0].headers
    assert "oar_" + "g" * 64 not in json.dumps(json.loads(seen[0].content))


def test_http_transport_refuses_an_injected_client_on_another_origin() -> None:
    with pytest.raises(RunnerTransportError, match="protected origin"):
        HttpHostedRunnerTransport(
            host="https://app.openadapt.ai",
            contract=CONTRACT,
            enrollment_token="oai_ingest_enrollment",
            runner_token="oar_" + "f" * 64,
            audit=FakeAudit(),
            client=httpx.Client(
                base_url="https://other.example",
                transport=httpx.MockTransport(
                    lambda _request: pytest.fail("an origin mismatch must not reach HTTP")
                ),
            ),
        )


def test_http_transport_refuses_renewal_header_outside_registration() -> None:
    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("a renewal header must not reach poll HTTP")
            ),
        ),
    )

    with pytest.raises(RunnerTransportError, match="restricted to registration"):
        transport._post(
            "/api/runners/poll",
            WireModel(schema_version="openadapt.hosted-runner-poll/v1"),
            response_type=WireModel,
            expected_status=200,
            headers={
                "Authorization": "Bearer " + "oar_" + "f" * 64,
                "Content-Type": "application/json",
                "X-OpenAdapt-Runner-Renewal-Token": "oar_" + "f" * 64,
            },
        )


def test_http_transport_refuses_case_insensitive_default_renewal_header() -> None:
    with pytest.raises(RunnerTransportError, match="retains a renewal credential"):
        HttpHostedRunnerTransport(
            host="https://app.openadapt.ai",
            contract=CONTRACT,
            enrollment_token="oai_ingest_enrollment",
            runner_token="oar_" + "f" * 64,
            audit=FakeAudit(),
            client=httpx.Client(
                base_url="https://app.openadapt.ai",
                headers={"X-OpenAdapt-Runner-Renewal-Token": "oar_" + "e" * 64},
                transport=httpx.MockTransport(
                    lambda _request: pytest.fail("a retained header must not reach HTTP")
                ),
            ),
        )


def test_http_registration_never_follows_a_cross_origin_redirect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "app.openadapt.ai":
            return httpx.Response(
                307,
                headers={
                    "Location": "https://other.example/steal",
                    "Cache-Control": "no-store",
                },
            )
        pytest.fail("the renewal credential must not follow a redirect")

    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            follow_redirects=True,
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(RunnerTransportError, match="HTTP 307"):
        transport.register(_registration_request())

    assert [str(request.url) for request in seen] == [
        "https://app.openadapt.ai/api/runners/register"
    ]


def test_http_transport_refuses_noncanonical_callback_run_uuid() -> None:
    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("an invalid run id must not reach HTTP")
            ),
        ),
    )

    with pytest.raises(RunnerTransportError, match="canonical run UUID"):
        transport.callback("run-1", WireModel(schema_version="invalid"))


@pytest.mark.parametrize(
    "host",
    [
        "http://app.openadapt.ai",
        "https://APP.openadapt.ai",
        "https://app.openadapt.ai/",
        "https://app.openadapt.ai:443",
        "https://user@app.openadapt.ai",
        "https://app.openadapt.ai/path",
    ],
)
def test_http_transport_requires_one_canonical_https_origin(host: str) -> None:
    with pytest.raises(RunnerTransportError, match="origin"):
        HttpHostedRunnerTransport(
            host=host,
            contract=CONTRACT,
            enrollment_token="enrollment-token",
            runner_token="oar_" + "f" * 64,
            audit=FakeAudit(),
        )


def test_http_callback_retries_exact_body_after_lost_accepted_response() -> None:
    run_id = "88888888-8888-4888-8888-888888888888"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("accepted response was lost", request=request)
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "schema_version": "openadapt.hosted-runner-callback-result/v1",
                "status": "duplicate",
                "run_id": run_id,
                "outcome": "RECONCILIATION_REQUIRED",
                "dispatch_state": "closed",
                "accepted_events": 1,
            },
        )

    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            transport=httpx.MockTransport(handler),
        ),
    )
    callback = WireModel(
        schema_version="openadapt.hosted-runner-callback/v1",
        dispatch_id="77777777-7777-4777-8777-777777777777",
        runner_session_id="33333333-3333-4333-8333-333333333333",
        idempotency_key="fixture-" + "0" * 32,
        lease_token="oal_" + "a" * 64,
        product_release_admission_sha256="d" * 64,
        workflow_admission_sha256="e" * 64,
        events=({"outcome": "RECONCILIATION_REQUIRED"},),
    )

    with pytest.raises(RunnerTransportError, match="did not complete"):
        transport.callback(run_id, callback)
    response = transport.callback(run_id, callback)

    assert response.status == "duplicate"
    assert [request.url.path for request in requests] == [
        f"/api/runners/runs/{run_id}/callback",
        f"/api/runners/runs/{run_id}/callback",
    ]
    assert requests[0].content == requests[1].content
    assert all(
        request.headers["Authorization"] == "Bearer " + "oar_" + "f" * 64 for request in requests
    )


@pytest.mark.parametrize(
    ("http_status", "wire_status"),
    ((202, "duplicate"), (200, "accepted")),
)
def test_http_callback_rejects_status_that_disagrees_with_http_code(
    http_status: int,
    wire_status: str,
) -> None:
    run_id = "88888888-8888-4888-8888-888888888888"
    transport = HttpHostedRunnerTransport(
        host="https://app.openadapt.ai",
        contract=CONTRACT,
        enrollment_token="oai_ingest_enrollment",
        runner_token="oar_" + "f" * 64,
        audit=FakeAudit(),
        client=httpx.Client(
            base_url="https://app.openadapt.ai",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    http_status,
                    headers={"Cache-Control": "no-store"},
                    json={
                        "schema_version": ("openadapt.hosted-runner-callback-result/v1"),
                        "status": wire_status,
                        "run_id": run_id,
                        "outcome": "RECONCILIATION_REQUIRED",
                        "dispatch_state": "closed",
                        "accepted_events": 1,
                    },
                )
            ),
        ),
    )

    with pytest.raises(RunnerTransportError, match="did not match HTTP"):
        transport.callback(
            run_id,
            WireModel(
                schema_version="openadapt.hosted-runner-callback/v1",
                dispatch_id="77777777-7777-4777-8777-777777777777",
                runner_session_id="33333333-3333-4333-8333-333333333333",
                idempotency_key="fixture-" + "0" * 32,
                lease_token="oal_" + "a" * 64,
                product_release_admission_sha256="d" * 64,
                workflow_admission_sha256="e" * 64,
                events=({"outcome": "RECONCILIATION_REQUIRED"},),
            ),
        )


def test_windows_journal_refuses_unsafe_open_file_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = hosted_runner.RunnerJournal(tmp_path / "journal")
    dispatch_id = "77777777-7777-4777-8777-777777777777"
    journal.record(dispatch_id, "finished", outcome="VERIFIED")
    monkeypatch.setattr(hosted_runner, "_is_windows", lambda: True)

    def refuse_unsafe(_descriptor: int) -> None:
        raise hosted_runner.RunnerJournalError("Windows runner journal ACL is unsafe")

    monkeypatch.setattr(hosted_runner, "_require_private_windows_acl", refuse_unsafe)

    with pytest.raises(hosted_runner.RunnerJournalError, match="ACL is unsafe"):
        journal.get(dispatch_id)


def test_windows_journal_refuses_when_temp_file_acl_check_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = hosted_runner.RunnerJournal(tmp_path / "journal")
    dispatch_id = "77777777-7777-4777-8777-777777777777"
    monkeypatch.setattr(hosted_runner, "_is_windows", lambda: True)

    def refuse_unavailable(_descriptor: int) -> None:
        raise hosted_runner.RunnerJournalError(
            "Windows runner journal ACL verification is unavailable"
        )

    monkeypatch.setattr(hosted_runner, "_require_private_windows_acl", refuse_unavailable)

    with pytest.raises(hosted_runner.RunnerJournalError, match="verification is unavailable"):
        journal.record(dispatch_id, "leased", recovery_binding={"lease_token": "secret"})

    assert not journal._path(dispatch_id).exists()
    assert list((tmp_path / "journal").glob("*.tmp")) == []


@pytest.mark.skipif(hosted_runner._is_windows(), reason="POSIX mode check")
def test_journal_refuses_to_unlink_an_unsafe_temporary_file(tmp_path: Path) -> None:
    journal = hosted_runner.RunnerJournal(tmp_path / "journal")
    dispatch_id = "77777777-7777-4777-8777-777777777777"
    journal._secure_dir()
    temporary = journal._dir / f".{dispatch_id}.unsafe.tmp"
    temporary.write_text("credential-bearing")
    temporary.chmod(0o644)

    with pytest.raises(hosted_runner.RunnerJournalError, match="journal is unsafe"):
        journal.clear_temporary(dispatch_id)

    assert temporary.exists()


def test_mismatched_callback_response_keeps_exact_callback_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MismatchedTransport(FakeTransport):
        def callback(self, run_id: str, request: WireModel) -> WireModel:
            response = super().callback(run_id, request)
            response.outcome = "VERIFIED"
            return response

    adapter = FakeAdapter()
    transport = MismatchedTransport()
    dispatch = _dispatch("mismatched-callback")
    transport.queue.append(dispatch)
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)

    delay = service.tick()

    assert delay is not None and delay > 0
    assert adapter.actuations == [str(dispatch.dispatch_id)]
    entry = service.journal.get(str(dispatch.dispatch_id))
    assert entry is not None
    assert entry["phase"] == "callback_pending"
    assert entry["outcome"] == "RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("phase", ["leased", "executing"])
def test_interrupted_lease_uses_recovery_binding_without_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    service = _service(
        tmp_path,
        monkeypatch,
        transport=transport,
        adapter=adapter,
        registered=True,
    )
    dispatch = _dispatch("process-exit")
    binding = adapter.recovery_binding(dispatch)
    target = hosted_runner.callback_url(
        service.config.hosted_host,
        str(dispatch.run_id),
    )
    service.journal.record(
        str(dispatch.dispatch_id),
        phase,
        run_id=str(dispatch.run_id),
        workflow_id=str(dispatch.workflow_id),
        callback_url=target,
        recovery_binding=binding.model_dump(mode="json"),
    )

    assert service.tick() == 0.0

    assert adapter.execute_calls == []
    assert adapter.actuations == []
    assert transport.poll_requests == []
    assert len(adapter.reconciliation_calls) == 1
    assert [item.model_dump(mode="json") for item in adapter.callback_build_calls] == [
        binding.model_dump(mode="json")
    ]
    assert len(transport.callback_requests) == 1
    assert transport.callback_requests[0][0] == str(dispatch.run_id)
    assert transport.callback_requests[0][1]["events"][-1]["outcome"] == "RECONCILIATION_REQUIRED"
    retained_entry = service.journal.get(str(dispatch.dispatch_id))
    assert retained_entry is not None
    assert retained_entry["phase"] == "finished"
    assert retained_entry["outcome"] == "RECONCILIATION_REQUIRED"
    retained = json.dumps(retained_entry)
    assert "delivery_authority_token" not in retained
    assert dispatch.delivery_authority_token not in retained
    assert dispatch.lease_token not in retained
    assert dispatch.lease_token not in json.dumps(service.status())
    assert service.status()["state"] == "polling"
    assert service.status()["last_error"] is None


def test_interrupted_callback_ambiguity_keeps_private_state_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    transport.callback_failures = 1
    service = _service(
        tmp_path,
        monkeypatch,
        transport=transport,
        adapter=adapter,
        registered=True,
    )
    dispatch = _dispatch("interrupted-callback-ambiguity")
    binding = adapter.recovery_binding(dispatch)
    target = hosted_runner.callback_url(
        service.config.hosted_host,
        str(dispatch.run_id),
    )
    service.journal.record(
        str(dispatch.dispatch_id),
        "executing",
        run_id=str(dispatch.run_id),
        workflow_id=str(dispatch.workflow_id),
        callback_url=target,
        recovery_binding=binding.model_dump(mode="json"),
    )

    delay = service.tick()

    assert delay is not None and delay > 0
    pending = service.journal.get(str(dispatch.dispatch_id))
    assert pending is not None
    assert pending["phase"] == "callback_pending"
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"
    assert pending["callback_url"] == target
    assert pending["callback"]["lease_token"] == dispatch.lease_token
    assert adapter.execute_calls == []
    assert transport.poll_requests == []
    assert dispatch.lease_token not in json.dumps(service.status())
    assert dispatch.lease_token not in json.dumps(service.services.audit.entries)
    stale_temporary = service.journal._dir / (
        f".{dispatch.dispatch_id}.accepted-before-replace.tmp"
    )
    stale_temporary.write_text(json.dumps({"lease_token": dispatch.lease_token}))
    stale_temporary.chmod(0o600)

    assert service.tick() == 0.0
    finished = service.journal.get(str(dispatch.dispatch_id))
    assert finished is not None
    assert finished["phase"] == "finished"
    assert finished["callback"] is None
    assert dispatch.lease_token not in json.dumps(finished)
    assert not stale_temporary.exists()
    assert adapter.execute_calls == []
    assert transport.poll_requests == []


def test_interrupted_callback_uses_retained_url_after_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    service = _service(
        tmp_path,
        monkeypatch,
        transport=transport,
        adapter=adapter,
        registered=True,
    )
    dispatch = _dispatch("retained-callback-target")
    binding = adapter.recovery_binding(dispatch)
    retained_target = hosted_runner.callback_url(
        "https://app.openadapt.ai",
        str(dispatch.run_id),
    )
    service.journal.record(
        str(dispatch.dispatch_id),
        "executing",
        run_id=str(dispatch.run_id),
        workflow_id=str(dispatch.workflow_id),
        callback_url=retained_target,
        recovery_binding=binding.model_dump(mode="json"),
    )
    service.config.hosted_host = "https://different.example"

    assert service.tick() == 0.0

    assert transport.callback_target(str(dispatch.run_id)) == retained_target
    assert len(transport.callback_requests) == 1
    assert transport.poll_requests == []
    assert adapter.execute_calls == []


def test_air_gapped_enable_refuses_before_loading_flow_or_network(tmp_path: Path) -> None:
    class FailBridge:
        def hosted_runner_contract(self) -> Any:
            raise AssertionError("Flow must not load")

    config = EngineConfig(
        data_dir=tmp_path,
        storage_mode="air-gapped",
        runner_enabled=False,
    )
    services = SimpleNamespace(flow_bridge=FailBridge())
    service = RunnerService(
        config,
        services,
        transport_factory=lambda **_values: (_ for _ in ()).throw(
            AssertionError("network transport must not be built")
        ),
    )

    status = service.enable()

    assert status["enabled"] is False
    assert status["state"] == "error"
    assert "air-gapped" in status["last_error"]


def test_missing_flow_adapter_refuses_before_network(tmp_path: Path) -> None:
    class MissingBridge:
        def hosted_runner_contract(self) -> Any:
            raise hosted_runner.HostedRunnerAdapterUnavailableError(
                "This Desktop build needs a newer bundled OpenAdapt Flow runtime."
            )

    config = EngineConfig(
        data_dir=tmp_path,
        storage_mode="enterprise",
        runner_enabled=False,
    )
    services = SimpleNamespace(flow_bridge=MissingBridge())
    service = RunnerService(
        config,
        services,
        transport_factory=lambda **_values: (_ for _ in ()).throw(
            AssertionError("network transport must not be built")
        ),
    )

    status = service.enable()

    assert status["enabled"] is True
    assert status["state"] == "incompatible"
    assert "newer bundled OpenAdapt Flow" in status["last_error"]


def test_missing_protected_runner_host_refuses_before_network_or_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingHostAdapter(FakeAdapter):
        def protected_runner_origin(self, _runner_config: Path) -> str:
            raise ValueError("hosted runner requires a protected runner host origin")

    adapter = MissingHostAdapter()
    transport = FakeTransport()
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)

    assert service.tick() is None
    status = service.status()

    assert status["enabled"] is True
    assert status["state"] == "error"
    assert (
        f'Set [runner].host = "{service.config.hosted_host}" in '
        f"{service.runner_config}" in status["last_error"]
    )
    assert "won't edit this operator trust manifest" in status["last_error"]
    assert transport.register_requests == []
    assert transport.poll_requests == []
    assert service._thread is None


def test_desktop_host_mismatch_refuses_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    transport = FakeTransport()
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)
    service.config.hosted_host = "https://different.example"

    assert service.tick() is None

    assert transport.register_requests == []
    assert transport.poll_requests == []
    assert service.status()["state"] == "error"
    assert "differs from the protected runner host" in service.status()["last_error"]


def test_dispatch_refuses_callback_target_outside_the_bound_transport_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RedirectingTransport(FakeTransport):
        def callback_target(self, run_id: str) -> str:
            return hosted_runner.callback_url("https://other.example", run_id)

    adapter = FakeAdapter()
    transport = RedirectingTransport()
    dispatch = _dispatch("redirected-callback-target")
    transport.queue.append(dispatch)
    service = _service(tmp_path, monkeypatch, transport=transport, adapter=adapter)

    delay = service.tick()

    assert delay is not None and delay > 0
    assert adapter.execute_calls == []
    assert transport.callback_requests == []
    assert service.journal.get(str(dispatch.dispatch_id)) is None
