"""OpenEMR adapter for the Real-App Verification Matrix."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from benchmarks.real_apps.apps.openemr.fault_injectors import (
    silent_referral_drop_rule,
)
from benchmarks.real_apps.apps.openemr.readback import readback_fhir_service_request
from benchmarks.real_apps.apps.openemr.seed import (
    default_synthetic_referral,
    openemr_local_bridge_status,
)
from benchmarks.real_apps.fault_proxy import FaultProxy, FaultRule
from benchmarks.real_apps.harness import (
    AppRunResult,
    ReadbackResult,
    RealAppSpec,
    WorkflowSpec,
)

OPENEMR_SPEC = RealAppSpec(
    app_id="openemr",
    display_name="OpenEMR 8.0.0.3",
    vertical="healthcare-ehr",
    surface="web",
    ui_architecture="legacy-php-nested-iframes",
    persistence_oracle="mysql+fhir-r4",
    compose_file=Path(__file__).resolve().parent / "docker-compose.yml",
    tier="pr",
    workflows=(
        WorkflowSpec(
            workflow_id="create_referral",
            description="Register a synthetic patient referral and verify via FHIR R4 read-back",
            expected_writes=1,
            fault_mode="drop_write_return_200",
        ),
    ),
)


class _InProcessOpenEMRFhirServer:
    """Minimal local FHIR R4 ServiceRequest store for contract-mode verification."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def __enter__(self) -> _InProcessOpenEMRFhirServer:
        store = self.records

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                rec_id = str(data.get("id") or "")
                store[rec_id] = data
                raw = json.dumps(data).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                prefix = "/ServiceRequest/"
                if self.path.startswith(prefix):
                    rec_id = self.path[len(prefix) :]
                    if rec_id in store:
                        raw = json.dumps(store[rec_id]).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/fhir+json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _post_json(url: str, payload: dict[str, Any]) -> int:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/fhir+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        resp.read()
        return int(resp.status)


class OpenEMRAdapter:
    """RealAppAdapter for OpenEMR (bridges ``benchmark/openemr_local``)."""

    spec = OPENEMR_SPEC

    def run_contract_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        fixture = default_synthetic_referral()
        bridge = openemr_local_bridge_status()

        with _InProcessOpenEMRFhirServer() as backend, FaultProxy(backend.base_url) as proxy:
            # Happy path: passthrough write + independent read-back directly on backend
            proxy.set_rule(FaultRule(mode="none"))
            _post_json(f"{proxy.base_url}/ServiceRequest", fixture.to_fhir_service_request())
            happy_rb = readback_fhir_service_request(
                backend.base_url,
                external_ref=fixture.external_ref,
                expected_patient_id=fixture.patient_id,
                expected_reason=fixture.referral_reason,
            )

            # Silent fault path: proxy drops write & returns HTTP 200
            fault_ref = "ref-syn-fault-1002"
            fault_payload = dict(fixture.to_fhir_service_request())
            fault_payload["id"] = fault_ref
            proxy.set_rule(silent_referral_drop_rule())
            fault_status = _post_json(f"{proxy.base_url}/ServiceRequest", fault_payload)
            fault_rb = readback_fhir_service_request(
                backend.base_url,
                external_ref=fault_ref,
                expected_patient_id=fixture.patient_id,
                expected_reason=fixture.referral_reason,
            )

        return AppRunResult(
            app_id=self.spec.app_id,
            display_name=self.spec.display_name,
            vertical=self.spec.vertical,
            surface=self.spec.surface,
            tier=self.spec.tier,
            mode="contract",
            happy_path_verified=happy_rb.confirmed,
            silent_fault_caught=(fault_status == 200 and not fault_rb.confirmed),
            fault_mode_tested="drop_write_return_200",
            elapsed_s=time.monotonic() - t0,
            happy_readback=happy_rb,
            fault_readback=fault_rb,
            notes=f"openemr_local_bridge={bridge['exists']}",
        )

    def run_live_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        fhir_url = os.environ.get("OPENEMR_FHIR_URL", "").strip()
        if not fhir_url:
            rb = ReadbackResult(
                confirmed=False,
                oracle="openemr-fhir-r4",
                reason="OPENEMR_FHIR_URL is not set; start docker-compose.yml first",
            )
            return AppRunResult(
                app_id=self.spec.app_id,
                display_name=self.spec.display_name,
                vertical=self.spec.vertical,
                surface=self.spec.surface,
                tier=self.spec.tier,
                mode="live",
                happy_path_verified=False,
                silent_fault_caught=False,
                fault_mode_tested="drop_write_return_200",
                elapsed_s=time.monotonic() - t0,
                happy_readback=rb,
                fault_readback=rb,
            )
        fixture = default_synthetic_referral()
        rb = readback_fhir_service_request(
            fhir_url,
            external_ref=fixture.external_ref,
            expected_patient_id=fixture.patient_id,
            expected_reason=fixture.referral_reason,
        )
        return AppRunResult(
            app_id=self.spec.app_id,
            display_name=self.spec.display_name,
            vertical=self.spec.vertical,
            surface=self.spec.surface,
            tier=self.spec.tier,
            mode="live",
            happy_path_verified=rb.confirmed,
            silent_fault_caught=False,
            fault_mode_tested="drop_write_return_200",
            elapsed_s=time.monotonic() - t0,
            happy_readback=rb,
            fault_readback=ReadbackResult(
                confirmed=False,
                oracle="openemr-fhir-r4",
                reason="Live fault verification requires proxy routing",
            ),
        )
